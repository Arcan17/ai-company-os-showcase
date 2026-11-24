#!/usr/bin/env bash
# UN COMANDO: construye la imagen, la levanta y espera a que responda.   ./start.sh            (arranca)
#                                                                           ./start.sh stop       (apaga; conserva los datos)
#                                                                           ./start.sh reset      (apaga y BORRA los datos de la demo)
# Requiere Podman o Docker con «compose». Datos 100 % ficticios; sin integraciones externas.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8731}"; export PORT
# AICOS_ENGINE=docker|podman fuerza el motor (útil si hay ambos instalados, como en un runner de CI); sin él se prefiere Podman y, si no, Docker.
ENG="${AICOS_ENGINE:-}"
if [ -z "$ENG" ]; then
  if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then ENG=podman
  elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then ENG=docker; fi
fi
case "$ENG" in
  podman|docker) command -v "$ENG" >/dev/null 2>&1 && "$ENG" compose version >/dev/null 2>&1 || { echo "✖ «$ENG compose» no está disponible." >&2; exit 1; }; C=("$ENG" compose) ;;
  *) echo "✖ Hace falta Podman o Docker con «compose» instalado." >&2; exit 1 ;;
esac
case "${1:-up}" in
  stop)  "${C[@]}" down; exit 0 ;;
  reset) "${C[@]}" down -v; echo "▶ Datos de la demo borrados."; exit 0 ;;
  up) ;;
  *) echo "uso: ./start.sh [up|stop|reset]" >&2; exit 2 ;;
esac
echo "▶ Construyendo y levantando con ${C[*]} …"
"${C[@]}" up -d --build
echo -n "▶ Esperando a que responda"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo; echo "✔ Listo → http://127.0.0.1:${PORT}   (cuentas FICTICIAS; contraseña demo1234; p. ej. dueno@climasur.test)"; exit 0
  fi
  echo -n "."; sleep 2
done
echo; echo "✖ No respondió a tiempo. Mira los registros:  ${C[*]} logs aicos" >&2; exit 1
