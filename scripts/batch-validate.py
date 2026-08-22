#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lote de validación end-to-end: N paquetes variados del AUR por el pipeline completo.

Cadena por paquete: prepare (RPC+filtro+clone) → transpile (flake+lock) →
build_with_hash_fix → full_pipeline XBPS (stage→patchelf→shlibs→create→firma→chroot→smoke).
Registra resultados en <workspace>/batch-results.json
(AUR2XBPS_ROOT, default ~/.local/share/aur2xbps).
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.aur.pipeline import prepare_package
from src.aur.parser import parse_srcinfo_file
from src.common.paths import BATCH_RESULTS as RESULTS, DERIVATIONS as DERIV_BASE
from src.nix.generator import transpile, build_with_hash_fix, VCSPackageError
from src.xbps.pipeline import full_pipeline

# Lote por defecto (sobrescribir con: batch-validate.py pkg1 pkg2 …)
# Formato CLI: "pkg[:bin1,bin2]" — smoke_bins separados por coma.
DEFAULT_BATCH = []


def run_one(pkg: str, smoke_bins) -> dict:
    rec = {"pkg": pkg, "stages": {}, "ok": False, "error_stage": None, "detail": ""}
    t0 = time.time()
    try:
        # 1. prepare
        pr = prepare_package(pkg)
        rec["stages"]["prepare"] = not pr.blocked and pr.srcinfo is not None
        if pr.blocked:
            rec["error_stage"] = "prepare-security"
            rec["detail"] = "; ".join(pr.errors)[:200]
            return rec
        if pr.srcinfo is None:
            rec["error_stage"] = "prepare"
            rec["detail"] = "; ".join(pr.errors)[:200]
            return rec

        # 2. transpile + lock
        out = DERIV_BASE / pkg
        if (out / "flake.nix").exists():
            (out / "flake.nix").unlink()
        try:
            transpile(pr.srcinfo, out)
            rec["stages"]["transpile"] = True
        except VCSPackageError as e:
            rec["error_stage"] = "transpile-vcs"
            rec["detail"] = str(e)[:200]
            return rec

        # 3. build nix (con auto-hash-fix)
        attr = list(pr.srcinfo.packages.keys())[0]
        ok, msg = build_with_hash_fix(out, attr, timeout=1200)
        rec["stages"]["nix-build"] = ok
        if not ok:
            rec["error_stage"] = "nix-build"
            rec["detail"] = msg[-300:]
            return rec

        # 4. pipeline XBPS completo
        p = list(pr.srcinfo.packages.values())[0]
        pkgver = f"{p.pkgname}-{p.pkgver}_{p.pkgrel}"
        bins = smoke_bins or [f"/usr/bin/{attr}", f"/bin/{attr}"]
        xr = full_pipeline(nix_result=out / "result", pkgname=attr,
                           pkgver=pkgver, desc=f"{pkg} via aur2xbps",
                           smoke_binaries=bins)
        rec["stages"]["xbps-create"] = r.xbps_path is not None if False else xr.xbps_path is not None
        rec["stages"]["sign"] = xr.signed
        rec["stages"]["chroot-install"] = xr.installed
        rec["stages"]["smoke"] = xr.smoke_ok
        rec["sha256"] = xr.sha256
        if not all(rec["stages"].values()):
            rec["error_stage"] = next(k for k, v in rec["stages"].items() if not v)
            rec["detail"] = "; ".join(xr.errors)[:200]
            return rec
        rec["ok"] = True
        rec["detail"] = f"smoke OK sha={xr.sha256[:16]}"
    except Exception as e:
        rec.setdefault("stages", {})
        rec["error_stage"] = rec.get("error_stage") or "exception"
        rec["detail"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        rec["seconds"] = round(time.time() - t0, 1)
    return rec


def main():
    # CLI: pkg1[:bin1,bin2] pkg2 … — sin argumentos no hay lote fijo
    only = sys.argv[1:] or None
    if not only:
        print("uso: batch-validate.py <pkg[:bin1,bin2]> [pkg…]  — sin lote fijo en código")
        return 0
    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    for spec in only:
        pkg, _, bins_spec = spec.partition(":")
        bins = bins_spec.split(",") if bins_spec else None
        if results.get(pkg, {}).get("ok"):
            print(f"⏭️  {pkg}: ya OK en ejecución previa")
            continue
        print(f"\n=== {pkg} ===", flush=True)
        rec = run_one(pkg, bins)
        results[pkg] = rec
        icon = "✅" if rec["ok"] else "❌"
        print(f"{icon} {pkg}: {rec['detail'][:120]} [{rec['seconds']}s]", flush=True)
        RESULTS.write_text(json.dumps(results, indent=2))

    total = len(results)
    oks = sum(1 for r in results.values() if r.get("ok"))
    print(f"\n=== RESUMEN: {oks}/{total} OK ({100 * oks // max(total,1)}%) ===")
    for pkg, r in sorted(results.items()):
        mark = "✅" if r.get("ok") else "❌"
        stage = r.get("error_stage") or "-"
        print(f"{mark} {pkg:35s} {stage:20s} {r.get('detail','')[:70]}")


if __name__ == "__main__":
    main()
