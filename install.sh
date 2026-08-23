#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# install.sh — instalador portable de aur2xbps para Void Linux.
#
#   - Verifica que el sistema sea Void (/etc/os-release ID=void)
#   - Instala dependencias con xbps-install (sin sudo si ya es root o no hace falta)
#   - Crea la estructura en $AUR2XBPS_DATA_DIR (default ~/.local/share/aur2xbps)
#   - Genera claves RSA de firma si no existen (permisos 600)
#   - Escribe ~/.config/aur2xbps/config.toml con defaults portables
#   - Instala servicio runit para servir el repo por HTTP
#
# Nix es OPCIONAL: sin Nix, aur2xbps funciona en modo solo-plantillas
# (xbps-src como motor de compilación).
set -eu

# Directorio del propio script (independiente del cwd del invocador)
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

err()  { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; }
ok()   { printf '\033[1;32mok:\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33maviso:\033[0m %s\n' "$1"; }
ask()  { printf '%s [s/N] ' "$1"; read -r ans; [ "$ans" = "s" ] || [ "$ans" = "S" ]; }

# --------------------------------------------------------------- 1. Void check
if ! grep -q '^ID=void\|^ID="void"' /etc/os-release 2>/dev/null; then
    err "este instalador es para Void Linux (ID=void en /etc/os-release)"
    exit 1
fi
ok "Void Linux detectado"

# --------------------------------------------------------------- 2. privilegios
PRIV=""
[ "$(id -u)" = "0" ] || {
    if command -v doas >/dev/null 2>&1; then PRIV="doas";
    elif command -v sudo >/dev/null 2>&1; then PRIV="sudo";
    else err "necesitas doas o sudo para instalar paquetes del sistema"; exit 1; fi
}

# --------------------------------------------------------------- 3. dependencias
# Sincronizar repodata con fallback de mirror: la imagen/instalación vieja
# puede apuntar a alpha.de (cert SSL inválido) → cambiar a repo-default.
# -y SIEMPRE: sin tty, xbps-install pregunta "continue?" y con EOF aborta
# devolviendo exit 0 (falso éxito silencioso).
xbps_sync() {
    $PRIV xbps-install -Sy "$@" >/dev/null 2>&1 && return 0
    warn "sincronización de repos falló; probando mirror repo-default.voidlinux.org…"
    mkdir -p /etc/xbps.d
    printf 'repository=https://repo-default.voidlinux.org/current\n' \
        > /etc/xbps.d/00-repository-main.conf
    $PRIV xbps-install -Sy "$@"
}

# Actualizar el propio xbps primero (imágenes antiguas tienen bugs de TLS)
if ! xbps_sync -u xbps 2>/dev/null; then
    warn "no se pudo actualizar xbps; continúando con la versión instalada"
fi

DEPS="git python3 jq bubblewrap patchelf curl zstd tar openssl file binutils xbps python3-httpx python3-yaml"
MISSING=""
for d in $DEPS; do
    xbps-query -S "$d" >/dev/null 2>&1 || MISSING="$MISSING $d"
done
if [ -n "$MISSING" ]; then
    echo "instalando dependencias:$MISSING"
    xbps_sync $MISSING || { err "xbps-install falló"; exit 1; }
fi
# xtools aporta xbps-src en Void
if ! command -v xbps-src >/dev/null 2>&1 && ! xbps-query -S xtools >/dev/null 2>&1; then
    $PRIV xbps-install -Sy xtools || warn "xtools no instalado (¿xbps-src disponible?)"
fi
ok "dependencias base"

# --------------------------------------------------------------- 4. Nix opcional
NIX_MODE=templates
if command -v nix >/dev/null 2>&1; then
    NIX_MODE=nix
    ok "nix detectado: modo hermético completo"
else
    warn "nix no está instalado: se usará xbps-src como motor (modo solo-plantillas)"
    if ask "¿instalar Nix (multiusuario) ahora?"; then
        curl -sSL https://install.determinate.systems/nix | sh -s -- install ||
            warn "instalación de nix falló; continúa en modo solo-plantillas"
        [ -x /nix/var/nix/profiles/default/bin/nix ] && \
            export PATH="/nix/var/nix/profiles/default/bin:$PATH" && NIX_MODE=nix
    fi
