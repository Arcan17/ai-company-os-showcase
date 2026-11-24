#!/usr/bin/env python3
"""Evidence for the QA report: (1) SQLite rows of the audited plan, read-only from the audit copy; (2) a LIVE SSE capture against the audit server
(a fresh plan: frames are timestamped as the actions happen). usage: backend/.venv/bin/python qa/evidence.py <PLAN_ID>"""
import json, os, sqlite3, sys, threading, time, uuid
import httpx
from qa_client import csrf_login
PLAN = sys.argv[1]
DB_PATH = os.environ.get("AICOS_QA_DB", "backend/data/audit.db"); OUT_DIR = os.environ.get("AICOS_QA_OUT", "docs/qa"); os.makedirs(OUT_DIR, exist_ok=True)
db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True); db.row_factory = sqlite3.Row
def rows(sql): return [dict(r) for r in db.execute(sql).fetchall()]
out = [f"# Evidencia SQLite — plan `{PLAN}` (copia de auditoría de la base real; solo lectura)\n"]
def table(title, data, cols):
    out.append(f"\n## {title}\n"); out.append("| " + " | ".join(cols) + " |"); out.append("|" + "---|" * len(cols))
    for r in data: out.append("| " + " | ".join(str(r.get(c, ""))[:90].replace("|", "/").replace("\n", " ") for c in cols) + " |")
p = rows(f"select id,company_id,created_by,status,source,intent,request_id,rounds,confirmed_by,confirmed_at,created_at,updated_at from plans where id='{PLAN}'")
co = rows(f"select slug from companies where id='{p[0]['company_id']}'")[0]["slug"]; who = rows(f"select name,role,email from users where id='{p[0]['created_by']}'")[0]
conf = rows(f"select name,role from users where id='{p[0]['confirmed_by']}'")[0]
out.append(f"Empresa: **{co}** · solicitó: **{who['name']} ({who['role']})** · confirmó: **{conf['name']} ({conf['role']})**")
table("plans", p, ["id", "status", "source", "intent", "request_id", "rounds", "confirmed_at", "updated_at"])
table("plan_steps", rows(f"select s.seq,s.action,s.agent,u.name as responsable,s.depends_on,s.access,s.permission,s.origin,s.status,s.task_id from plan_steps s left join users u on u.id=s.assignee_user_id where s.plan_id='{PLAN}' order by s.seq"),
      ["seq", "action", "agent", "responsable", "depends_on", "access", "permission", "origin", "status", "task_id"])
table("tasks (run_id = plan)", rows(f"select t.id,t.kind,t.status,u.name as responsable,cu.name as completada_por,t.completed_at,t.evidence,t.idem_key from tasks t left join users u on u.id=t.assignee_user_id left join users cu on cu.id=t.completed_by where t.run_id='{PLAN}' order by t.created_at"),
      ["id", "kind", "status", "responsable", "completada_por", "completed_at", "evidence", "idem_key"])
table("delegations", rows(f"select id,from_agent,to_agent,status,idem_key from delegations where run_id='{PLAN}'"), ["id", "from_agent", "to_agent", "status", "idem_key"])
table("results", rows(f"select kind,summary from results where run_id='{PLAN}'"), ["kind", "summary"])
ids = ",".join(f"'{r['id']}'" for r in rows(f"select id from tasks where run_id='{PLAN}'"))
table("audit_log (cierres y rechazos de las tareas del plan)", rows(f"select ts,actor_type,action,resource,outcome,detail from audit_log where resource in ({ids}) or (action='tasks.complete' and outcome='error' and ts>='{p[0]['created_at']}') order by id"), ["ts", "actor_type", "action", "resource", "outcome", "detail"])
table("audit_log (plans.*)", rows(f"select ts,actor_type,action,resource,outcome,detail from audit_log where resource='{PLAN}' or action like 'plans.%' and ts>='{p[0]['created_at']}' order by id limit 20"), ["ts", "actor_type", "action", "resource", "outcome", "detail"])
table("events (run_id = plan; fuente única del visualizador)", rows(f"select seq,type,agent,mode,title from events where run_id='{PLAN}' order by seq"), ["seq", "type", "agent", "mode", "title"])
open(f"{OUT_DIR}/evidence-sqlite.md", "w").write("\n".join(out) + "\n")
print(f"{OUT_DIR}/evidence-sqlite.md escrito")

