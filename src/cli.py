# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI de aur2xbps — interfaz estable para humanos y helpers (vouru).

Comandos:
  aur2xbps query <pkg>            Metadatos AUR en JSON
  aur2xbps resolve <pkg>          Dependencias mapeadas a Void (JSON)
  aur2xbps template <pkg>         Genera srcpkgs/<pkg>/template (xbps-src puro)
  aur2xbps build <pkg>            Compila: Nix si está; si no, xbps-src
  aur2xbps repo [--sign]          Crea/actualiza repo local firmado

Variables de entorno que los helpers deben respetar:
  AUR2XBPS_CONFIG, AUR2XBPS_DATA_DIR, AUR2XBPS_REPO_DIR, AUR2XBPS_KEYS_DIR,
  AUR2XBPS_OFFLINE, AUR2XBPS_ARCH  (ver src/common/config.py)

Salida de máquina siempre en stdout; logs/warnings por stderr.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from src.common.config import get_config
from src.common.tools import has_nix


class BuildError(Exception):
    def __init__(self, errors):
        self.errors = errors or ["desconocido"]
        super().__init__(self.errors[0])


def _prepare(pkgname: str):
    """RPC → clone → filtro seguridad. Retorna PrepareResult."""
    from src.aur.pipeline import prepare_package
    return prepare_package(pkgname)


def _die(msg: str, code: int = 1) -> None:
    print(f"aur2xbps: error: {msg}", file=sys.stderr)
    sys.exit(code)


def _emit(payload: dict) -> None:
    """JSON de máquina por stdout (limpio: todo el progreso va a stderr)."""
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


class _StderrOnly:
    """Contexto: redirige el fd de stdout hacia stderr durante el comando,
    de modo que TODO progreso (prints de Python Y procesos hijos como
    xbps-src/nix/git) salga por stderr y el stdout quede reservado al JSON."""

    def __enter__(self):
        import os
        self._os = os
        sys.stdout.flush()
        self._saved_fd = os.dup(1)
        os.dup2(2, 1)                 # fd1 → stderr
        self._saved_stdout = sys.stdout
        sys.stdout = sys.stderr
        return self

    def __exit__(self, *exc):
        sys.stdout.flush()
        self._os.dup2(self._saved_fd, 1)
        self._os.close(self._saved_fd)
        sys.stdout = self._saved_stdout
        return False


# ------------------------------------------------------------------ comandos

def cmd_query(args) -> int:
    from src.aur.client import AURClient
    cfg = get_config()
    client = AURClient(db_path=cfg.rpc_cache_db, offline=cfg.offline)
    results = client.info([args.pkg])
    if not results:
        _die(f"'{args.pkg}' no encontrado en el AUR", 2)
    r = results[0]
    out = {
        "name": r.get("Name"),
        "version": f"{r.get('Version')}",
        "description": r.get("Description"),
        "url": r.get("URL"),
        "license": r.get("License", []),
        "depends": r.get("Depends", []),
        "makedepends": r.get("MakeDepends", []),
        "optdepends": r.get("OptDepends", []),
        "checkdepends": r.get("CheckDepends", []),
        "conflicts": r.get("Conflicts", []),
        "provides": r.get("Provides", []),
        "package_base": r.get("PackageBase"),
        "last_modified": r.get("LastModified"),
        "out_of_date": r.get("OutOfDate"),
        "num_votes": r.get("NumVotes"),
        "popularity": r.get("Popularity"),
    }
    if args.sources:
        from src.aur.pipeline import prepare_package
        pr = _prepare(args.pkg)
        if pr.srcinfo:
            pkg = next(iter(pr.srcinfo.packages.values()))
            arch = cfg.arch
            out["sources"] = pkg.sources_for(arch) or pkg.sources_for("")
            out["sha256sums"] = (pkg.sums_for("sha256", arch)
                                 or pkg.sums_for("sha256", "") or [])
    return out


def cmd_resolve(args) -> int:
    pr = _prepare(args.pkg)
    if pr.blocked:
        _die(f"'{args.pkg}' bloqueado por el filtro de seguridad: {pr.errors}", 3)
    if not pr.srcinfo:
        _die(f"no se pudo obtener .SRCINFO de '{args.pkg}': {pr.errors}", 2)
    from src.void.mapping import map_dep
    runtime: list[str] = []
    buildtime: list[str] = []
    for pkg in pr.srcinfo.packages.values():
        for d in pkg.depends_for(get_config().arch) + pkg.depends_for(""):
            m = map_dep(d)
            if m and m not in runtime:
                runtime.append(m)
        for d in pkg.makedepends_for(get_config().arch) + pkg.makedepends_for(""):
            m = map_dep(d)
            if m and m not in buildtime:
                buildtime.append(m)
    return {"package": args.pkg,
            "arch": get_config().arch,
            "depends": sorted(runtime),
            "makedepends": sorted(buildtime)}


