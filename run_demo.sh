#!/usr/bin/env bash
# One-command local demo: backend (FastAPI) + built frontend on http://127.0.0.1:8000  — LOCAL ONLY.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8731}"
if [ ! -x backend/.venv/bin/python ]; then
  echo "▶ Creando entorno Python 3.12 (uv)…"
  (cd backend && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt)
fi
if [ ! -d frontend/dist ]; then
  echo "▶ Compilando frontend…"
  (cd frontend && npm ci && npm run build)
fi
[ "${AICOS_RESET:-0}" = "1" ] && rm -f backend/data/aicos.db* && echo "▶ Base de datos reiniciada"
export AICOS_ALLOW_COMPANY_CREATION="${AICOS_ALLOW_COMPANY_CREATION:-1}"   # laboratorio LOCAL: permite crear empresas ficticias desde la pantalla de login (apágalo con =0)
echo "▶ AI Company OS → http://127.0.0.1:${PORT}   (cuentas DEMO ficticias; contraseña demo1234)"
echo "  Motor: ${AICOS_LLM_PROVIDER:-deterministic}   (AICOS_LLM_PROVIDER=ollama para el LLM local opcional)"
[ "${AICOS_OPEN_BROWSER:-0}" = "1" ] && ( sleep 1.5; command -v open >/dev/null && open "http://127.0.0.1:${PORT}" || true ) &   # opt-in: por defecto NO se abre ningún navegador
cd backend && exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --no-proxy-headers