# ---- live SSE capture on a fresh plan (frames timestamped as actions happen)
B = os.environ.get("AICOS_QA_URL", "http://127.0.0.1:8742")
def login(e): return csrf_login(httpx.Client(base_url=B, timeout=30), e)   # keeps the session's CSRF token and sends it on every write
o, a = login("dueno@climasur.test"), login("admin@climasur.test")
rid = rows("select r.id from requests r join customers c on c.id=r.customer_id where c.name='Marta Rojas' and r.category='mantencion' and r.status='propuesta_preparada' limit 1")[0]["id"]
pl = o.post("/api/plans", json={"text": f"Pide a Javiera que verifique los materiales de {rid} (captura SSE)", "client_plan_id": "sse-" + uuid.uuid4().hex[:8]}).json()["plan"]
frames, t0 = [], time.time()
def listen():
    with httpx.stream("GET", f"{B}/api/plans/{pl['id']}/stream", cookies=o.cookies, timeout=30) as r:
        ev = {}
        for line in r.iter_lines():
            if line.startswith("id:"): ev["id"] = line[3:].strip()
            elif line.startswith("event:"): ev["event"] = line[6:].strip()
            elif line.startswith("data:"):
                d = json.loads(line[5:]); frames.append((round(time.time() - t0, 2), d["seq"], d["type"], d["agent"], d["mode"], d["title"][:70])); ev = {}
            if time.time() - t0 > 11: return
th = threading.Thread(target=listen, daemon=True); th.start(); time.sleep(1.0)
marks = [(round(time.time() - t0, 2), "ACCIÓN: dueña confirma el plan")]; o.post(f"/api/plans/{pl['id']}/confirm"); time.sleep(2.5)
tid = rows(f"select id from tasks where run_id='{pl['id']}'")[0]["id"]
marks.append((round(time.time() - t0, 2), "ACCIÓN: Javiera completa su tarea con evidencia")); a.post(f"/api/tasks/{tid}/complete", json={"evidence": "Captura SSE: verificado el inventario durante la auditoría"}); time.sleep(3.5); th.join(timeout=8)
lines = ["# Captura SSE EN VIVO — `/api/plans/{id}/stream` (auditoría, servidor :8742)", "", f"Plan `{pl['id']}`. Cada fila es un frame SSE recibido; `t` = segundos desde que se abrió el stream. Las filas ACCIÓN marcan cuándo se ejecutó cada acción humana.", "",
         "| t (s) | seq | type | agente | modo | título |", "|---|---|---|---|---|---|"]
allrows = sorted([(x[0], "frame", x[1:]) for x in frames] + [(t, "mark", m) for t, m in marks], key=lambda x: x[0])
for t, kind, f in allrows:
    lines.append(f"| {t} | | **{f}** | | | |" if kind == "mark" else f"| {t} | {f[0]} | {f[1]} | {f[2]} | {f[3]} | {f[4].replace('|', '/')} |")
dbn = rows(f"select count(*) n from events where run_id='{pl['id']}'")[0]["n"]
lines += ["", f"Frames SSE recibidos: **{len(frames)}** · filas en `events` para el plan: **{dbn}** · {'COINCIDEN' if len(frames) == dbn else 'NO COINCIDEN'}"]
open(f"{OUT_DIR}/evidence-sse.md", "w").write("\n".join(lines) + "\n"); print(f"{OUT_DIR}/evidence-sse.md escrito:", len(frames), "frames vs", dbn, "filas")
