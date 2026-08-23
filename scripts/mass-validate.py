#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validación masiva reproducible: N paquetes `-bin` + M de fuente → build +
smoke funcional, con motor dual (xbps-src y Nix).

Objetivo (AUDIT-2026-08 / plan madurez): iterar hasta 100% — cada paquete
termina en OK o clasificado con causa raíz agrupable.

Muestreo determinista: packages.gz del AUR + random.Random(seed). Reanudable:
los ya-OK en el JSON se saltan en re-ejecuciones.

Uso:
  python3 scripts/mass-validate.py --count-bin 100 --count-src 100 --engine both
  python3 scripts/mass-validate.py --pilot          # 3+3 rápido
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
def sample_packages(n_bin: int, n_src: int, seed: int,
                    client) -> tuple[list[str], list[str]]:
    """Muestra reproducible desde el índice completo de AUR."""
    count = client.sync_packages_index()
    cur = client._conn.execute("SELECT pkgname FROM packages_index")
    names = [r[0] for r in cur.fetchall()]
    print(f"índice AUR: {count} paquetes")
    bins = sorted(n for n in names if n.endswith("-bin"))
    srcs = sorted(
        n for n in names
        if not n.endswith(("-bin", "-git", "-svn", "-hg", "-cvs", "-bzr"))
        and not n.startswith(("lib32-", "multilib-")))
    rng = random.Random(seed)
    picked_bin = sorted(rng.sample(bins, min(n_bin, len(bins))))
    picked_src = sorted(rng.sample(srcs, min(n_src, len(srcs))))
    return picked_bin, picked_src


# ---------------------------------------------------------------- runner
def run_cli(args: list[str], timeout: int) -> tuple[int, dict | None, str]:
    """Invoca aur2xbps aislado; retorna (rc, json_stdout, stderr_tail)."""
    cmd = [sys.executable, "-m", "src.cli"] + args
    env = dict(os.environ)
    env["AUR2XBPS_BUILD_TIMEOUT"] = str(timeout)
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           timeout=timeout + 120, env=env)
    except subprocess.TimeoutExpired:
        return 124, None, f"timeout global {timeout + 120}s"
    # stdout es SOLO el JSON del CLI (_StderrOnly manda el resto a stderr),
    # pero puede ser multilinea → parsear el bloque completo
    payload = _parse_json_out(r.stdout)
    if r.returncode != 0:
        tail = ((r.stderr or "") + "\n" + (r.stdout or ""))[-500:]
        return r.returncode, payload, tail
    if payload is None:
        payload = {"ok": True, "unparsed": True}
    return r.returncode, payload, (r.stderr or "")[-800:]


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


