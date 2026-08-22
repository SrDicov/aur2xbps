#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# export-key.sh — exporta la clave pública del repo firmado
# Uso: ./scripts/export-key.sh [destino]
set -euo pipefail
KEY="${AUR2XBPS_KEYDIR:-/etc/xbps/keys/aur2xbps}/pubkey.pem"
WS="${AUR2XBPS_ROOT:-$(cat ~/.config/aur2xbps/root 2>/dev/null || echo "$HOME/.local/share/aur2xbps")}"
DEST="${1:-$WS/repo-void/pubkey.pem}"
if [ ! -f "$KEY" ]; then echo "❌ $KEY no encontrado"; exit 1; fi
mkdir -p "$(dirname "$DEST")"
cp "$KEY" "$DEST"
echo "clave pública copiada a: $DEST"
echo "en el equipo Void destino:"
echo "  sudo mkdir -p /var/db/xbps/keys"
echo "  # el fingerprint se muestra al hacer xbps-install -S --repository=..."