def cmd_template(args) -> int:
    pr = _prepare(args.pkg)
    if pr.blocked:
        _die(f"'{args.pkg}' bloqueado por el filtro de seguridad: {pr.errors}", 3)
    if not pr.srcinfo:
        _die(f"no se pudo obtener .SRCINFO de '{args.pkg}': {pr.errors}", 2)
    from src.void.template import generate_template, sync_to_void_srcpkgs
    cfg = get_config()
    out_dir = Path(args.out).expanduser() if args.out else cfg.srcpkgs_dir
    results = generate_template(pr.srcinfo, out_dir)
    copied = 0
    if not args.no_sync and cfg.void_packages_dir.is_dir():
        try:
            copied = sync_to_void_srcpkgs(out_dir)
        except Exception as e:
            print(f"aur2xbps: aviso: sync a srcpkgs falló: {e}", file=sys.stderr)
    payload = {
        "templates": [
            {"pkgname": r.pkgname, "version": r.version,
             "template": str(r.template_path),
             "restricted": r.restricted, "warnings": r.warnings}
            for r in results],
        "synced_to_srcpkgs": copied,
        "build_hint": (
            f"cd {cfg.void_packages_dir} && ./xbps-src pkg "
            f"{results[0].pkgname}") if results else None,
    }
    return payload


def cmd_build(args) -> int:
    cfg = get_config()
    engine = args.engine
    if engine == "auto":
        engine = "nix" if has_nix() else "xbps-src"
    if engine == "nix":
        return _build_with_nix(args.pkg)
    return _build_with_xbps_src(args.pkg)


def _build_with_xbps_src(pkgname: str) -> int:
    """Genera plantilla y compila con xbps-src (sin Nix)."""
    import os as _os
    cfg = get_config()
    vp = cfg.void_packages_dir
    if not (vp / "xbps-src").is_file():
        _die(f"no existe {vp}/xbps-src; clona void-packages o ejecuta install.sh", 4)
    # 1. generar plantilla directamente (sin tocar stdout)
    from src.void.template import generate_template
    pr = _prepare(pkgname)
    if pr.blocked or not pr.srcinfo:
        _die(f"no se pudo preparar '{pkgname}': {pr.errors}", 2)
    gen_dir = vp / "_aur2xbps-srcpkgs"
    results = generate_template(pr.srcinfo, gen_dir)
    if not results:
        _die(f"sin plantillas generadas para '{pkgname}'", 2)
    pkgname = results[0].pkgname
    # copiar al srcpkgs oficial
    import shutil as _sh
    for tdir in gen_dir.iterdir():
        dst = vp / "srcpkgs" / tdir.name
        if dst.exists():
            _sh.rmtree(dst)
        _sh.copytree(tdir, dst)
    # 2. bootstrap si hace falta
    # xbps-src NO puede correr como root salvo XBPS_ALLOW_CHROOT_BREAKOUT (CI).
    # Como no-root usa xbps-uchroot (sudo interno) — correr como dueño del árbol.
    owner = vp.stat().st_uid
    if _os.getuid() == 0:
        priv = ["env", "XBPS_ALLOW_CHROOT_BREAKOUT=1"]
    else:
        priv = []
    if not (cfg.masterdir / "etc").is_dir():
        print("[build] binary-bootstrap…", file=sys.stderr)
        subprocess.run(priv + ["./xbps-src", "binary-bootstrap"], cwd=vp, check=True)
    # 3. limpiar estado stale: dobuild.sh toca el stamp *_build_done ANTES de
    #    instalar, así que un fallo posterior (pkglint) lo deja puesto y TODOS
    #    los retries siguientes se saltan fetch/extract/install en silencio
    #    (incluso con -f, que solo re-ejecuta el target 'build').
    subprocess.run(priv + ["./xbps-src", "clean", pkgname], cwd=vp,
                   capture_output=True)
    # 4. compilar (-f fuerza re-ejecución de fases: sin esto, stamps
    #    *_install_done de intentos previos saltan do_install silenciosamente)
    r = subprocess.run(
        priv + ["./xbps-src", "-f", "-A", cfg.arch, "pkg", pkgname], cwd=vp)
    if r.returncode != 0:
        _die(f"xbps-src pkg {pkgname} falló ({r.returncode})", r.returncode or 5)
    binpkgs = vp / "hostdir" / "binpkgs"
    found = sorted(binpkgs.glob(f"{pkgname}-*.xbps"))
    return {"engine": "xbps-src", "ok": True,
            "binpkgs": [str(p) for p in found],
            "repository": str(binpkgs)}


