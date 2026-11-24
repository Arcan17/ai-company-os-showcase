#!/usr/bin/env bash
# AI Company OS — DEMO COMERCIAL (aislada del laboratorio :8731). Datos 100 % ficticios, solo loopback, motor determinista por defecto.
#
#   ./run_demo_comercial.sh                 # = start: prepara la BD ficticia (idempotente), arranca en un puerto libre y muestra BD/puerto/modo
#   ./run_demo_comercial.sh status          # qué corre (demo, plan B y laboratorio) — sin tocar nada
#   ./run_demo_comercial.sh preflight       # comprobaciones de solo lectura antes de presentar
#   ./run_demo_comercial.sh reset [--yes]   # RESTABLECE SOLO la base de la demo (pide confirmación, hace copia previa)
#   ./run_demo_comercial.sh planb           # BD «plan B» con el guion ya ejecutado de verdad (API real) en otro puerto
#   ./run_demo_comercial.sh stop            # detiene SOLO los procesos que este script arrancó
#   ./run_demo_comercial.sh test            # pytest de la demo + Playwright del guion (sobre copias temporales)
#   Variables: DEMO_PORT (primer puerto a probar, 8743) · AICOS_LLM_PROVIDER=ollama (variante opcional; por defecto determinista)
#
# Garantías: nunca abre ni escribe backend/data/aicos.db; nunca termina un proceso que no haya arrancado este script (pidfile + comprobación de comando y carpeta);
# nunca usa los puertos 8731 ni 8000; la BD de la demo debe llamarse demo_comercial* (el código se niega si no).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA="$ROOT/backend/data"
DB="$DATA/demo_comercial.sqlite3"
PLANB_DB="$DATA/demo_comercial_planb.sqlite3"
PID_MAIN="$DATA/.demo_comercial.pid"
PID_PLANB="$DATA/.demo_comercial_planb.pid"
PY="$ROOT/backend/.venv/bin/python"
RESERVED="8731 8000 8741 8742"   # laboratorio, otro servicio, e2e y auditoría: nunca se usan
CMD="${1:-start}"; shift || true
YES=0; FORCE=0
for a in "$@"; do case "$a" in --yes) YES=1;; --force) FORCE=1;; esac; done

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
port_pid() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }
is_ours() {   # $1=pid → 0 si es un uvicorn de ESTE proyecto (comando + carpeta de trabajo)
  local pid="$1"; [ -n "$pid" ] || return 1
  ps -p "$pid" -o command= 2>/dev/null | grep -q "uvicorn app.main:app" || return 1
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep -q "^n$ROOT/backend$"
}
pid_from_file() { [ -f "$1" ] && awk '{print $1}' "$1" || true; }
port_from_file() { [ -f "$1" ] && awk '{print $2}' "$1" || true; }
running_port() {   # imprime el puerto si el pidfile apunta a un proceso nuestro vivo
  local pid; pid="$(pid_from_file "$1")"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && is_ours "$pid"; then port_from_file "$1"; fi
}
free_port() {  # primer puerto libre desde $1, saltando los reservados
  local p="$1" i=0
  while [ $i -lt 30 ]; do
    case " $RESERVED " in *" $p "*) ;; *) [ -z "$(port_pid "$p")" ] && { echo "$p"; return; } ;; esac
    p=$((p+1)); i=$((i+1))
  done
  die "no encontré un puerto libre cerca de $1"
}
ensure_env() {
  [ -x "$PY" ] || { say "▶ Creando entorno Python 3.12 (uv)…"; (cd "$ROOT/backend" && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt); }
  [ -f "$ROOT/frontend/dist/index.html" ] || { say "▶ Compilando frontend…"; (cd "$ROOT/frontend" && npm ci && npm run build); }
  mkdir -p "$DATA"
}
commit_label() { local c; c="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo "?")"; git -C "$ROOT" diff --quiet 2>/dev/null || c="$c+cambios"; echo "$c"; }
seed_db() {  # $1=ruta (debe llamarse demo_comercial*) — idempotente
  (cd "$ROOT/backend" && "$PY" -m app.demo_seed --db "$1") >/dev/null
}
start_server() {  # $1=db $2=pidfile $3=puerto inicial $4=etiqueta
  local db="$1" pidf="$2" want="$3" label="$4" port rp
  rp="$(running_port "$pidf")"
  if [ -n "$rp" ]; then say "• $label ya está corriendo en http://127.0.0.1:$rp (PID $(pid_from_file "$pidf")) — no se reinicia."; echo "$rp"; return; fi
  rm -f "$pidf"
  if [ -n "${DEMO_PORT:-}" ] && [ "$label" = "demo" ]; then
    [ -z "$(port_pid "$DEMO_PORT")" ] || die "DEMO_PORT=$DEMO_PORT está ocupado (por otro proceso: no lo toco). Elige otro o quita la variable."
    case " $RESERVED " in *" $DEMO_PORT "*) die "el puerto $DEMO_PORT está reservado (laboratorio/otro servicio)";; esac
    port="$DEMO_PORT"
  else port="$(free_port "$want")"; fi
  local provider="${AICOS_LLM_PROVIDER:-deterministic}"
  local logf="$DATA/$(basename "$db" .sqlite3).log"
  # exec => el PID guardado es el del propio uvicorn; stdout/stderr/stdin desligados para no bloquear al que nos llama
  ( cd "$ROOT/backend" && exec nohup env AICOS_DB_URL="sqlite:///data/$(basename "$db")" AICOS_DEMO_MODE=1 AICOS_LLM_PROVIDER="$provider" AICOS_STEP_DELAY="${AICOS_STEP_DELAY:-0.45}" AICOS_COMMIT="$(commit_label)" \
      "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" --no-proxy-headers ) >> "$logf" 2>&1 < /dev/null &
  echo "$! $port" > "$pidf"
  disown 2>/dev/null || true
  local i=0; until curl -s -o /dev/null "http://127.0.0.1:$port/api/demo/info"; do i=$((i+1)); [ $i -gt 60 ] && die "el servidor no arrancó; mira $logf"; sleep 0.5; done
  echo "$port"
}
banner() {  # $1=etiqueta $2=puerto $3=db
  local prov="${AICOS_LLM_PROVIDER:-deterministic}"
  say ""
  say "══════════════════════════════════════════════════════════════════════════"
  say "  AI COMPANY OS · $1 · datos 100 % ficticios · solo este Mac (127.0.0.1)"
  say "  Guía y accesos : http://127.0.0.1:$2/demo"
  say "  Base de datos  : backend/data/$(basename "$3")   (NO es aicos.db)"
  say "  Puerto / modo  : $2  ·  motor: $prov  ·  commit: $(commit_label)"
  say "  3 sesiones     : http://dueno.localhost:$2/demo-login/owner"
  say "                   http://admin.localhost:$2/demo-login/admin"
  say "                   http://tecnico.localhost:$2/demo-login/tech      (hosts distintos = cookies separadas)"
  say "  Laboratorio    : :8731 intacto (este script no lo toca)"
  say "══════════════════════════════════════════════════════════════════════════"
}