fi

# --------------------------------------------------------------- 5. workspace
DATA_DIR="${AUR2XBPS_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/aur2xbps}"
CACHE_DIR="${AUR2XBPS_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/aur2xbps}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/aur2xbps"
KEYS_DIR="${AUR2XBPS_KEYS_DIR:-$CONFIG_DIR/keys}"

# POSIX sh (dash) no soporta expansión de llaves {a,b}: listar rutas completas
mkdir -p "$DATA_DIR/sources" "$DATA_DIR/derivations" "$DATA_DIR/srcpkgs" \
         "$DATA_DIR/fake-root" "$DATA_DIR/void" \
         "$CACHE_DIR/aur-rpc" "$CONFIG_DIR" "$KEYS_DIR"
chmod 700 "$KEYS_DIR"
ok "estructura creada en $DATA_DIR"

# void-packages: clonar si no existe
VP="$DATA_DIR/void/void-packages"
if [ ! -d "$VP/.git" ]; then
    echo "clonando void-packages (depth 1)…"
    git clone --depth 1 https://github.com/void-linux/void-packages.git "$VP"
fi

# masterdir bootstrap si falta y xbps-src disponible
# (xbps-src rechaza root salvo XBPS_ALLOW_CHROOT_BREAKOUT=1 — caso contenedor/CI)
# xbps-src moderno nombra el masterdir por arquitectura: masterdir[-$ARCH]
ARCH="$(uname -m)"
find_masterdir() {
    for m in "$VP/masterdir-$ARCH" "$VP/masterdir"; do
        [ -d "$m/etc" ] && { printf '%s\n' "$m"; return 0; }
    done
    return 1
}
MASTERDIR_REAL=""
if ! find_masterdir >/dev/null && [ ! -d "$DATA_DIR/void/masterdir/etc" ] && [ -x "$VP/xbps-src" ]; then
    echo "bootstrap del masterdir (puede tardar)…"
    if [ "$(id -u)" = "0" ]; then
        (cd "$VP" && XBPS_ALLOW_CHROOT_BREAKOUT=1 ./xbps-src binary-bootstrap) || \
            warn "binary-bootstrap falló; ejecútalo a mano después"
    else
        (cd "$VP" && ./xbps-src binary-bootstrap) || \
            warn "binary-bootstrap falló; ejecútalo a mano después"
    fi
fi
if MASTERDIR_REAL="$(find_masterdir)"; then
    # compat: exponer también $DATA_DIR/void/masterdir como symlink al real
    [ -e "$DATA_DIR/void/masterdir" ] || ln -s "$MASTERDIR_REAL" "$DATA_DIR/void/masterdir"
fi

# --------------------------------------------------------------- 6. claves RSA
if [ ! -f "$KEYS_DIR/privkey.pem" ]; then
    (cd "$SCRIPT_DIR" && python3 - <<PY
import sys
sys.path.insert(0, "$SCRIPT_DIR")
from src.xbps.signing import generate_keypair
from pathlib import Path
generate_keypair(Path("$KEYS_DIR/privkey.pem"), Path("$KEYS_DIR/pubkey.pem"))
print("par RSA generado")
PY
    )
    chmod 600 "$KEYS_DIR/privkey.pem"
    chmod 644 "$KEYS_DIR/pubkey.pem" 2>/dev/null || true
    ok "claves de firma en $KEYS_DIR"
else
    ok "claves existentes respetadas"
fi

# --------------------------------------------------------------- 7. config.toml
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    cat > "$CONFIG_DIR/config.toml" <<EOF
# aur2xbps — configuración generada por install.sh
[paths]
data_dir = "$DATA_DIR"
cache_dir = "$CACHE_DIR"
repo_dir = "$DATA_DIR/repo"
keys_dir = "$KEYS_DIR"
masterdir = "$DATA_DIR/void/masterdir"
void_packages_dir = "$VP"

