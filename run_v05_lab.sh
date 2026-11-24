#!/usr/bin/env bash
# AI Company OS V0.5 — laboratorio de CONFIGURACIÓN (crear empresas ficticias, empleados y asistentes desde la UI). Aislado de :8731 y de la demo :8743.
#   ./run_v05_lab.sh            # = start: BD propia backend/data/v05_lab.sqlite3 (Clima Sur + Motor Norte sembradas), puerto libre desde 8750, creación de empresas ACTIVADA (solo loopback)
#   ./run_v05_lab.sh status | stop | reset [--yes]
# Garantías: nunca abre aicos.db ni demo_comercial*; no usa 8731/8743/8744/8000; solo detiene lo que arrancó (pidfile + comando + carpeta); no abre navegadores.
# La UI V0.5 se compila en frontend/dist-v05 (no toca frontend/dist, que sirven el laboratorio y la demo ya en marcha).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"; DATA="$ROOT/backend/data"; DB="$DATA/v05_lab.sqlite3"; PIDF="$DATA/.v05_lab.pid"; PY="$ROOT/backend/.venv/bin/python"
RESERVED="8731 8743 8744 8745 8741 8742 8000"; CMD="${1:-start}"; shift || true; YES=0; for a in "$@"; do [ "$a" = "--yes" ] && YES=1; done
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
port_pid() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }
is_ours() { local pid="$1"; [ -n "$pid" ] || return 1; ps -p "$pid" -o command= 2>/dev/null | grep -q "uvicorn app.main:app" || return 1; lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep -q "^n$ROOT/backend$"; }
running_port() { [ -f "$PIDF" ] || return 0; local pid; pid="$(awk '{print $1}' "$PIDF")"; if kill -0 "$pid" 2>/dev/null && is_ours "$pid"; then awk '{print $2}' "$PIDF"; fi; }
free_port() { local p=8750 i=0; while [ $i -lt 30 ]; do case " $RESERVED " in *" $p "*) ;; *) [ -z "$(port_pid "$p")" ] && { echo "$p"; return; } ;; esac; p=$((p+1)); i=$((i+1)); done; die "sin puerto libre cerca de 8750"; }
ensure() { [ -x "$PY" ] || { (cd "$ROOT/backend" && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt); }
  [ -f "$ROOT/frontend/dist-v05/index.html" ] || { echo "▶ Compilando la UI V0.5 (frontend/dist-v05)…"; (cd "$ROOT/frontend" && { [ -d node_modules ] || npm ci; } && BUILD_DIR=dist-v05 npm run build); }; mkdir -p "$DATA"; }
case "$CMD" in
  start)
    ensure; rp="$(running_port)"
    if [ -n "$rp" ]; then echo "• ya está corriendo en http://127.0.0.1:$rp"; exit 0; fi
    port="$(free_port)"; logf="$DATA/v05_lab.log"
    ( cd "$ROOT/backend" && exec nohup env AICOS_DB_URL="sqlite:///data/v05_lab.sqlite3" AICOS_STATIC_DIR="$ROOT/frontend/dist-v05" AICOS_ALLOW_COMPANY_CREATION=1 AICOS_LLM_PROVIDER="${AICOS_LLM_PROVIDER:-deterministic}" AICOS_STEP_DELAY="${AICOS_STEP_DELAY:-0.45}" \
        "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" --no-proxy-headers ) >> "$logf" 2>&1 < /dev/null &
    echo "$! $port" > "$PIDF"; disown 2>/dev/null || true
    i=0; until curl -s -o /dev/null "http://127.0.0.1:$port/api/setup/info"; do i=$((i+1)); [ $i -gt 60 ] && die "no arrancó; mira $logf"; sleep 0.5; done
    echo "══════════════════════════════════════════════════════════════════"
    echo "  AI COMPANY OS V0.5 · laboratorio de configuración (datos FICTICIOS, solo este Mac)"
    echo "  URL            : http://127.0.0.1:$port      (pantalla de login → «Crear una empresa ficticia»)"
    echo "  Base de datos  : backend/data/v05_lab.sqlite3   (NO es aicos.db ni la demo)"
    echo "  Guía           : docs/v05/GUIA_NUEVA_EMPRESA.md"
    echo "  Intactos       : laboratorio :8731 y demo :8743"
    echo "══════════════════════════════════════════════════════════════════"
    ;;
  status) rp="$(running_port)"; [ -n "$rp" ] && echo "● laboratorio V0.5 corriendo en http://127.0.0.1:$rp" || echo "○ laboratorio V0.5 detenido"; echo "▫ :8731 → $( [ -n "$(port_pid 8731)" ] && echo en marcha || echo detenido ) · demo :8743 → $( [ -n "$(port_pid 8743)" ] && echo en marcha || echo detenida )  (este script no los toca)";;
  stop) if [ -f "$PIDF" ]; then pid="$(awk '{print $1}' "$PIDF")"; if is_ours "$pid"; then kill "$pid" && echo "✔ detenido PID $pid"; else echo "• el proceso ya no existe o no es nuestro"; fi; rm -f "$PIDF"; else echo "• nada que detener"; fi;;
  reset)
    [ "$(basename "$DB")" = "v05_lab.sqlite3" ] || die "ruta inesperada"
    echo "Esto BORRA solo $DB (copia previa en backend/data/backups/). No toca aicos.db, la demo ni :8731."
    if [ "$YES" != 1 ]; then printf 'Escribe REINICIAR-LAB para confirmar: '; read -r ans; [ "$ans" = "REINICIAR-LAB" ] || die "cancelado (no se cambió nada)"; fi
    was="$(running_port)"; [ -n "$was" ] && "$0" stop >/dev/null
    if [ -f "$DB" ]; then mkdir -p "$DATA/backups"; bk="$DATA/backups/v05_lab-$(date +%Y%m%d%H%M%S).sqlite3"; "$PY" - "$DB" "$bk" <<'PY'
import sqlite3, sys
s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()
PY
      echo "✔ copia previa: ${bk#$ROOT/}"; fi
    case "$DB" in */backend/data/v05_lab.sqlite3) rm -f "$DB" "$DB-wal" "$DB-shm";; *) die "ruta no permitida";; esac
    echo "✔ laboratorio restablecido"; [ -n "$was" ] && "$0" start;;
  *) die "uso: $0 [start|stop|status|reset] [--yes]";;
esac
