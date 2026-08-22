#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# vcs-refresh.sh — consulta git ls-remote de cada derivación -git y regenera
# el flake si el rev cambió. Uso: ./scripts/vcs-refresh.sh [--offline] [--force]
set -euo pipefail
OFFLINE=false; FORCE=false
for arg in "$@"; do
  case "$arg" in --offline) OFFLINE=true;; --force) FORCE=true;; esac
done

DERIV_BASE="${AUR2XBPS_ROOT:-$(cat ~/.config/aur2xbps/root 2>/dev/null || echo "$HOME/.local/share/aur2xbps")}/derivations"
REFS_FILE="${AUR2XBPS_ROOT:-$(cat ~/.config/aur2xbps/root 2>/dev/null || echo "$HOME/.local/share/aur2xbps")}/cache/rev-pins.json"
mkdir -p "$(dirname "$REFS_FILE")"
[ -f "$REFS_FILE" ] || echo '{}' > "$REFS_FILE"

for flake in "$DERIV_BASE"/*/flake.nix; do
  pkg=$(basename "$(dirname "$flake")")
  grep -q "pkgs.fetchgit" "$flake" || continue
  url=$(grep -oP 'url = "\K[^"]+' "$flake" | grep -v nixpkgs | head -1)
  [ -z "$url" ] && continue
  old_rev=$(grep -oP 'rev = "\K[^"]+' "$flake" | head -1)
  if $OFFLINE; then
    echo "[offline] $pkg: rev=${old_rev:0:12} (sin consultar)"
    continue
  fi
  new_rev=$(git ls-remote "$url" HEAD 2>/dev/null | awk '{print $1}') || { echo "⚠️ $pkg: ls-remote falló"; continue; }
  [ -z "$new_rev" ] && continue
  if [ "$new_rev" = "$old_rev" ] && ! $FORCE; then
    echo "✅ $pkg: sin cambios (${old_rev:0:12})"
  else
    echo "🔄 $pkg: ${old_rev:0:12} → ${new_rev:0:12} — regenerando…"
    # Re-transpilar con el nuevo rev (pin_git_rev lo detecta en generate_flake)
    python3 -c "
from src.aur.pipeline import prepare_package
from src.nix.generator import transpile, build_with_hash_fix
from pathlib import Path
import shutil
pr = prepare_package('$pkg')
out = Path('$DERIV_BASE/$pkg')
shutil.rmtree(out, ignore_errors=True)
transpile(pr.srcinfo, out)
okb, msg = build_with_hash_fix(out, '$pkg', timeout=1800)
print('build:', 'OK' if okb else msg[-200:])
"
  fi
done
echo "vcs-refresh completado"

# Modo --submodules: detectar repos con .gitmodules y regenerar con fetchSubmodules=true
if [ "${1:-}" = "--submodules" ]; then
  echo "=== Detectando submódulos en derivaciones -git ==="
  for flake in "$DERIV_BASE"/*/flake.nix; do
    pkg=$(basename "$(dirname "$flake")")
    grep -q "fetchgit" "$flake" || continue
    # Verificar si el repo upstream tiene submódulos
    url=$(grep -oP 'url = "\K[^"]+' "$flake" | grep -v nixpkgs | head -1)
    [ -z "$url" ] && continue
    has_subs=$(git ls-remote --refs "$url" "refs/heads/*" 2>/dev/null | head -1)
    # Verificar si el flake ya tiene fetchSubmodules
    if ! grep -q "fetchSubmodules" "$flake"; then
      echo "  $pkg: sin fetchSubmodules — añadiendo…"
      sed -i 's|hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";|hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";\n                    fetchSubmodules = true;|' "$flake"
    fi
  done
fi