case "$CMD" in
  start)
    ensure_env
    [ "$(basename "$DB")" = "demo_comercial.sqlite3" ] || die "ruta de BD inesperada"
    seed_db "$DB"
    port="$(start_server "$DB" "$PID_MAIN" "${DEMO_PORT:-8743}" demo | tail -1)"
    banner "DEMO COMERCIAL" "$port" "$DB"
    (cd "$ROOT/backend" && "$PY" -m app.demo_preflight --db "$DB" --url "http://127.0.0.1:$port") || say "⚠ el preflight tiene fallos: revisa antes de presentar"
    say "  → Abre tú la guía en tu navegador: http://127.0.0.1:$port/demo   (este script no abre ni controla ningún navegador)"
    ;;
  stop)
    for f in "$PID_MAIN" "$PID_PLANB"; do
      pid="$(pid_from_file "$f")"
      if [ -n "$pid" ] && is_ours "$pid"; then kill "$pid" && say "✔ detenido PID $pid ($(basename "$f"))"; rm -f "$f"
      elif [ -f "$f" ]; then say "• $(basename "$f"): el proceso ya no existe o no es nuestro; solo limpio el archivo"; rm -f "$f"; fi
    done
    say "Laboratorio :8731 y demás servicios: no tocados."
    ;;
  status)
    for pair in "demo:$PID_MAIN:$DB" "plan B:$PID_PLANB:$PLANB_DB"; do
      label="${pair%%:*}"; rest="${pair#*:}"; f="${rest%%:*}"; db="${rest#*:}"
      rp="$(running_port "$f")"
      if [ -n "$rp" ]; then say "● $label  corriendo  http://127.0.0.1:$rp  (PID $(pid_from_file "$f"))  BD $(basename "$db")"; else say "○ $label  detenida  (BD: $( [ -f "$db" ] && echo "existe, $(( $(wc -c < "$db") / 1024 )) KB" || echo "no existe"))"; fi
    done
    say "▫ laboratorio :8731 → $( [ -n "$(port_pid 8731)" ] && echo "en marcha (PID $(port_pid 8731)); este script no lo toca" || echo "detenido")"
    ;;
  preflight)
    ensure_env; rp="$(running_port "$PID_MAIN")"
    (cd "$ROOT/backend" && "$PY" -m app.demo_preflight --db "$DB" ${rp:+--url "http://127.0.0.1:$rp"})
    ;;
  reset)
    ensure_env
    [ "$(basename "$DB")" = "demo_comercial.sqlite3" ] || die "ruta de BD inesperada"
    say "Esto RESTABLECE SOLO la base de la demo comercial:"; say "  $DB"; say "Hace una copia previa en backend/data/backups/. NO toca aicos.db ni el laboratorio :8731."
    if [ "$YES" != 1 ]; then printf 'Escribe REINICIAR-DEMO para confirmar: '; read -r ans; [ "$ans" = "REINICIAR-DEMO" ] || die "cancelado (no se cambió nada)"; fi
    was="$(running_port "$PID_MAIN")"
    [ -n "$was" ] && "$0" stop >/dev/null
    if [ -f "$DB" ]; then
      mkdir -p "$DATA/backups"; bk="$DATA/backups/demo_comercial-$(date +%Y%m%d%H%M%S).sqlite3"
      "$PY" - "$DB" "$bk" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()
