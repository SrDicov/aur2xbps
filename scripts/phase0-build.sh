#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
ARCH="${AUR2XBPS_ARCH:-$(uname -m)}"
# Fase 0 Q2 — Derivación flakes + patchelf
# Uso: ./scripts/phase0-build.sh spotify

PKG="$1"
WS="${AUR2XBPS_ROOT:-$(cat ~/.config/aur2xbps/root 2>/dev/null || echo "$HOME/.local/share/aur2xbps")}"
SRC_BASE="$WS/sources/$PKG"
OUT_BASE="$WS/derivations/$PKG"
RESULT_LINK="$WS/result-$PKG"

echo "=== generar flake para $PKG ==="
python3 -c "
from src.aur.parser import parse_srcinfo_file
from src.nix.generator import generate_flake_for_srcinfo
from pathlib import Path
si = parse_srcinfo_file('$SRC_BASE/.SRCINFO')
flake = generate_flake_for_srcinfo(si, Path('$OUT_BASE'))
print(f'flake: {flake}')
print(flake.read_text()[:3000])
"

echo "[LINT] patchelf fixupPhase"
python3 -c "
from src.nix.patchelf import lint_fixupPhase
from pathlib import Path
txt = Path('$OUT_BASE/flake.nix').read_text()
errs = lint_fixupPhase(txt)
if errs:
    print('LINT FAIL')
    print('\n'.join(errs))
    exit(1)
print('LINT PASS')
"

echo "[BUILD] nix build .#${PKG//-/_}-drv --option sandbox true"
# flake usa nombres con _ por Nix; mapear
cd "$OUT_BASE"
# generar flake.lock
nix flake lock --extra-experimental-features "nix-command flakes" 2>&1 | head -n 20 || true
# Intento build dry-run primero
nix build ".#${PKG//-/_}-drv" --dry-run --extra-experimental-features "nix-command flakes" --option sandbox true 2>&1 | head -n 100 || true
# Build real con SOURCE_DATE_EPOCH=0
SOURCE_DATE_EPOCH=0 nix build ".#${PKG//-/_}-drv" --out-link "$RESULT_LINK" --extra-experimental-features "nix-command flakes" --option sandbox true 2>&1 | tail -n 100
ls -lh "$RESULT_LINK" 2>&1 | head
ls -lh "$RESULT_LINK/usr" 2>&1 | head -n 20 || ls -lh "$RESULT_LINK" 2>&1 | head -n 20

echo "[VALIDATE] readelf + ldd"
for f in $(find "$RESULT_LINK" -type f -executable -exec sh -c 'file "$1" | grep -q ELF && echo "$1"' _ {} \; | head -n 5); do
  echo "--- $f ---"
  readelf -d "$f" | grep -E 'RPATH|RUNPATH|NEEDED' || true
  ldd "$f" | head -n 20 || true
done
echo "Q2 done $PKG"
