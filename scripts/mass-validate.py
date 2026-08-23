#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validación masiva reproducible: N paquetes `-bin` + M de fuente (o T
aleatorios del índice completo) → build + smoke funcional, con motor dual
(xbps-src y Nix).

Objetivo (AUDIT-2026-08 / plan madurez): iterar hasta 100% — cada paquete
termina en OK o clasificado con causa raíz agrupable.

Muestreo determinista: packages.gz del AUR + random.Random(seed). Los
bloqueados por seguridad NO consumen cuota: se construye una cola de
reemplazo (×3) y se continúa hasta completar N evaluados-no-bloqueados o
agotarla; quedan registrados en la clave `@blocked` del JSON. Con --shard
I/N cada proceso toma pool[I::N] con su propia cuota (la suma entre shards
puede diferir ±1 de N) y escribe results-shard-IofN.{json,md}. Logs
completos por paquete+motor en $AUR2XBPS_LOGS (default /tmp/aur2xbps-logs).
Reanudable: los ya-OK y los bloqueados registrados se saltan en
re-ejecuciones.

Uso:
  python3 scripts/mass-validate.py --count-bin 100 --count-src 100 --engine both
  python3 scripts/mass-validate.py --count-total 100 --seed 12345 --shard 2/10
  python3 scripts/mass-validate.py --print-queue --count-total 12 --seed 7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.common.paths import DEFAULT_DB            # noqa: E402
from src.common.tools import has_nix               # noqa: E402


@dataclass
class Result:
    pkg: str
    kind: str                       # bin|src
    engine_results: dict = field(default_factory=dict)   # engine -> {stages...}
    sha256: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.engine_results) and all(
            e.get("ok") for e in self.engine_results.values())


# ---------------------------------------------------------------- sampling
BLOCKED_KEY = "@blocked"   # clave reservada del JSON (@ no es válido en pkgname)
POOL_MULT = 3


def fetch_index(client) -> list[str]:
    """Índice completo de AUR vía caché SQLite (packages_index)."""
    count = client.sync_packages_index()
    cur = client._conn.execute("SELECT pkgname FROM packages_index")
    names = [r[0] for r in cur.fetchall()]
    print(f"índice AUR: {count} paquetes")
    return names


def _split_kinds(names: list[str]) -> tuple[list[str], list[str]]:
    bins = sorted(n for n in names if n.endswith("-bin"))
    srcs = sorted(
        n for n in names
        if not n.endswith(("-bin", "-git", "-svn", "-hg", "-cvs", "-bzr"))
        and not n.startswith(("lib32-", "multilib-")))
    return bins, srcs


def sample_queue(names: list[str], n_bin: int, n_src: int,
                 seed: int) -> list[tuple[str, str]]:
    """Cola ordenada de candidatos con reemplazo ×3 por tipo (mismo rng)."""
    rng = random.Random(seed)
    bins, srcs = _split_kinds(names)
    pb = rng.sample(bins, min(n_bin * POOL_MULT, len(bins))) if bins else []
    ps = rng.sample(srcs, min(n_src * POOL_MULT, len(srcs))) if srcs else []
    return [(p, "bin") for p in pb] + [(p, "src") for p in ps]


def sample_queue_total(names: list[str], total: int,
                       seed: int) -> list[tuple[str, str]]:
    """Índice COMPLETO sin exclusiones (-bin/-git/lib32 incluidos):
    cola min(len(names), total×3), reproducible por seed."""
    rng = random.Random(seed)
    picked = rng.sample(sorted(names), min(total * POOL_MULT, len(names)))
    return [(n, "bin" if n.endswith("-bin") else "src") for n in picked]


def parse_shard(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)/(\d+)", spec.strip())
    if not m or int(m.group(2)) < 1 or int(m.group(1)) >= int(m.group(2)):
        raise argparse.ArgumentTypeError(
            f"shard inválido: {spec!r} (esperado 'i/N' con 0 ≤ i < N)")
    return int(m.group(1)), int(m.group(2))


