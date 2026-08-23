#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# campaign.sh — orquestador de campañas masivas remotas (workflow mass-build.yml
# en GitHub Actions). Lanza el workflow, localiza el run y organiza los
# artefactos descargados delegando en scripts/campaign-report.py.
#
# Los comandos `gh` SOLO se invocan dentro de las funciones de subcomando:
# `campaign.sh -h` funciona sin gh ni git.

set -euo pipefail

readonly WORKFLOW="mass-build.yml"
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPORT_PY="$HERE/campaign-report.py"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

uso() {
  cat <<'EOF'
Uso: scripts/campaign.sh <subcomando> [opciones]

Orquesta campañas masivas remotas de aur2xbps (workflow mass-build.yml en
GitHub Actions) y organiza sus artefactos localmente.

Subcomandos:
  start [opciones]          Lanza el workflow y muestra su RUN_ID.
      --seed S                semilla de muestreo reproducible
      --count N               número de paquetes a construir
      --shards K              número de shards paralelos
      --engines E             both | nix | xbps-src
      --timeout-min M         timeout por build (minutos)
      --only "p1 p2 ..."      lista explícita de paquetes (espacio-separada)
      --wait                  espera al run y ejecuta 'fetch' al terminar

  fetch RUN_ID [DIR] [--keep-raw]
                            Descarga los artefactos del run y genera:
                              xbps/<motor>/<pkg>/, logs/, campaign-results.json,
                              manifest.json, report.md, failed-only.txt y
                              blocked-only.txt.
                            DIR por defecto:
                              ${AUR2XBPS_ARCHIVE:-/nix/xbps-archive}/<fechaUTC>-run<RUN_ID>

  retry-failed DIR          Relanza la campaña solo con los paquetes listados
                            en DIR/failed-only.txt (ambos motores, 1 shard).

  analyze DIR               Resumen compacto del directorio ya organizado.

Opciones globales:
  -h, --help                Muestra esta ayuda.

Ejemplos:
  scripts/campaign.sh start --seed 42 --count 100 --shards 10 --engines both --wait
  scripts/campaign.sh fetch 9876543210
  scripts/campaign.sh analyze /nix/xbps-archive/20260823-run9876543210
  scripts/campaign.sh retry-failed /nix/xbps-archive/20260823-run9876543210
EOF
}

necesita_gh() {
  command -v gh >/dev/null 2>&1 || die "'gh' no está disponible en PATH"
}

raiz_repo() {
  # GOTCHA: gh debe ejecutarse desde el workdir del repo.
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

# --------------------------------------------------------------- start
cmd_start() {
  local seed="" count="" shards="" engines="" timeout_min="" only=""
  local esperar=0

  while [ $# -gt 0 ]; do
    case "$1" in
      --seed)        [ $# -ge 2 ] || die "--seed requiere un valor"; seed="$2"; shift 2 ;;
      --count)       [ $# -ge 2 ] || die "--count requiere un valor"; count="$2"; shift 2 ;;
      --shards)      [ $# -ge 2 ] || die "--shards requiere un valor"; shards="$2"; shift 2 ;;
      --engines)     [ $# -ge 2 ] || die "--engines requiere un valor"; engines="$2"; shift 2 ;;
      --timeout-min) [ $# -ge 2 ] || die "--timeout-min requiere un valor"; timeout_min="$2"; shift 2 ;;
      --only)        [ $# -ge 2 ] || die "--only requiere un valor"; only="$2"; shift 2 ;;
      --wait)        esperar=1; shift ;;
      *) die "opción desconocida para 'start': $1" ;;
    esac
  done

  necesita_gh

  local -a flags=()
  [ -n "$seed" ] && flags+=(-f "seed=$seed")
  [ -n "$count" ] && flags+=(-f "count=$count")
  [ -n "$shards" ] && flags+=(-f "shards=$shards")
  [ -n "$engines" ] && flags+=(-f "engines=$engines")
  [ -n "$timeout_min" ] && flags+=(-f "timeout_min=$timeout_min")
  [ -n "$only" ] && flags+=(-f "only=$only")

  cd "$(raiz_repo)"
  echo ">> lanzando $WORKFLOW en main..."
  gh workflow run "$WORKFLOW" --ref main ${flags[@]+"${flags[@]}"}

  echo ">> esperando a que GitHub registre el run..."
  sleep 8

  local lista run_id
  lista="$(gh run list --workflow="$WORKFLOW" --limit 5 \
    --json databaseId,status,event,headSha,createdAt)" \
    || die "gh run list falló"
  run_id="$(printf '%s\n' "$lista" | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(1)
candidatos = [
    r for r in runs
    if r.get("event") == "workflow_dispatch"
    and r.get("status") in ("queued", "in_progress")
]
if not candidatos:
    sys.exit(1)
candidatos.sort(key=lambda r: str(r.get("createdAt", "")), reverse=True)
print(candidatos[0]["databaseId"])
')" || die "no hay ningún run reciente en cola o en ejecución de $WORKFLOW"

  echo "RUN_ID=$run_id"

  if [ "$esperar" -eq 1 ]; then
    echo ">> esperando la finalización del run $run_id..."
    gh run watch "$run_id" --exit-status \
      || echo "aviso: el run terminó con fallos; se intenta recuperar igualmente"
    cmd_fetch "$run_id"
  fi
}

