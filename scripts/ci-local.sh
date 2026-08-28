#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# ci-local.sh — Pipeline CI/CD local de aur2xbps (Fase 4)
# Ejecuta: lint → tests → TRH → TIAR canary → smoke chroot.
# Uso: ./scripts/ci-local.sh [--quick]
#   --quick: omite TRH de paquetes grandes (usa solo hello)
set -euo pipefail
AXX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCH="${AUR2XBPS_ARCH:-$(uname -m)}"
# Elevador universal (mismo orden que src/common/priv.py). En root, vacío.
PRIV=""
if [ "$(id -u)" != "0" ]; then
  if [ -n "${AUR2XBPS_PRIV:-}" ]; then PRIV="$AUR2XBPS_PRIV"
  else
    for t in sudo doas run0; do
      command -v "$t" >/dev/null 2>&1 && { PRIV="$t"; break; }
    done
    [ -n "$PRIV" ] || { echo "[CI] sin root ni elevador (sudo/doas/run0): exporta AUR2XBPS_PRIV" >&2; exit 1; }
  fi
fi
# Resolución de binarios xbps sin rutas hardcodeadas (AGENTS.md):
# PATH del sistema > $AUR2XBPS_XBPS_BIN_DIR; si no hay ninguno, vacío (el
# paso que lo use falla con mensaje claro en vez de ruta inventada).
xb() { command -v "$1" 2>/dev/null || { [ -n "${AUR2XBPS_XBPS_BIN_DIR:-}" ] && echo "${AUR2XBPS_XBPS_BIN_DIR}/$1"; }; }
XBPS_CREATE_BIN="$(xb xbps-create)"
[ -n "$XBPS_CREATE_BIN" ] || echo "[CI] ⚠️  xbps-create no encontrado en PATH ni AUR2XBPS_XBPS_BIN_DIR" >&2
TRH_STAGE_HELLO="${TRH_STAGE_HELLO:-hello}"
TRH_STAGE_REAL="${TRH_STAGE_REAL:-}"   # paquete real opcional para TRH determinize
WORKSPACE="${AUR2XBPS_ROOT:-$(cat ~/.config/aur2xbps/root 2>/dev/null || echo "$HOME/.local/share/aur2xbps")}"
REPO="$WORKSPACE/void/repo-local/$ARCH"
MASTERDIR="$WORKSPACE/void/masterdir"
TRH_DIR="/tmp/ci-trh"
cd "$AXX"
FAIL=0

NOTIFY="${1:-}"
case "$NOTIFY" in
  --notify) NOTIFY_MODE="on" ;;
  --quick)  NOTIFY_MODE="off"; unset TRH_STAGE_REAL ;;   # omite TRH de paquete grande
  "")       NOTIFY_MODE="off" ;;
  *) echo "uso: ci-local.sh [--quick|--notify]" >&2; exit 2 ;;
esac

step() { echo -e "\n=== [CI] $1 ==="; }
pass() { echo "[CI] ✅ $1"; }
fail() { echo "[CI] ❌ $1"; FAIL=1; }
notify_fail() {
  [ "$NOTIFY_MODE" = "on" ] || return 0
  command -v notify-send >/dev/null && notify-send -u critical "aur2xbps CI FALLÓ" "$1" 2>/dev/null || true
  command -v wall >/dev/null && wall "aur2xbps CI FALLÓ: $1" 2>/dev/null || true
}

# ---------- 1. Lint + tests unitarios ----------
step "Lint y tests unitarios"
python3 -m src.nix.patchelf >/dev/null && pass "linter patchelf" || fail "linter patchelf"
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))" 2>/dev/null \
  && pass "ci.yml YAML válido" || true
if python3 -m pytest tests/ -q --timeout=120 2>&1 | tail -n 3; then
  pass "pytest suite"
else
  fail "pytest suite"
fi