def shard_quotas(total: int, n_shards: int) -> list[int]:
    """Cuota por shard: ceil(total/n) para todos +1 a los primeros total%n."""
    base = -(-total // n_shards)
    rem = total % n_shards
    return [base + (1 if j < rem else 0) for j in range(n_shards)]


# ---------------------------------------------------------------- runner
def run_cli(args: list[str], timeout: int, pkg: str = "", engine: str = "",
            logdir: Path | None = None) -> tuple[int, dict | None, str]:
    """Invoca aur2xbps aislado; retorna (rc, json_stdout, stderr_tail) y
    persiste stdout completo (log-*.json) y stderr (err-*.log) en logdir."""
    cmd = [sys.executable, "-m", "src.cli"] + args
    env = dict(os.environ)
    env["AUR2XBPS_BUILD_TIMEOUT"] = str(timeout)
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           timeout=timeout + 120, env=env)
        out_txt, err_txt, rc = r.stdout or "", r.stderr or "", r.returncode
    except subprocess.TimeoutExpired as e:
        out_txt = _as_text(getattr(e, "stdout", None))
        err_txt = _as_text(getattr(e, "stderr", None))
        err_txt += f"\ntimeout global {timeout + 120}s\n"
        rc = 124
    _save_logs(logdir, pkg, engine, out_txt, err_txt)
    # stdout es SOLO el JSON del CLI (_StderrOnly manda el resto a stderr),
    # pero puede ser multilinea → parsear el bloque completo
    payload = _parse_json_out(out_txt)
    if rc != 0:
        tail = (err_txt + "\n" + out_txt)[-500:]
        return rc, payload, tail
    if payload is None:
        payload = {"ok": True, "unparsed": True}
    return rc, payload, err_txt[-800:]


def _as_text(data) -> str:
    if data is None:
        return ""
    return (data.decode("utf-8", errors="replace")
            if isinstance(data, bytes) else str(data))


def _save_logs(logdir: Path | None, pkg: str, engine: str,
               out_txt: str, err_txt: str) -> None:
    if logdir is None or not pkg:
        return
    safe = re.sub(r"[^A-Za-z0-9_.+-]", "_", pkg)
    try:
        (logdir / f"log-{safe}-{engine}.json").write_text(out_txt)
        (logdir / f"err-{safe}-{engine}.log").write_text(err_txt)
    except OSError:
        pass


def _parse_json_out(text: str) -> dict | None:
    """Extrae el primer objeto JSON balanceado de la salida del CLI."""
    text = text.strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