PY
      say "✔ copia previa: ${bk#$ROOT/}"
    fi
    case "$DB" in */backend/data/demo_comercial.sqlite3) rm -f "$DB" "$DB-wal" "$DB-shm";; *) die "ruta de BD no permitida";; esac
    seed_db "$DB"; say "✔ base de la demo recreada con el dataset ficticio"
    if [ -n "$was" ]; then port="$(start_server "$DB" "$PID_MAIN" "$was" demo | tail -1)"; banner "DEMO COMERCIAL (restablecida)" "$port" "$DB"; fi
    (cd "$ROOT/backend" && "$PY" -m app.demo_preflight --db "$DB")
    ;;
  planb)
    ensure_env
    if [ ! -f "$PLANB_DB" ] || [ "$FORCE" = 1 ]; then
      [ -n "$(running_port "$PID_PLANB")" ] && "$0" stop >/dev/null
      rm -f "$PLANB_DB" "$PLANB_DB-wal" "$PLANB_DB-shm"
      say "▶ Ejecutando el guion completo (3 usuarios, API real) sobre la BD plan B…"
      (cd "$ROOT/backend" && "$PY" -m app.demo_autoplay --db "data/$(basename "$PLANB_DB")" 2>&1 | grep -v -i "warning\|from starlette" | grep -E '^ "(plan_id|final_status|forbidden_status|events|ok)"')
    fi
    port="$(start_server "$PLANB_DB" "$PID_PLANB" 8744 "plan B" | tail -1)"
    banner "PLAN B · ejecución ya terminada (real, persistida)" "$port" "$PLANB_DB"
    say "  Abre el visualizador del plan completado: http://dueno.localhost:$port/demo-login/owner?next=/coordinator   (elige el plan «Completada»)"
    ;;
  test)
    ensure_env
    (cd "$ROOT/backend" && "$PY" -m pytest tests/test_demo.py -q)
    (cd "$ROOT/frontend" && npx playwright test -c playwright.demo.config.ts)
    ;;
  *) die "uso: $0 [start|stop|status|preflight|reset|planb|test] [--yes] [--force]";;
esac
