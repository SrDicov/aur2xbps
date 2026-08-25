#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
ARCH="${AUR2XBPS_ARCH:-$(uname -m)}"
# Fase 0 Q1 — Extracción .SRCINFO (solo .SRCINFO, no PKGBUILD eval)
# Uso: ./scripts/phase0-extract.sh spotify visual-studio-code-bin google-chrome

WS="${AUR2XBPS_ROOT:-$(cat ~/.config/aur2xbps/root 2>/dev/null || echo "$HOME/.local/share/aur2xbps")}"
CACHE_DIR="$WS/cache/aur-rpc"
SRC_BASE="$WS/sources"
REPO_AXX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for pkg in "$@"; do
  case "$pkg" in ""|*[!A-Za-z0-9._+-]*|.|..) echo "pkg inválido: $pkg" >&2; continue;; esac
  echo "=== $pkg ==="
  echo "[RPC] curl info"
  curl -sG "https://aur.archlinux.org/rpc/v5/info?arg[]=$pkg" | jq '.type, .resultcount' || true
  echo "[GIT] clone"
  if [ -d "$SRC_BASE/$pkg/.git" ]; then
    git -C "$SRC_BASE/$pkg" pull --ff-only || true
  else
    rm -rf "$SRC_BASE/$pkg"
    git clone "https://aur.archlinux.org/$pkg.git" "$SRC_BASE/$pkg"
  fi
  echo "[SRCINFO] parse split"
  python3 -c "
from src.aur.parser import parse_srcinfo_file
from src.aur.security import check_atomic_arch, validate_license
si = parse_srcinfo_file('$SRC_BASE/$pkg/.SRCINFO')
print(f\"pkgbase={si.pkgbase} subpkgs={list(si.packages.keys())}\")
for n,p in si.packages.items():
    print(f\"  {n}: ver={p.pkgver}_{p.pkgrel} arch={p.arch} deps={p.depends[:3]} src_x86_64={p.source_x86_64[:1]}\")
block, reasons = check_atomic_arch(si)
print('SECURITY:', 'BLOCK' if block else 'PASS', reasons)
print('LICENSE:', validate_license(si))
if block:
    exit(2)
"
  echo "[OK] $pkg"
done
echo "Q1 done"