def smoke_functional(xbps_path: Path, timeout: int = 20) -> dict:
    """Smoke SIN instalar: extrae el .xbps a un tmp, localiza un binario de
    /usr/bin, lo ejecuta con --version/--help (LD_LIBRARY_PATH apuntando a
    los lib del propio paquete) y clasifica el resultado."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="aur2xbps-smoke-"))
    try:
        try:
            with tarfile.open(xbps_path, "r:zst") as tf:
                tf.extractall(tmp, filter="data")     # anti-traversal (3.12+)
        except Exception as e:
            return {"smoke": False, "reason": f"xbps ilegible: {type(e).__name__}"}
        bin_dir = tmp / "usr" / "bin"
        if not bin_dir.is_dir():
            # fallback bundle: /opt/<app> o /usr/lib/<app>
            bins = sorted(tmp.rglob("bin")) or []
            bin_dir = next((b for b in bins if b.is_dir()
                            and any(p.is_file() for p in b.iterdir())), None)
            if bin_dir is None:
                return {"smoke": False, "reason": "sin_binarios_usr_bin"}
        # candidatos: ficheros directos O symlinks (instaladores -bin enlazan
        # /usr/bin → ../lib/<app>; ejecutar el destino resuelto)
        cands: list[tuple[str, Path]] = []
        for p in sorted(bin_dir.iterdir()):
            if not p.is_file() and not p.is_symlink():
                continue
            target = p.resolve()
            if target.is_file():
                cands.append((p.name, target))
        if not cands:
            return {"smoke": False, "reason": "sin_ejecutables"}
        base = xbps_path.name.split("-")[0]
        cand = next((c for c in cands if c[0] == base), None) \
            or next((c for c in cands if "-bin" not in c[0]), cands[0])
        env = {"PATH": "/usr/bin:/bin",
               "LD_LIBRARY_PATH": ":".join(
                   str(tmp / d) for d in ("usr/lib", "usr/lib64", "lib")
                   if (tmp / d).is_dir()),
               "HOME": str(tmp)}
        for flag in ("--version", "--help"):
            try:
                r = subprocess.run([str(cand[1]), flag], capture_output=True,
                                   text=True, timeout=timeout, env=env)
            except subprocess.TimeoutExpired:
                continue          # app GUI arrancó y esperó X: no es fallo de carga
            err = (r.stderr or "").lower()
            if r.returncode == -11:
                return {"smoke": False, "reason": "segfault", "bin": cand[0]}
            if r.returncode in (-4, -7):
                return {"smoke": False, "reason": f"signal{-r.returncode}",
                        "bin": cand[0]}
            if "not found" in err and ("shared" in err or ".so" in err):
                return {"smoke": False, "reason": "loader_error",
                        "bin": cand[0]}
            if r.returncode <= 1:
                return {"smoke": True, "reason": "exit_ok",
                        "bin": cand[0], "flag": flag}
        return {"smoke": False, "reason": "exit_no_reconocido", "bin": cand[0]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def free_mb(path: Path = Path("/")) -> int:
    st = shutil.disk_usage(path)
    return st.free // (1024 * 1024)


def _publish_binpkgs(binpkgs: list[Path]) -> None:
    """Copia los .xbps del motor xbps-src al repo común (mejor esfuerzo).

    Desactivable con AUR2XBPS_COPY_BINPKGS=0 (CI atribuye por origen y
    evitaría mezclar binarios xbps-src dentro del repo del motor nix).
    """
    if os.environ.get("AUR2XBPS_COPY_BINPKGS", "1") == "0":
        return
    try:
        from src.common.config import get_config
        repo = get_config().repo_x86_64
        repo.mkdir(parents=True, exist_ok=True)
        for b in binpkgs:
            shutil.copy2(b, repo / b.name)
    except Exception as e:                                  # noqa: BLE001
        print(f"WARNING: copia al repo falló: {e}")


def validate_one(pkg: str, kind: str, engines: list[str],
                 timeout: int, keep_artifacts: bool) -> dict:
    """Build+smoke para UN paquete bajo cada motor. Nunca lanza."""
    LOGDIR = Path(os.environ.get("AUR2XBPS_LOGS", "/tmp/aur2xbps-logs"))
    LOGDIR.mkdir(parents=True, exist_ok=True)
    out: dict = {"pkg": pkg, "kind": kind, "engines": {}}
    for engine in engines:
        stages = {"prepare": False, "build": False, "smoke": False}
        detail = ""
        t0 = time.time()
        rc, payload, tail = run_cli(["build", pkg, "--engine", engine],
                                    timeout, pkg, engine, LOGDIR)
        if rc != 0:
            # distinguir bloqueo de seguridad (exit 2/3) de fallo de build
            blocked = rc in (2, 3) or "Atomic" in tail or "bloquead" in tail.lower()
            detail = ("bloqueado por filtro de seguridad" if blocked
                      else tail[-300:])
        elif payload and payload.get("blocked"):
            detail = "bloqueado por filtro de seguridad (payload)"
        else:
            stages["prepare"] = True
            stages["build"] = bool(payload and payload.get("ok"))
            binpkgs = [Path(p) for p in (payload or {}).get("binpkgs", [])]
            if binpkgs:
                if engine == "xbps-src":
                    _publish_binpkgs(binpkgs)
                xbps = binpkgs[0]
                h = hashlib.sha256(xbps.read_bytes()).hexdigest()
                sm = smoke_functional(xbps, timeout=min(30, timeout // 60))
                stages["smoke"] = sm.pop("smoke")
                out.setdefault("smoke_detail", sm)
                out["sha256"] = h[:16]
                if not stages["smoke"] and sm.get("reason"):
                    detail = f"smoke: {sm['reason']} ({sm.get('bin', '?')})"
                if not keep_artifacts and stages["smoke"]:
                    xbps.unlink(missing_ok=True)
            else:
                detail = "build ok sin .xbps"
            if not stages["build"]:
                detail = (payload or {}).get("error", "") or tail[-200:]
        out["engines"][engine] = {
            **stages, "seconds": round(time.time() - t0, 1),
            "ok": all(stages.values()),
            **({"detail": detail} if detail else {})}
    if any(e.get("detail", "").startswith("bloqueado")
           for e in out["engines"].values()):
        out["blocked"] = True
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(prog="mass-validate")
    ap.add_argument("--count-bin", type=int, default=100)
    ap.add_argument("--count-src", type=int, default=100)
    ap.add_argument("--count-total", type=int, default=0,
                    help="muestrea N nombres ALEATORIOS del índice COMPLETO "
                         "de AUR (incluye -bin/-git/lib32); ignora "
                         "--count-bin/--count-src")
    ap.add_argument("--seed", type=int, default=20260823)
    ap.add_argument("--engine", choices=["both", "nix", "xbps-src"],
                    default="both")
    ap.add_argument("--workers", type=int, default=6,
                    help="paralelismo de preparación RPC/clone")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("AUR2XBPS_BUILD_TIMEOUT", 3600)))
    ap.add_argument("--min-free-mb", type=int, default=1500)
    ap.add_argument("--keep-artifacts", action="store_true")
    ap.add_argument("--shard", type=parse_shard, metavar="I/N",
                    help="procesa pool[I::N]; --out pasa a "
                         "results-shard-IofN.{json,md}; la suma de evaluados "
                         "entre shards puede diferir ±1 de N")
    ap.add_argument("--print-queue", action="store_true",
                    help="imprime la cola resultante (paquete<TAB>tipo) tras "
                         "sampling/sharding y sale sin construir nada")
    ap.add_argument("--out",
                    help="default mass-results.json (results-shard-IofN.json "
                         "con --shard)")
    ap.add_argument("--only", nargs="*",
                    help="validar solo estos paquetes (prioridad sobre conteos)")
    args = ap.parse_args()

    from src.aur.client import AURClient
    client = AURClient(db_path=DEFAULT_DB)

    if args.only:
        pool = [(p, "bin" if p.endswith("-bin") else "src") for p in args.only]
        quota_total = len(pool)
    elif args.count_total > 0:
        pool = sample_queue_total(fetch_index(client), args.count_total,
                                  args.seed)
        quota_total = args.count_total
    else:
        pool = sample_queue(fetch_index(client), args.count_bin,
                            args.count_src, args.seed)
        quota_total = args.count_bin + args.count_src

    if args.shard:
        si, sn = args.shard
        queue = pool[si::sn]
        quota = shard_quotas(quota_total, sn)[si]
    else:
        si, sn, queue, quota = 0, 0, pool, quota_total

    results_path = Path(args.out or (
        f"results-shard-{si}of{sn}.json" if args.shard else "mass-results.json"))

    if args.print_queue:
        print(f"# cola: {len(queue)} candidatos · cuota: {quota} "
              f"evaluados-no-bloqueados"
              + (f" · shard {si}/{sn}" if args.shard else ""))
        for p, k in queue:
            print(f"{p}\t{k}")
        return 0

    engines = ["xbps-src", "nix"] if args.engine == "both" else [args.engine]
    if "nix" in engines and not has_nix():
        print("WARNING: nix no disponible; corrida solo-xbps-src")
        engines.remove("nix")

    results: dict = {}
    if results_path.exists():
        results = json.loads(results_path.read_text())
        done_ok = {k for k, v in results.items()
                   if isinstance(v, dict) and v.get("ok")}
        prev_blocked = {b.get("pkg") for b in results.get(BLOCKED_KEY, [])
                        if isinstance(b, dict)}
        queue = [(p, k) for p, k in queue
                 if p not in done_ok and p not in prev_blocked]
        print(f"reanudando: {len(done_ok)} ya-OK y {len(prev_blocked)} "
              f"bloqueados previos saltados")
    results.setdefault(BLOCKED_KEY, [])

    print(f"cola: {len(queue)} paquetes × motores {engines} · cuota: {quota} "
          f"evaluados-no-bloqueados")
    evaluados = 0
    for i, (pkg, kind) in enumerate(queue, 1):
        if evaluados >= quota:
            break
        if free_mb() < args.min_free_mb:
            print(f"[{i}/{len(queue)}] disco bajo ({free_mb()}MB): limpieza…")
            vp = Path(os.environ.get("AUR2XBPS_VP",
                      Path.home() / ".local/share/aur2xbps/void/void-packages"))
            if vp.exists():
                subprocess.run(["./xbps-src", "clean", "ALL"], cwd=vp,
                               capture_output=True, timeout=600)
                shutil.rmtree(vp / "hostdir" / "sources", ignore_errors=True)
            if has_nix():
                try:
                    subprocess.run(["nix-store", "--gc"], capture_output=True,
                                   timeout=1800)
                except Exception:                            # noqa: BLE001
                    pass
            if free_mb() < args.min_free_mb:
                print("  disco aún crítico: abortando para proteger el host")
                break
        print(f"[{i}/{len(queue)}] {pkg} ({kind}) …", flush=True)
        try:
            r = validate_one(pkg, kind, engines, args.timeout, args.keep_artifacts)
        except Exception as e:                                  # noqa: BLE001
            r = {"pkg": pkg, "kind": kind, "engines": {},
                 "crash": f"{type(e).__name__}: {str(e)[:200]}"}
        r["ok"] = bool(r.get("engines")) and all(
            e.get("ok") for e in r["engines"].values())
        was_blocked = r.pop("blocked", False) or any(
            e.get("detail", "").startswith("bloqueado")
            for e in r.get("engines", {}).values())
        if was_blocked:
            causa = next((e.get("detail", "bloqueado")
                          for e in r.get("engines", {}).values()
                          if e.get("detail", "").startswith("bloqueado")),
                         "bloqueado")
            results[BLOCKED_KEY].append({"pkg": pkg, "causa": causa})
            print(f"   🚫 excluido (no cuenta para la cuota): {causa}")
        else:
            results[pkg] = r
            evaluados += 1
            marks = json.dumps({k: v.get("ok")
                                for k, v in r.get("engines", {}).items()})
            print(f"   {'✅' if r['ok'] else '❌'} {marks} "
                  f"[{evaluados}/{quota}]")
        results_path.write_text(json.dumps(results, indent=2))

    _summary(results, results_path.with_suffix(".md"), engines)
    return 0


def _summary(results: dict, md_path: Path, engines: list[str]) -> None:
    blocked_list = [b for b in results.get(BLOCKED_KEY, [])
                    if isinstance(b, dict)]
    norm = {k: v for k, v in results.items()
            if isinstance(v, dict) and k != BLOCKED_KEY}
    ok = [k for k, v in norm.items() if v.get("ok")]
    fails = {k: v for k, v in norm.items() if not v.get("ok")}
    causes: dict[str, list[str]] = {}
    for k, v in fails.items():
        for eng, e in v.get("engines", {}).items():
            if not e.get("ok"):
                c = e.get("detail", "desconocido")[:60]
                causes.setdefault(c, []).append(f"{k} [{eng}]")

    lines = [
        "# Reporte mass-validate\n",
        f"- Total evaluados: {len(norm)} · OK: {len(ok)} · Fallo: "
        f"{len(fails)} · Bloqueados excluidos: {len(blocked_list)}\n",
        f"- Tasa OK: {100 * len(ok) / max(len(norm), 1):.1f}%\n",
        "\n## Causas raíz agrupadas\n",
    ]
    for c, pkgs in sorted(causes.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- `{c}` ×{len(pkgs)}: {', '.join(pkgs[:8])}\n")
    if blocked_list:
        lines.append("\n## Bloqueados (excluidos)\n")
        for b in blocked_list:
            lines.append(f"- {b['pkg']} — {str(b.get('causa', ''))[:80]}\n")
    lines.append("\n## Detalle\n")
    for k, v in sorted(norm.items()):
        mark = "OK" if v.get("ok") else "FAIL"
        eng = "; ".join(f"{e}={'/'.join(s for s in ('prepare', 'build', 'smoke')
                                       if ev.get(s)) or 'FAIL'}"
                        for e, ev in v.get("engines", {}).items())
        extra = ""
        if not v.get("ok"):
            if v.get("crash"):
                extra = f" · crash: {v['crash'][:80]}"
            else:
                first_bad = next((e.get("detail", "desconocido")
                                  for e in v.get("engines", {}).values()
                                  if not e.get("ok")), "")
                if first_bad:
                    extra = f" · causa: {first_bad[:80]}"
        lines.append(f"- [{mark}] {k}: {eng}{extra}\n")
    md_path.write_text("".join(lines))
    print(f"\nresumen → {md_path} · datos → {md_path.with_suffix('.json')}")


if __name__ == "__main__":
    sys.exit(main())
