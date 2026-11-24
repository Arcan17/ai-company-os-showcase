#!/usr/bin/env bash
# Prueba de humo del contenedor: usa ./start.sh en un proyecto y puerto APARTE, comprueba el flujo corto y lo apaga borrando sus datos.
# Uso (raíz del repo):  qa/smoke_container.sh      Necesita Podman o Docker con «compose».
set -euo pipefail
cd "$(dirname "$0")/.."
export PORT="${SMOKE_PORT:-18731}" COMPOSE_PROJECT_NAME="aicos-smoke" AICOS_STEP_DELAY=0
cleanup() { ./start.sh reset >/dev/null 2>&1 || true; }
trap cleanup EXIT
./start.sh
echo "▶ Estado de salud del contenedor y solo lectura:"
ENGINE="${AICOS_ENGINE:-}"
if [ -z "$ENGINE" ]; then command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1 && ENGINE=podman || ENGINE=docker; fi
CID=$($ENGINE compose ps -q aicos)
$ENGINE inspect -f 'usuario={{.Config.User}} solo_lectura={{.HostConfig.ReadonlyRootfs}}' "$CID"
[ "$($ENGINE inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$CID")" = "true" ] || { echo "✖ el sistema de archivos no es de solo lectura"; exit 1; }
[ "$($ENGINE inspect -f '{{.Config.User}}' "$CID")" = "10001" ] || { echo "✖ el contenedor corre como root"; exit 1; }
echo "▶ Flujo corto por HTTP:"
python3 qa/smoke_http.py "http://127.0.0.1:${PORT}"
if [ "${AICOS_PUBLIC_DEMO:-0}" = "1" ]; then
  echo "▶ Demo pública: dos visitantes aislados:"
  python3 qa/smoke_public.py "http://127.0.0.1:${PORT}"
fi
