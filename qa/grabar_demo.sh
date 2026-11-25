#!/usr/bin/env bash
# Kit de grabación: recorre la demo REAL (misma app, mismo servidor) con rótulos y cursor visibles y deja, en ./grabaciones/ (ignorada por Git):
#   demo-completo.mp4 · escena-NN-<nombre>.mp4 y .gif (una por escena, para pegar en LinkedIn/README) · capturas/*.png
#
#   qa/grabar_demo.sh            # con el motor de reglas (determinista, sin dependencias)
#   qa/grabar_demo.sh --ia       # con IA LOCAL gratis (Ollama): AICOS_OLLAMA_URL / AICOS_OLLAMA_MODEL (por defecto este Mac y qwen3.5:9b); la escena del plan muestra a la IA proponer
# Necesita: ffmpeg (y gifsicle, opcional, para achicar los GIF). Todo con datos ficticios; nada se publica ni se sube desde aquí.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"; OUT="${GRABACIONES_DIR:-$ROOT/grabaciones}"
IA=0; [ "${1:-}" = "--ia" ] && IA=1
command -v ffmpeg >/dev/null || { echo "✖ Falta ffmpeg (brew install ffmpeg)." >&2; exit 1; }
if [ "$IA" = "1" ]; then
  URL="${AICOS_OLLAMA_URL:-http://127.0.0.1:11434}"; MODEL="${AICOS_OLLAMA_MODEL:-qwen3.5:9b}"
  TAGS="$(curl -fsS --max-time 5 "$URL/api/tags" 2>/dev/null || true)"
  [ -n "$TAGS" ] || { echo "✖ No hay un Ollama respondiendo en $URL (arráncalo o apunta a otro con AICOS_OLLAMA_URL)." >&2; exit 1; }
  printf '%s' "$TAGS" | python3 -c "import sys,json; n=[m['name'] for m in json.load(sys.stdin).get('models',[])]; sys.exit(0 if '$MODEL' in n else 1)" \
    || { echo "✖ El modelo «${MODEL}» no está instalado en $URL (ollama pull ${MODEL})." >&2; exit 1; }
  export AICOS_LLM_PROVIDER=ollama AICOS_OLLAMA_URL="$URL" AICOS_OLLAMA_MODEL="$MODEL" AICOS_LLM_PLAN_TIMEOUT="${AICOS_LLM_PLAN_TIMEOUT:-90}"
  echo "▶ Grabando con IA local: ${MODEL} en $URL"
else
  echo "▶ Grabando con el motor de reglas (usa --ia para IA local)"
fi
rm -rf "$OUT/raw" "$OUT/capturas"; mkdir -p "$OUT"
( cd frontend && BUILD_DIR=dist-v05 npm run build >/dev/null )
( cd frontend && RECORD_VIDEO=1 RECORD_IA="$IA" REC_OUT="$OUT/raw" REC_SHOTS="$OUT/capturas" npx playwright test -c playwright.record.config.ts )
[ -f "$OUT/raw/frames.txt" ] || { echo "✖ La prueba no capturó cuadros (¿falló el recorrido?)." >&2; exit 1; }
echo "▶ Uniendo los cuadros y convirtiendo…"
VIDEO="$OUT/demo-completo.mp4"
ffmpeg -loglevel error -y -f concat -safe 0 -i "$OUT/raw/frames.txt" -vf "fps=25,format=yuv420p" -c:v libx264 -preset slow -crf 20 -movflags +faststart "$VIDEO"
python3 - "$OUT" "$VIDEO" <<'PY'
import json, subprocess, sys, shutil, os
out, video = sys.argv[1], sys.argv[2]
m = json.load(open(f"{out}/raw/marks.json"))
sc, dur = m["escenas"], m["duracion"]
LEAD = 0.0          # las marcas ya están medidas contra el primer cuadro del video
for i, s in enumerate(sc):
    a = max(0.0, s["desde"] + LEAD - 0.2)
    b = (sc[i + 1]["desde"] + LEAD) if i + 1 < len(sc) else dur + LEAD
    nombre = s["escena"]
    mp4, gif = f"{out}/escena-{nombre}.mp4", f"{out}/escena-{nombre}.gif"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", video, "-c:v", "libx264", "-preset", "slow", "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4], check=True)
    vf = "fps=10,scale=880:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", video, "-vf", vf, "-loop", "0", gif], check=True)
    if shutil.which("gifsicle"):
        subprocess.run(["gifsicle", "-O3", "--lossy=60", "-b", gif], check=True)
print("\n  Archivo".ljust(46) + "Tamaño")
for f in sorted(os.listdir(out)):
    if f.endswith((".mp4", ".gif")):
        print("  " + f.ljust(44) + f"{os.path.getsize(f'{out}/{f}') / 1e6:.1f} MB")
PY
echo "✔ Listo en $OUT  (capturas en $OUT/capturas)"