[repo]
host = "127.0.0.1"
port = 8080

[build]
log_level = "INFO"
restricted_mode = true
EOF
    ok "config escrita en $CONFIG_DIR/config.toml"
else
    ok "config.toml existente respetado"
fi

# --------------------------------------------------------------- 8. servicio runit
# Void usa runit (no systemd). Servicio de sistema si somos root con /etc/sv;
# si no, servicio de usuario bajo $USER_SERVICE_DIR (runsvdir del usuario).
PORT="${AUR2XBPS_PORT:-8080}"
SV_NAME="aur2xbps-repo"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

_write_run_script() {
    # $1 = destino del directorio del servicio
    mkdir -p "$1"
    cat > "$1/run" <<EOF
#!/bin/sh
# runit service: aur2xbps repo HTTP (sirve ${DATA_DIR}/repo)
exec env PYTHONPATH="$SCRIPT_DIR" \\
    python3 "$SCRIPT_DIR/scripts/serve-repo.py" \\
    --host "${AUR2XBPS_HOST:-127.0.0.1}" --port "$PORT" \\
    --docroot "$DATA_DIR/repo" 2>&1
EOF
    chmod +x "$1/run"
}

if [ -d /etc/runit ] && [ -w /etc/runit/runsvdir/default ] || [ "$(id -u)" = "0" ]; then
    _write_run_script "/etc/runit/runsvdir/default/$SV_NAME"
    if command -v sv >/dev/null 2>&1; then
        sv down "$SV_NAME" 2>/dev/null || true   # limpiar estado previo si existía
        sv up "$SV_NAME" 2>/dev/null && \
            ok "servicio runit activo: sv status $SV_NAME" || \
            warn "servicio creado; arranca en el próximo ciclo de runsvdir (o: sv up $SV_NAME)"
    else
        ok "servicio runit instalado en /etc/runit/runsvdir/default/$SV_NAME"
    fi
elif [ -d /etc/runit ]; then
    USER_SV="${XDG_CONFIG_HOME:-$HOME/.config}/aur2xbps/runit"
    _write_run_script "$USER_SV/$SV_NAME"
    cat > "$BIN_DIR/aur2xbps-serve-repo" <<EOF
#!/bin/sh
export PYTHONPATH="$SCRIPT_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 "$SCRIPT_DIR/scripts/serve-repo.py" "\$@"
EOF
    chmod +x "$BIN_DIR/aur2xbps-serve-repo"
    ok "servicio runit de usuario en $USER_SV/$SV_NAME"
    echo ""
    echo "  Para arrancarlo como servicio de usuario:"
    echo "    runsvdir $USER_SV &          # una vez por sesión (o añádelo a tu autostart)"
    echo "  Alternativa directa:"
    echo "    $BIN_DIR/aur2xbps-serve-repo &"
else
    warn "runit no detectado: sirve el repo manualmente con scripts/serve-repo.py"
fi

# --------------------------------------------------------------- 9. CLI en PATH
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/aur2xbps" <<EOF
#!/bin/sh
export PYTHONPATH="$SCRIPT_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m src.cli "\$@"
EOF
chmod +x "$BIN_DIR/aur2xbps"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "añade $BIN_DIR a tu PATH para usar 'aur2xbps'" ;;
esac
ok "CLI instalado: aur2xbps (query|resolve|template|build|repo)"

echo ""
echo "════════════════════════════════════════════════════"
echo " Instalación completa — modo: $NIX_MODE"
echo "════════════════════════════════════════════════════"
echo " Uso básico:"
echo "   aur2xbps template <pkg>   # genera plantilla xbps-src"
echo "   aur2xbps build <pkg>      # compila ($NIX_MODE)"
echo "   vouru install <pkg>       # flujo vouru (usa las plantillas)"
echo " Repo HTTP:  sv status aur2xbps-repo   (runit, puerto $PORT)"
echo "════════════════════════════════════════════════════"
