#!/usr/bin/env bash
# Demo LOCAL con IA real y GRATIS: el planificador del coordinador usa un modelo de Ollama (por defecto el de este Mac; o el del HP por túnel SSH). SOLO LOCAL: la demo pública
# jamás usa un modelo (se niega a arrancar). La IA solo PROPONE el plan; el motor lo valida y una persona aprueba. Los datos siguen siendo ficticios.
#
#   ./run_demo_ia.sh                                   # Ollama de este Mac, modelo qwen3.5:9b (~4 s por plan)
#   AICOS_OLLAMA_MODEL=qwen3.5:4b-q4_K_M ./run_demo_ia.sh
#   # IA del HP (más lenta, ~16 s por plan; sin GPU):  ssh -N -L 18434:127.0.0.1:11434 hp-homelab &
#   AICOS_OLLAMA_URL=http://127.0.0.1:18434 AICOS_OLLAMA_MODEL=qwen3:4b-instruct-hermes ./run_demo_ia.sh
# Variables: PORT (8731) · AICOS_RESET=1 (base nueva) · AICOS_LLM_PLAN_TIMEOUT (90 s: pasado el plazo se usan las reglas, rotulado).
set -euo pipefail
cd "$(dirname "$0")"
URL="${AICOS_OLLAMA_URL:-http://127.0.0.1:11434}"
MODEL="${AICOS_OLLAMA_MODEL:-qwen3.5:9b}"
TAGS="$(curl -fsS --max-time 5 "$URL/api/tags" 2>/dev/null || true)"
if [ -z "$TAGS" ]; then
  echo "✖ No hay un Ollama respondiendo en $URL." >&2
  echo "  Arráncalo (abre la app de Ollama o ejecuta: ollama serve) o apunta a otro con AICOS_OLLAMA_URL." >&2
  exit 1
fi
if ! printf '%s' "$TAGS" | python3 -c "import sys,json; n=[m['name'] for m in json.load(sys.stdin).get('models',[])]; sys.exit(0 if '$MODEL' in n else 1)"; then
  echo "✖ El modelo «${MODEL}» no está instalado en $URL." >&2
  echo "  Instálalo con: ollama pull ${MODEL}   (o elige otro con AICOS_OLLAMA_MODEL)." >&2
  exit 1
fi
export AICOS_LLM_PROVIDER=ollama AICOS_OLLAMA_URL="$URL" AICOS_OLLAMA_MODEL="$MODEL"
export AICOS_LLM_PLAN_TIMEOUT="${AICOS_LLM_PLAN_TIMEOUT:-90}"
echo "▶ IA real y gratis: $MODEL en $URL (plazo por plan: ${AICOS_LLM_PLAN_TIMEOUT} s)"
exec ./run_demo.sh