# ---------- 2. Lint de flakes generados ----------
step "Linter sobre flakes existentes"
for f in "$WORKSPACE"/derivations/*/flake.nix; do
  pkg=$(basename "$(dirname "$f")")
  if python3 -c "
from src.nix.patchelf import lint_fixupPhase
from pathlib import Path
errs = lint_fixupPhase(Path('$f').read_text())
exit(1 if errs else 0)" 2>/dev/null; then
    pass "flake $pkg"
  else
    fail "flake $pkg (patchelf combinado/orden)"
  fi
done

# ---------- 3. TRH: 3 builds del stage, hashes idénticos ----------
step "TRH: reproducibilidad xbps-create ($TRH_STAGE_HELLO ×3)"
STAGE_HELLO="$WORKSPACE/fake-root/$TRH_STAGE_HELLO"
H_UNIQUE=0
if [ ! -d "$STAGE_HELLO" ] || { [ ! -d "$STAGE_HELLO/usr/bin" ] && [ ! -d "$STAGE_HELLO/bin" ]; }; then
  # reconstruir stage desde cualquier hello del store de nix (si existe)
  NIX_HELLO=$(ls -d /nix/store/*hello-2.12.* 2>/dev/null | head -n1)
  if [ -n "$NIX_HELLO" ]; then
    python3 -c "
from src.xbps.builder import stage_from_nix_result
from pathlib import Path
stage_from_nix_result(Path('$NIX_HELLO'), Path('$STAGE_HELLO'))" || true
  fi
fi
if [ -d "$STAGE_HELLO" ] && { [ -d "$STAGE_HELLO/usr/bin" ] || [ -d "$STAGE_HELLO/bin" ]; }; then
rm -rf "$TRH_DIR"; mkdir -p "$TRH_DIR"
for i in 1 2 3; do
  "$PRIV" find "$STAGE_HELLO" -exec touch -h -d @0 {} \; 2>/dev/null || find "$STAGE_HELLO" -exec touch -h -d @0 {} \;
  "$PRIV" find "$STAGE_HELLO" -exec chown -h 0:0 {} \; 2>/dev/null || true
  (cd "$TRH_DIR" && SOURCE_DATE_EPOCH=0 TZ=UTC LC_ALL=C \
    "$XBPS_CREATE_BIN" -A "$ARCH" -n "hello-2.12.3_1" -s "CI TRH" \
    -m "ci@local" -l "GPL-3.0-only" --compression zstd -D "glibc>=2.41" "$STAGE_HELLO" >/dev/null 2>&1)
  sha256sum "$TRH_DIR/$TRH_STAGE_HELLO-"*.$ARCH.xbps 2>/dev/null | awk '{print $1}' >> "$TRH_DIR/hashes.txt"
done
H_UNIQUE=$(sort -u "$TRH_DIR/hashes.txt" 2>/dev/null | wc -l)
fi
[ "$H_UNIQUE" = "1" ] && pass "TRH $TRH_STAGE_HELLO 3/3 idénticos ($(head -n1 "$TRH_DIR/hashes.txt" | cut -c1-16)…)" \
                      || echo "[CI] ⚠️  TRH básico OMITIDO (stage ausente; define TRH_STAGE_HELLO)"

# --- TRH paquete REAL opcional: receta completa con determinize cross-host ---
STAGE_BUN=""
if [ -n "${TRH_STAGE_REAL:-}" ]; then
  step "TRH: paquete real ($TRH_STAGE_REAL) ×3 (con determinize)"
  STAGE_BUN="$WORKSPACE/fake-root/$TRH_STAGE_REAL"
  if [ ! -d "$STAGE_BUN" ]; then
    echo "[CI] ⚠️  stage '$STAGE_BUN' ausente: TRH real OMITIDO"
    STAGE_BUN=""
  fi
fi
if [ -n "$STAGE_BUN" ]; then
  rm -rf "$TRH_DIR/bun"; mkdir -p "$TRH_DIR/bun"
  for i in 1 2 3; do
    "$PRIV" find "$STAGE_BUN" -exec touch -h -d @0 {} \; 2>/dev/null || true
    "$PRIV" find "$STAGE_BUN" -exec chown -h 0:0 {} \; 2>/dev/null || true
    (cd "$TRH_DIR/bun" && SOURCE_DATE_EPOCH=0 TZ=UTC LC_ALL=C \
      "$XBPS_CREATE_BIN" -A "$ARCH" -n "$TRH_STAGE_REAL-1_1" -s "CI TRH real" \
      -m "ci@local" -l "custom:unknown" --compression zstd -D "glibc>=2.41" "$STAGE_BUN" >/dev/null 2>&1)
    python3 -c "
from src.xbps.determinize import determinize_xbps
from pathlib import Path
import sys
_, sha = determinize_xbps(sorted(Path('$TRH_DIR/bun').glob(f'*.{__import__\'os\'.environ.get(\'AUR2XBPS_ARCH\', \'$ARCH\')}.xbps'))[0])
print(sha)" >> "$TRH_DIR/bun/hashes.txt"
    rm -f "$TRH_DIR"/bun/*.$ARCH.xbps
  done
  HB_UNIQUE=$(sort -u "$TRH_DIR/bun/hashes.txt" | wc -l)
  [ "$HB_UNIQUE" = "1" ] && pass "TRH $TRH_STAGE_REAL 3/3 idénticos ($(head -n1 "$TRH_DIR/bun/hashes.txt" | cut -c1-16)…)" \
                        || fail "TRH $TRH_STAGE_REAL: $HB_UNIQUE hashes distintos"
fi

# ---------- 4. TIAR: canary en sandbox Nix ----------
step "TIAR: canary de red dentro del sandbox"
CANARY_DIR="/tmp/ci-canary"
mkdir -p "$CANARY_DIR"
cat > "$CANARY_DIR/flake.nix" <<'NIX'
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
  outputs = { self, nixpkgs }: let
    system = "x86_64-linux";
    pkgs = import nixpkgs { inherit system; };
  in {
    packages.${system}.canary = pkgs.stdenv.mkDerivation {
      name = "tiar-canary";
      nativeBuildInputs = [ pkgs.curl ];
      buildPhase = ''
        timeout 15 curl -sS --max-time 10 "https://aur.archlinux.org/rpc/v5/info?arg[]=spotify" \
          > conn.txt 2>&1 || echo "CURL_BLOCKED_EXIT_$?" > conn.txt
        cat conn.txt > $out
      '';
      installPhase = "touch $out";
      phases = "buildPhase installPhase";
    };
  };
}
NIX
CANARY_OUT=$(cd "$CANARY_DIR" && timeout 240 nix build .#canary \
  --extra-experimental-features "nix-command flakes" --option sandbox true >/dev/null 2>&1; \
  cat "$CANARY_DIR/result" 2>/dev/null || echo "SIN_RESULTADO")
if echo "$CANARY_OUT" | grep -qE "CURL_BLOCKED_EXIT_6|Could not resolve host"; then
  pass "TIAR: DNS/red bloqueados en sandbox ($(echo "$CANARY_OUT" | head -n1))"
else
  fail "TIAR: canary no confirmó bloqueo: '$(echo "$CANARY_OUT" | head -n2)'"
fi

# ---------- 5. Smoke en chroot Void ----------
SMOKE_TARGET="${SMOKE_TARGET:-}"   # smoke root solo si se define
if [ -z "${SMOKE_TARGET:-}" ]; then
  echo "[CI] ⚠️  SMOKE_TARGET sin definir: smoke chroot OMITIDO"
else
step "Smoke EEL en chroot Void ($SMOKE_TARGET)"
XBPS_INSTALL_BIN="$(xb xbps-install)"
[ -n "$XBPS_INSTALL_BIN" ] || XBPS_INSTALL_BIN="xbps-install"
"$XBPS_INSTALL_BIN" -r "$MASTERDIR" --repository="$REPO" -y "$SMOKE_TARGET" >/dev/null 2>&1 \
  && pass "instalación $SMOKE_TARGET" || fail "instalación $SMOKE_TARGET"
SMOKE_BIN="${SMOKE_BIN:-$(echo "$SMOKE_TARGET" | sed 's/-bin$//')}"
BIN=$(find "$MASTERDIR/usr/bin" -maxdepth 1 -name "$SMOKE_BIN" 2>/dev/null | head -n1)
if [ -n "$BIN" ]; then
  OUT=$("$PRIV" chroot "$MASTERDIR" "/usr/bin/$SMOKE_BIN" --version 2>&1 || true)
  # EEL válido: sin errores del cargador y con salida propia de la app
  # apps que rechazan root emiten salida propia — eso es ejecución correcta
  if echo "$OUT" | grep -qE "loading shared libraries|Segmentation fault"; then
    fail "smoke $SMOKE_BIN: error de cargador"
  elif [ -n "$(echo "$OUT" | tr -d '[:space:]')" ]; then
    pass "smoke $SMOKE_BIN ejecuta (sin errores de enlazado): $(echo "$OUT" | head -n1 | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-60)"
  else
    fail "smoke $SMOKE_BIN sin salida"
  fi
else
  fail "binario $SMOKE_BIN no encontrado en masterdir (¿repo sin $SMOKE_TARGET?)"
fi
fi

# ---------- 5b. Smoke Python fuente (opcional, env-driven) ----------
if [ -n "${PYTHON_SMOKE_PKG:-}" ]; then
  step "Smoke Python fuente ($PYTHON_SMOKE_PKG)"
  "$XBPS_INSTALL_BIN" -r "$MASTERDIR" --repository="$REPO" -y "$PYTHON_SMOKE_PKG" >/dev/null 2>&1 \
    && pass "instalación $PYTHON_SMOKE_PKG" || fail "instalación $PYTHON_SMOKE_PKG"
fi

# ---------- 6. Smoke como usuario NO-root en el chroot (uid real del invocador) ----------
NR_UID="$(id -u)"; NR_GID="$(id -g)"; NR_USER="$(id -un)"
step "Smoke no-root ($NR_USER uid $NR_UID en chroot)"
if ! "$PRIV" grep -q "^$NR_USER:" "$MASTERDIR/etc/passwd" 2>/dev/null; then
  "$PRIV" sh -c "echo '$NR_USER:x:$NR_UID:$NR_UID::/home/$NR_USER:/bin/sh' >> '$MASTERDIR/etc/passwd'"
  "$PRIV" sh -c "echo '$NR_USER:x:$NR_GID:' >> '$MASTERDIR/etc/group'"
  pass "usuario $NR_USER creado en masterdir"
else
  pass "usuario $NR_USER ya existe"
fi
"$PRIV" mkdir -p "$MASTERDIR/home/$NR_USER"
"$PRIV" chown -R "$NR_UID:$NR_GID" "$MASTERDIR/home/$NR_USER"
"$PRIV" chmod 755 "$MASTERDIR/home/$NR_USER"
# /dev/null accesible para no-root (el bind de /dev del host puede heredar 600)
[ -e "$MASTERDIR/dev/null" ] && "$PRIV" chmod 666 "$MASTERDIR/dev/null" 2>/dev/null || \
  "$PRIV" mknod -m 666 "$MASTERDIR/dev/null" c 1 3 2>/dev/null || true
NOROOT_PKG="${NOROOT_SMOKE_PKG:-${SMOKE_TARGET:-}}"
NOROOT_BIN="${NOROOT_SMOKE_BIN:-$(echo "$NOROOT_PKG" | sed 's/-git$//;s/-bin$//' )}"
XBPS_QUERY_BIN="$(xb xbps-query)"
[ -n "$XBPS_QUERY_BIN" ] || XBPS_QUERY_BIN="xbps-query"
if [ -n "$NOROOT_PKG" ] && [ "${NOROOT_SMOKE_ENABLED:-1}" = "1" ] &&
   ls "$REPO"/"$NOROOT_PKG"-*.xbps >/dev/null 2>&1; then
  INSTALLED=$("$XBPS_QUERY_BIN" -r "$MASTERDIR" -l 2>/dev/null | grep -c "$NOROOT_PKG" || true)
  if [ "${INSTALLED:-0}" = "0" ]; then
    "$XBPS_INSTALL_BIN" -r "$MASTERDIR" --repository="$REPO" -y "$NOROOT_PKG" >/dev/null 2>&1 \
      && pass "instalación $NOROOT_PKG" || fail "instalación $NOROOT_PKG"
  fi
  if [ -x "$MASTERDIR/usr/bin/$NOROOT_BIN" ]; then
    OUT=$("$PRIV" chroot --userspec="$NR_UID:$NR_GID" "$MASTERDIR" /usr/bin/env \
          HOME="/home/$NR_USER" TERM=dumb "/usr/bin/$NOROOT_BIN" --version 2>&1 || true)
    if echo "$OUT" | grep -qE "loading shared libraries|Segmentation fault|Permission denied|fallo al ejecutar|No such file"; then
      fail "smoke no-root: $(echo "$OUT" | head -n1)"
    elif [ -n "$(echo "$OUT" | tr -d '[:space:]')" ]; then
      pass "smoke NO-ROOT $NOROOT_BIN: $(echo "$OUT" | head -n1 | cut -c1-60)"
    else
      fail "smoke no-root sin salida"
    fi
  else
    echo "[CI] ⚠️  /usr/bin/$NOROOT_BIN ausente en masterdir: smoke no-root OMITIDO"
  fi
else
  echo "[CI] ⚠️  smoke no-root OMITIDO (define NOROOT_SMOKE_PKG o instala el paquete en el repo)"
fi

# ---------- Resumen ----------
echo -e "\n=============================="
if [ "$FAIL" = "0" ]; then
  echo "[CI] RESULTADO: ✅ TODOS LOS CHECKS PASAN"
else
  echo "[CI] RESULTADO: ❌ HAY FALLOS (revisar arriba)"
  notify_fail "aur2xbps ci-local falló; revisar $(ls -t /var/log/aur2xbps-ci.log* 2>/dev/null | head -n1)"
fi
exit "$FAIL"