def validate_one(pkg: str, kind: str, engines: list[str],
                 timeout: int, keep_artifacts: bool) -> dict:
    """Build+smoke para UN paquete bajo cada motor. Nunca lanza."""
    out: dict = {"pkg": pkg, "kind": kind, "engines": {}}
    for engine in engines:
        stages = {"prepare": False, "build": False, "smoke": False}
        detail = ""
        t0 = time.time()
        rc, payload, tail = run_cli(["build", pkg, "--engine", engine], timeout)
        if rc != 0:
            # distinguir bloqueo de seguridad (exit 2/3) de fallo de build
            blocked = rc in (2, 3) or "Atomic" in tail or "bloquead" in tail.lower()
            detail = ("bloqueado por filtro de seguridad" if blocked
                      else tail[-300:])
        else:
            stages["prepare"] = True
            stages["build"] = bool(payload and payload.get("ok"))
            binpkgs = [Path(p) for p in (payload or {}).get("binpkgs", [])]
            if binpkgs:
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
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(prog="mass-validate")
    ap.add_argument("--count-bin", type=int, default=100)
    ap.add_argument("--count-src", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20260823)
    ap.add_argument("--engine", choices=["both", "nix", "xbps-src"],
                    default="both")
    ap.add_argument("--workers", type=int, default=6,
                    help="paralelismo de preparación RPC/clone")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("AUR2XBPS_BUILD_TIMEOUT", 3600)))
    ap.add_argument("--min-free-mb", type=int, default=1500)
    ap.add_argument("--keep-artifacts", action="store_true")
    ap.add_argument("--out", default="mass-results.json")
    ap.add_argument("--only", nargs="*", help="validar solo estos paquetes")
    args = ap.parse_args()

    from src.aur.client import AURClient
    client = AURClient(db_path=DEFAULT_DB)

    if args.only:
        picked = [(p, "bin" if p.endswith("-bin") else "src") for p in args.only]
    else:
        pb, ps = sample_packages(args.count_bin, args.count_src, args.seed, client)
        picked = [(p, "bin") for p in pb] + [(p, "src") for p in ps]

    engines = ["xbps-src", "nix"] if args.engine == "both" else [args.engine]
    if "nix" in engines and not has_nix():
        print("WARNING: nix no disponible; corrida solo-xbps-src")
        engines.remove("nix")

    results_path = Path(args.out)
    results: dict[str, dict] = {}
    if results_path.exists():
        results = json.loads(results_path.read_text())
        done_ok = {k for k, v in results.items() if v.get("ok")}
        picked = [(p, k) for p, k in picked if p not in done_ok]
        print(f"reanudando: {len(done_ok)} ya-OK saltados")

    print(f"cola: {len(picked)} paquetes × motores {engines}")
    for i, (pkg, kind) in enumerate(picked, 1):
        if free_mb() < args.min_free_mb:
            print(f"[{i}/{len(picked)}] disco bajo ({free_mb()}MB): limpieza…")
            vp = Path(os.environ.get("AUR2XBPS_VP",
                      Path.home() / ".local/share/aur2xbps/void/void-packages"))
            if vp.exists():
                subprocess.run(["./xbps-src", "clean", "ALL"], cwd=vp,
                               capture_output=True, timeout=600)
                shutil.rmtree(vp / "hostdir" / "sources", ignore_errors=True)
            if free_mb() < args.min_free_mb:
                print("  disco aún crítico: abortando para proteger el host")
                break
        print(f"[{i}/{len(picked)}] {pkg} ({kind}) …", flush=True)
        try:
            r = validate_one(pkg, kind, engines, args.timeout, args.keep_artifacts)
        except Exception as e:                                  # noqa: BLE001
            r = {"pkg": pkg, "kind": kind, "engines": {},
                 "crash": f"{type(e).__name__}: {str(e)[:200]}"}
        r["ok"] = bool(r.get("engines")) and all(
            e.get("ok") for e in r["engines"].values())
        results[pkg] = r
        ok_mark = "✅" if r.get("ok") else (
            "🚫" if any(e.get("detail", "").startswith("bloqueado")
                        for e in r.get("engines", {}).values()) else "❌")
        print(f"   {ok_mark} {json.dumps({k: v.get('ok') for k, v in
                                          r.get('engines', {}).items()})}")
        results_path.write_text(json.dumps(results, indent=2))

    _summary(results, results_path.with_suffix(".md"), engines)
    return 0


def _summary(results: dict, md_path: Path, engines: list[str]) -> None:
    ok = [k for k, v in results.items() if v.get("ok")]
    fails = {k: v for k, v in results.items() if not v.get("ok")}
    causes: dict[str, list[str]] = {}
    for k, v in fails.items():
        for eng, e in v.get("engines", {}).items():
            if not e.get("ok"):
                c = e.get("detail", "desconocido")[:60]
                causes.setdefault(c, []).append(f"{k} [{eng}]")

    lines = [
        "# Reporte mass-validate\n",
        f"- Total: {len(results)} · OK: {len(ok)} · Fallo/bloqueo: {len(fails)}\n",
        f"- Tasa OK: {100 * len(ok) / max(len(results), 1):.1f}%\n",
        "\n## Causas raíz agrupadas\n",
    ]
    for c, pkgs in sorted(causes.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- `{c}` ×{len(pkgs)}: {', '.join(pkgs[:8])}\n")
    lines.append("\n## Detalle\n")
    for k, v in sorted(results.items()):
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
