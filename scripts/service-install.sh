#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Servicio del repo aur2xbps en el supervisor disponible:
#   runit (sistema/usuario) | dinit (sistema/usuario).  NUNCA systemd.
#
# Uso:   ./scripts/service-install.sh [--uninstall]
# Env:   AUR2XBPS_HOST/PORT/ROOT, AUR2XBPS_FORCE_INIT=runit|dinit (tests)
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SV_NAME="aur2xbps-repo"
PORT="${AUR2XBPS_PORT:-8080}"
HOST="${AUR2XBPS_HOST:-127.0.0.1}"
DATA_DIR="${AUR2XBPS_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/aur2xbps}"
BIN_DIR="$HOME/.local/bin"
WRAPPER="$BIN_DIR/aur2xbps-serve-repo"
USER_SV_RUNIT="${XDG_CONFIG_HOME:-$HOME/.config}/aur2xbps/runit"
USER_DINIT="${XDG_CONFIG_HOME:-$HOME/.config}/dinit.d"

msg()  { echo "[servicio] $*"; }
warn() { echo "[servicio] ⚠️  $*" >&2; }
is_root() { [ "$(id -u)" = "0" ]; }

pid1() { [ -r /proc/1/comm ] && cut -d/ -f1 /proc/1/comm 2>/dev/null || true; }

write_wrapper() {
    mkdir -p "$BIN_DIR"
    cat > "$WRAPPER" <<EOF
#!/bin/sh
export PYTHONPATH="$SCRIPT_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 "$SCRIPT_DIR/scripts/serve-repo.py" "\$@"
EOF
    chmod +x "$WRAPPER"
}

install_runit_system() {
    write_wrapper
    mkdir -p "/etc/runit/runsvdir/default/$SV_NAME"
    cat > "/etc/runit/runsvdir/default/$SV_NAME/run" <<EOF
#!/bin/sh
exec $WRAPPER 2>&1
EOF
    chmod +x "/etc/runit/runsvdir/default/$SV_NAME/run"
    if command -v sv >/dev/null 2>&1; then
        sv down "$SV_NAME" 2>/dev/null || true
        sv up "$SV_NAME" 2>/dev/null \
            && msg "runit sistema activo: sv status $SV_NAME" \
            || warn "creado; arrancará en el próximo ciclo de runsvdir (o: sv up $SV_NAME)"
    else
        msg "instalado en /etc/runit/runsvdir/default/$SV_NAME"
    fi
}

install_runit_user() {
    write_wrapper
    mkdir -p "$USER_SV_RUNIT/$SV_NAME"
    cat > "$USER_SV_RUNIT/$SV_NAME/run" <<EOF
#!/bin/sh
exec $WRAPPER 2>&1
EOF
    chmod +x "$USER_SV_RUNIT/$SV_NAME/run"
    msg "runit de usuario en $USER_SV_RUNIT/$SV_NAME"
    echo "  Arranque: runsvdir $USER_SV_RUNIT &      (una vez por sesión)"
    echo "  Directo : $WRAPPER &"
}

install_dinit_system() {
    write_wrapper
    mkdir -p /etc/dinit.d
    cat > "/etc/dinit.d/$SV_NAME" <<EOF
# dinit: aur2xbps repo HTTP (sistema)
type = process
command = $WRAPPER
restart = false
logfile = /var/log/aur2xbps-repo.log
EOF
    if command -v dinitctl >/dev/null 2>&1; then
        dinitctl start "$SV_NAME" 2>/dev/null \
            && msg "dinit sistema activo: dinitctl status $SV_NAME" \
            || warn "creado; arranca con: dinitctl start $SV_NAME"
    else
        msg "creado /etc/dinit.d/$SV_NAME"
    fi
}

install_dinit_user() {
    write_wrapper
    mkdir -p "$USER_DINIT"
    cat > "$USER_DINIT/$SV_NAME" <<EOF
# dinit: aur2xbps repo HTTP (usuario)
type = process
command = $WRAPPER
restart = false
logfile = $DATA_DIR/repo-service.log
EOF
    msg "dinit de usuario en $USER_DINIT/$SV_NAME"
    echo "  Arranque (sesión dinit): dinitctl --user start $SV_NAME"
    echo "  Directo : $WRAPPER &"
}

uninstall_all() {
    rm -rf "/etc/runit/runsvdir/default/$SV_NAME" 2>/dev/null || true
    rm -rf "$USER_SV_RUNIT/$SV_NAME" 2>/dev/null || true
    rm -f  "/etc/dinit.d/$SV_NAME" 2>/dev/null || true
    rm -f  "$USER_DINIT/$SV_NAME" 2>/dev/null || true
    command -v sv >/dev/null 2>&1 && sv down "$SV_NAME" 2>/dev/null || true
    command -v dinitctl >/dev/null 2>&1 && dinitctl stop "$SV_NAME" 2>/dev/null || true
    msg "servicios eliminados (runit+dinit, sistema y usuario)"
}

case "${1:-}" in
    --uninstall) uninstall_all; exit 0 ;;
    "") ;;
    *) echo "uso: service-install.sh [--uninstall]" >&2; exit 2 ;;
esac

INIT="${AUR2XBPS_FORCE_INIT:-$(pid1)}"
case "$INIT" in
    runit)
        if is_root && [ -d /etc/runit/runsvdir/default ]; then
            install_runit_system
        else
            # forzado explícito (o PID1=runit): escribir dotfiles de usuario
            # es inofensivo aunque runit aún no esté instalado
            install_runit_user
        fi
        ;;
    dinit)
        if is_root && [ -w /etc/dinit.d ]; then
            install_dinit_system
        else
            # escribir el unit de usuario es inofensivo aunque dinit no
            # esté aún instalado (lo recogerá la sesión al existir)
            install_dinit_user
        fi
        ;;
    *)
        # PID1 desconocido: heurística por directorios (runit primero: default Void)
        if [ -d /etc/runit ]; then
            if is_root && [ -w /etc/runit/runsvdir/default ]; then
                install_runit_system
            else
                install_runit_user
            fi
        elif [ -d /etc/dinit.d ] || command -v dinitctl >/dev/null 2>&1; then
            if is_root && [ -w /etc/dinit.d ]; then
                install_dinit_system
            else
                install_dinit_user
            fi
        else
            warn "ni runit ni dinit detectados: sirve el repo manualmente"
            echo "  $SCRIPT_DIR/scripts/serve-repo.py --host $HOST --port $PORT --docroot $DATA_DIR/repo"
        fi
        ;;
esac
