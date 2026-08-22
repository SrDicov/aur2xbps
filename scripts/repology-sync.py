#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sincroniza mapeo Arch→Nix desde Repology.

Descarga proyectos que existen en ambos repos (Arch ↔ NixOS) vía la API v1
y genera src/common/arch_to_nix_repology.json consumido por generator.py
como capa intermedia: tabla manual → repology → nombre directo.

Uso:
  python3 scripts/repology-sync.py [--limit N] [--out RUTA]

API: https://repology.org/api/v1/project/<name>/problems  (por proyecto)
Mejor enfoque masivo: dump https://repology.org/api/v1/repository/arch/projects
(no paginado completo; se consulta por lotes de nombres candidatos).
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

import httpx

from src.common.paths import REPO_ROOT, SOURCES

OUT_DEFAULT = REPO_ROOT / "src" / "common" / "arch_to_nix_repology.json"
API = "https://repology.org/api/v1"

HEADERS = {"User-Agent": "aur2xbps-repology-sync/1.0 (uso interno privado)"}


def fetch_project(name: str, client: httpx.Client) -> dict | None:
    r = client.get(f"{API}/project/{name}/packages", params={
        "repos": "arch_linux,nixos",
    })
    if r.status_code == 404:
        return None
    r.raise_for_status()
    pkgs = r.json()
    arch = nix = None
    for p in pkgs:
        repo = p.get("repo", "")
        if repo == "arch" and arch is None:
            arch = p.get("visiblename") or p.get("srcname")
        if repo == "nixos" and nix is None:
            nix = (p.get("visiblename") or "")
            # visiblename en nixos suele ser "python3Packages.foo" etc.
    if arch and nix:
        return {arch: nix}
    return None


def offline_oracle(candidates: list[str]) -> dict[str, str]:
    """Oráculo offline: valida qué nombres Arch existen literalmente en nixpkgs
    (import del flake.lock pinneado). Registra los que existen como attr directo."""
    import subprocess
    known = {}
    B = 60
    for i in range(0, len(candidates), B):
        chunk = candidates[i:i + B]
        expr = ('let p = import (builtins.getFlake "nixpkgs").outPath '
                '{ config.allowUnfree = true; }; in builtins.toJSON {')
        expr += " ".join(f'"{a}" = builtins.hasAttr "{a}" p;' for a in chunk)
        expr += " }"
        r = subprocess.run(
            ["nix", "eval", "--impure", "--json", "--extra-experimental-features",
             "nix-command flakes", "--expr", expr],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            print("oracle chunk falló:", r.stderr[-200:])
            continue
        d = json.loads(json.loads(r.stdout))
        for a, exists in d.items():
            if exists:
                known[a] = a
    return known


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="máx. nombres a consultar esta ejecución")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--names", nargs="*", help="nombres Arch concretos a consultar")
    args = ap.parse_args()

    out_path = Path(args.out)
    table = json.loads(out_path.read_text()) if out_path.exists() else {}

    # Nombres candidatos: los que el generador no mapea hoy (fallback raw).
    # Fuente práctica: índice AUR local → depends más frecuentes no presentes
    # en ARCH_TO_NIX. Para simplificar, aceptamos --names o derivamos del lote.
    if args.names:
        candidates = args.names
    else:
        from src.common.types import SrcInfoPackage
        # recolectar depends de todos los .SRCINFO conocidos
        from src.aur.parser import parse_srcinfo_file
        names: set[str] = set()
        for f in SOURCES.glob("*/.SRCINFO"):
            try:
                si = parse_srcinfo_file(f)
                for pkg in si.packages.values():
                    for d in pkg.depends_for() + pkg.makedepends_for():
                        names.add(d.split(">")[0].split("=")[0].split("<")[0].strip())
            except Exception:
                continue
        from src.nix.generator import ARCH_TO_NIX, _map_one
        candidates = sorted(n for n in names
                            if not n.startswith("lib32-")
                            and _map_one(n) == n  # sin mapeo especial
                            and n not in ARCH_TO_NIX)[: args.limit]

    added = 0
    with httpx.Client(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        for i, name in enumerate(candidates):
            try:
                got = fetch_project(name, client)
            except httpx.HTTPStatusError as e:
                print(f"[{i+1}/{len(candidates)}] {name}: HTTP {e.response.status_code}")
                time.sleep(2)
                continue
            except Exception as e:
                print(f"[{i+1}/{len(candidates)}] {name}: {type(e).__name__}")
                time.sleep(2)
                continue
            if got:
                k, v = next(iter(got.items()))
                if k != v:
                    table[k] = v
                    added += 1
                    print(f"✅ {k} → {v}")
            time.sleep(0.7)  # cortesía rate-limit repology (~10 req/min recomendado)

    if added == 0:
        # Degradación elegante: Repology restringe acceso automatizado (403/404).
        # Fallback: oráculo nixpkgs offline → registra attrs válidos directos.
        print("Repology no accesible; usando oráculo nixpkgs offline…")
        table.update(offline_oracle(candidates))
        added = sum(1 for k, v in table.items() if k == v)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, indent=2, sort_keys=True))
    print(f"\ntabla actualizada: +{added} entradas (total {len(table)}) → {out_path}")


if __name__ == "__main__":
    main()
