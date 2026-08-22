#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
ARCH="${AUR2XBPS_ARCH:-$(uname -m)}"
PKG="$1"
WS="${AUR2XBPS_ROOT:-$(cat ~/.config/aur2xbps/root 2>/dev/null || echo "$HOME/.local/share/aur2xbps")}"
REPO="$WS/void/repo-local"
MASTERDIR="$WS/void/masterdir"

# Bootstrap check
if [ ! -d "$MASTERDIR/etc" ]; then
  echo "[BOOTSTRAP] xbps-src binary-bootstrap"
  cd "$WS/void/void-packages"
  ./xbps-src binary-bootstrap || true
fi

echo "[VERIFY] dry-run"
xbps-uchroot "$MASTERDIR" -- sh -c "
  set -e
  xbps-install -R /host$WS/void/repo-local -S 2>&1 | head -n 20 || true
  xbps-install -R /host$WS/void/repo-local -u -d --dry-run $PKG 2>&1 | tee /tmp/dry.log
  if grep -q 'UNKNOWN PKG' /tmp/dry.log; then echo 'FAIL UNKNOWN PKG'; exit 1; fi
  echo 'TOPOLOGY OK'
"

echo "[INSTALL] real"
xbps-uchroot "$MASTERDIR" -- sh -c "
  set -e
  xbps-install -R /host$WS/void/repo-local -y $PKG 2>&1 | tail -n 30
  xbps-query -l | grep $PKG || true
  xbps-query -p state $PKG || true
  $PKG --version 2>&1 | head -n 5 || $PKG --help 2>&1 | head -n 5 || ls /usr/bin/$PKG* 2>&1 | head -n 20
  echo \"exit \$?\"
  ldd /usr/bin/$PKG 2>&1 | head -n 20 || ldd /usr/bin/${PKG//-/_} 2>&1 | head -n 20 || true
"

echo "[EEL] smoke done"
