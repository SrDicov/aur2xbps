#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
ARCH="${AUR2XBPS_ARCH:-$(uname -m)}"
# Fase 0 Q3 — xbps-create reproducibilidad + rindex --sign
PKG="$1"
[ -n "$PKG" ] || { echo "uso: phase0-package.sh <pkg>" >&2; exit 2; }
case "$PKG" in *[!A-Za-z0-9._+-]*|""|..|.*) echo "pkg inválido: $PKG" >&2; exit 2;; esac
WS="${AUR2XBPS_ROOT:-$(cat ~/.config/aur2xbps/root 2>/dev/null || echo "$HOME/.local/share/aur2xbps")}"
RESULT="$WS/result-$PKG"
STAGE="$WS/fake-root/$PKG"
REPO="$WS/void/repo-local/$ARCH"
PRIVKEY="${AUR2XBPS_KEYDIR:-/etc/xbps/keys/aur2xbps}/privkey.pem"

SRC_BASE="$WS/sources/$PKG"

# Extraer pkgver
PKGVER=$(python3 -c "
from src.aur.parser import parse_srcinfo_file
si = parse_srcinfo_file('$SRC_BASE/.SRCINFO')
p = si.packages.get('$PKG') or list(si.packages.values())[0]
print(p.pkgver_full)
")
echo "pkgver: $PKGVER"

# Generar key si no existe
if [ ! -f "$PRIVKEY" ]; then
  echo "[SIGN] generar privkey"
  python3 -c "from src.xbps.signing import generate_keypair; from pathlib import Path; generate_keypair(Path('$PRIVKEY'))"
fi

# Stage
rm -rf "$STAGE"; mkdir -p "$STAGE"
python3 -c "
from src.xbps.builder import stage_from_nix_result
from pathlib import Path
stage_from_nix_result(Path('$RESULT'), Path('$STAGE'))
print('staged', list(Path('$STAGE').rglob('*'))[:10])
"

# Crear xbps con reproducibilidad
OUT_XBPS="$REPO/${PKGVER}.${ARCH}.xbps"
mkdir -p "$REPO"
echo "[XBPS] create $OUT_XBPS"
python3 -c "
from src.xbps.builder import create_xbps
from pathlib import Path
out = create_xbps(Path('$STAGE'), Path('$OUT_XBPS'), arch='$ARCH', pkgver='$PKGVER', desc='$PKG AUR repack via nix', dependencies='', compression='zstd')
print(f'created {out} SHA256:')
import hashlib
print(hashlib.sha256(open(out,'rb').read()).hexdigest())
"

# rindex --sign
echo "[RINDEX] sign"
python3 -c "
from src.xbps.builder import rindex_add
from pathlib import Path
rindex_add(Path('$REPO'), [Path('$OUT_XBPS')], sign=True, privkey=Path('$PRIVKEY'), signedby='aur2xbps <aur2xbps@local>')
"
ls -lh "$REPO"/*.xbps 2>&1 | head -n 20
sha256sum "$REPO"/*.xbps 2>&1 | head
echo "Q3 done"