def _build_with_nix(pkgname: str) -> int:
    """Flujo completo hermético: transpile → nix build → XBPS firmado."""
    from src.nix.generator import build_with_hash_fix, transpile
    from src.xbps.pipeline import full_pipeline
    pr = _prepare(pkgname)
    if pr.blocked:
        _die(f"'{pkgname}' bloqueado por el filtro de seguridad: {pr.errors}", 3)
    if not pr.srcinfo:
        _die(f"no se pudo obtener .SRCINFO de '{pkgname}'", 2)
    cfg = get_config()
    out_dir = cfg.derivations_dir / pkgname
    transpile(pr.srcinfo, out_dir)
    ok, msg = build_with_hash_fix(out_dir, pkgname)
    if not ok:
        _die(f"nix build falló: {msg[:400]}", 6)
    pkg0 = next(iter(pr.srcinfo.packages.values()))
    pkgver = f"{pkgname}-{pkg0.pkgver}_{pkg0.pkgrel}"
    res = full_pipeline(out_dir / "result", pkgname, pkgver,
                        desc=(pkg0.pkgdesc or pkgname)[:80])
    if not (res.smoke_ok and res.installed):
        raise BuildError(res.errors)
    return {"engine": "nix", "ok": True,
            "xbps": str(res.xbps_path or ""),
            "sha256": res.sha256 or ""}


class _Namespace:
    """Mínimo para reutilizar cmd_template desde _build_with_xbps_src."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def cmd_repo(args) -> int:
    from src.xbps.builder import rindex_add
    cfg = get_config()
    cfg.repo_dir.mkdir(parents=True, exist_ok=True)
    cfg.repo_x86_64.mkdir(parents=True, exist_ok=True)
    files = sorted(cfg.repo_x86_64.glob("*.xbps"))
    privkey = cfg.privkey
    sign = args.sign and privkey.is_file()
    if args.sign and not privkey.is_file():
        print("aur2xbps: aviso: sin clave privada en",
              privkey, "; repo SIN firma", file=sys.stderr)
    rindex_add(cfg.repo_x86_64, files, sign=bool(sign),
               privkey=privkey if sign else None)
    return {"repo": str(cfg.repo_x86_64), "packages": len(files),
            "signed": bool(sign)}


# ------------------------------------------------------------------ parser

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="aur2xbps",
        description="AUR (Arch) → Void Linux (XBPS): consulta, plantillas xbps-src y empaquetado determinista")
    ap.add_argument("--version", action="version", version="aur2xbps 0.1.0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query", help="metadatos AUR en JSON")
    q.add_argument("pkg")
    q.add_argument("--sources", action="store_true",
                   help="incluye fuentes y hashes (.SRCINFO)")
    q.set_defaults(func=cmd_query)

    r = sub.add_parser("resolve", help="dependencias mapeadas a Void (JSON)")
    r.add_argument("pkg")
    r.set_defaults(func=cmd_resolve)

    t = sub.add_parser("template", help="genera plantilla xbps-src para vouru")
    t.add_argument("pkg")
    t.add_argument("--out", help="directorio destino (default <data>/srcpkgs)")
    t.add_argument("--no-sync", action="store_true",
                   help="no copiar a <void-packages>/srcpkgs")
    t.set_defaults(func=cmd_template)

    b = sub.add_parser("build", help="compila (Nix si está; si no xbps-src)")
    b.add_argument("pkg")
    b.add_argument("--engine", choices=["auto", "nix", "xbps-src"], default="auto")
    b.set_defaults(func=cmd_build)

    rp = sub.add_parser("repo", help="indexa/firma repositorio local")
    rp.add_argument("--sign", action="store_true", help="firma con la clave configurada")
    rp.set_defaults(func=cmd_repo)

    args = ap.parse_args(argv)
    try:
        with _StderrOnly():
            payload = args.func(args)
        if isinstance(payload, dict):
            _emit(payload)
        return 0
    except BuildError as e:
        _die(f"build falló: {'; '.join(e.errors)[:300]}", 7)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
