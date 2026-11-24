#!/usr/bin/env bash
# Starts an ISOLATED audit server (port 8742) on a COPY of the real SQLite DB. The real DB (port 8731) is never opened for writing.
# usage: qa/start_audit_server.sh [fresh-copy|keep]   (default fresh-copy: re-copy the real DB first)  — env AICOS_LLM_PROVIDER passes through
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT/backend"
PORT=8742; MODE="${1:-fresh-copy}"
OLD=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t || true)
if [ -n "$OLD" ]; then CWD=$(lsof -a -p "$OLD" -d cwd -Fn | tail -1); case "$CWD" in *ai-company-os/backend) kill "$OLD"; sleep 1;; *) echo "port $PORT used by a foreign process ($CWD) — refusing"; exit 2;; esac; fi
if [ "$MODE" = "fresh-copy" ]; then
  rm -f data/audit.db data/audit.db-wal data/audit.db-shm
  .venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect("file:data/aicos.db?mode=ro", uri=True); dst = sqlite3.connect("data/audit.db"); src.backup(dst); dst.close(); src.close()
print("audit.db = copia consistente de aicos.db (la base real no se modifica)")
PY
fi
AICOS_STATIC_DIR="${AICOS_STATIC_DIR:-$ROOT/frontend/${AICOS_UI_DIR:-dist-v05}}" AICOS_DB_URL=sqlite:///data/audit.db AICOS_STEP_DELAY=${AICOS_STEP_DELAY:-0.12} nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $PORT --no-proxy-headers >> "${AUDIT_LOG:-/tmp/aicos-audit.log}" 2>&1 &
for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:$PORT/api/auth/demo-accounts && break; sleep 0.5; done
echo "audit server up on :$PORT (pid $(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t))"