# --------------------------------------------------------------- fetch
cmd_fetch() {
  [ $# -ge 1 ] || die "uso: fetch RUN_ID [DIR] [--keep-raw]"
  local run_id="$1"
  shift

  local destino="" mantener_raw=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --keep-raw) mantener_raw=1; shift ;;
      -*) die "opción desconocida para 'fetch': $1" ;;
      *)
        if [ -z "$destino" ]; then
          destino="$1"; shift
        else
          die "argumento inesperado para 'fetch': $1"
        fi ;;
    esac
  done

  necesita_gh

  local base="${AUR2XBPS_ARCHIVE:-/nix/xbps-archive}"
  if [ -z "$destino" ]; then
    destino="$base/$(date -u +%Y%m%d)-run$run_id"
  fi
  local crudo="$destino/_raw"

  mkdir -p "$destino"
  # GOTCHA: gh run download no tiene --force en esta versión → borrar antes.
  rm -rf "$crudo"

  cd "$(raiz_repo)"
  echo ">> descargando artefactos del run $run_id en $crudo..."
  gh run download "$run_id" -D "$crudo"

  local -a py_args=("organize" "--raw" "$crudo" "--dest" "$destino" "--run" "$run_id")
  [ "$mantener_raw" -eq 1 ] && py_args+=("--keep-raw")

  python3 "$REPORT_PY" "${py_args[@]}"

  local n_xbps
  n_xbps="$(find "$destino/xbps" -type f -name '*.xbps' 2>/dev/null | wc -l | tr -d ' ')"
  echo "informe: $destino/report.md"
  echo "artefactos .xbps organizados: ${n_xbps:-0}"
}

# -------------------------------------------------------- retry-failed
cmd_retry_failed() {
  [ $# -eq 1 ] || die "uso: retry-failed DIR"
  local dir="$1"
  local fichero="$dir/failed-only.txt"

  local fallidos=""
  if [ -f "$fichero" ]; then
    fallidos="$(tr '\n\r' '  ' < "$fichero" | xargs 2>/dev/null || true)"
  fi
  if [ -z "$fallidos" ]; then
    echo "aviso: no hay paquetes fallidos que reintentar ($fichero vacío o ausente)"
    return 0
  fi

  echo ">> reintentando $(printf '%s\n' "$fallidos" | wc -w | tr -d ' ') paquete(s)..."
  cmd_start --only "$fallidos" --engines both --shards 1
}

# ------------------------------------------------------------- analyze
cmd_analyze() {
  [ $# -eq 1 ] || die "uso: analyze DIR"
  python3 "$REPORT_PY" analyze --dir "$1"
}

# ---------------------------------------------------------------- main
main() {
  [ $# -ge 1 ] || { uso >&2; exit 2; }
  case "$1" in
    -h|--help|help) uso ;;
    start)        shift; cmd_start "$@" ;;
    fetch)        shift; cmd_fetch "$@" ;;
    retry-failed) shift; cmd_retry_failed "$@" ;;
    analyze)      shift; cmd_analyze "$@" ;;
    *) die "subcomando desconocido: '$1' (prueba -h)" ;;
  esac
}

main "$@"
