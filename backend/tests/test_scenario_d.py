from fastapi.testclient import TestClient

from sqlalchemy import text

from app.api import create_app
from app.config import settings
from .conftest import chat, events, login, pending_approval

MANT = {"message": "Hola, quiero agendar una mantención preventiva de mi split del living.", "name": "Marta Rojas", "phone": "+56 9 5555 0101"}
TECH_MSG = "necesito dos cilindros de gas para la visita de Marta antes de ir"


def types(c, run):
    return [e["type"] for e in events(c, run)]


def start_d(anon, owner, mid="d-0001"):
    r = chat(anon, body=MANT, mid=mid, secret=f"{mid}-secreto-abcdefghijk")      # Issue #20: un reintento posterior solo recupera la conversación con el mismo secreto
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "waiting_approval" and run["scenario"] == "D"
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "waiting_technician"  # paused for a PERSON (the technician), not completed
    return r["run_id"], run["request_id"]


def tech_asks(tech, msg=TECH_MSG):
    return tech.post("/api/assistant/chat", json={"message": msg}).json()


def stock(app, sku="GAS-R410", slug="clima-sur"):
    with app.state.eng.connect() as c:
        return c.execute(text("SELECT stock FROM parts p JOIN companies co ON co.id=p.company_id WHERE co.slug=:s AND p.sku=:k"), {"s": slug, "k": sku}).scalar()


def count(app, table, where="1=1"):
    with app.state.eng.connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar()


def full_d_until_approval(anon, owner, tech):
    run_id, rid = start_d(anon, owner)
    r = tech_asks(tech)
    assert r["tool_calls"][0]["ok"], r
    return run_id, rid


def test_d_full_collaboration_with_evidence(app, anon, owner, admin, tech):
    run_id, rid = full_d_until_approval(anon, owner, tech)
    run = owner.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "waiting_approval"
    # persisted, correlated collaboration objects
    assert [(d["from_agent"], d["to_agent"]) for d in run["delegations"] if d["from_agent"] == "tech_assistant"] == [("tech_assistant", "admin_assistant")]
    mat_task = next(t for t in run["tasks"] if t["kind"] == "material")
    assert mat_task["assignee"] == "Javiera Soto" and mat_task["status"] == "pendiente"
    assert {m["title"] for m in run["commitments"]} >= {"Informar el material necesario antes de la visita", "Confirmar la visita con el cliente", "Entregar GAS-R410 x2 al técnico antes de la visita"}
    ap = pending_approval(owner)
    assert ap["amount"] == 36000 and "Material" in ap["subject"] if "subject" in ap else True
    # admin cannot close the task before the owner approved the spend (no resolved-without-evidence)
    assert admin.post(f"/api/tasks/{mat_task['id']}/complete", json={"evidence": "ya lo entregué en bodega"}).status_code == 400
    assert stock(app) == 12
    owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"})
    run = owner.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "waiting_task" and run["materials"][0]["status"] == "aprobado"
    assert run["approvals"][-1]["decided_by"] == "Carolina Vega"
    # only the assigned person can complete it, and only with real evidence
    assert owner.post(f"/api/tasks/{mat_task['id']}/complete", json={"evidence": "yo lo entrego, listo"}).status_code in (400, 403)  # owner has no tasks.complete
    assert tech.post(f"/api/tasks/{mat_task['id']}/complete", json={"evidence": "intento ajeno de cierre"}).status_code == 400
    assert admin.post(f"/api/tasks/{mat_task['id']}/complete", json={"evidence": "corto"}).status_code == 422
    assert stock(app) == 12
    ok = admin.post("/api/assistant/chat", json={"message": f"completar tarea {mat_task['id']} evidencia: entregué 2 cilindros de gas al técnico en bodega"}).json()
    assert ok["tool_calls"][0]["ok"] and "stock GAS-R410 −2" in ok["reply"]
    assert stock(app) == 10  # REAL effect, once
    again = admin.post(f"/api/tasks/{mat_task['id']}/complete", json={"evidence": "reintento duplicado de la entrega"}).json()
    assert again["deduplicated"] is True and stock(app) == 10
    run = owner.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "waiting_task"  # now waits for the technician's reception
    recv = [t for t in tech.get("/api/tasks").json()["tasks"] if t["kind"] == "recepcion"]
    assert len(recv) == 1 and recv[0]["assignee"] == "Matías Fuentes"
    assert admin.post(f"/api/tasks/{recv[0]['id']}/complete", json={"evidence": "no es mi tarea, intento"}).status_code == 400
    done = tech.post(f"/api/tasks/{recv[0]['id']}/complete", json={"evidence": "recibí los 2 cilindros y revisé el sello"}).json()
    assert done["effect"] == "material recibido por el técnico"
    run = owner.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "completed" and run["results"][-1]["kind"] == "material_entregado_y_recibido"
    assert {m["status"] for m in run["materials"]} == {"recibido"}
    assert all(c["status"] in ("cumplido", "abierto") for c in run["commitments"])
    assert [c["status"] for c in run["commitments"] if c["title"].startswith(("Informar", "Entregar"))] == ["cumplido", "cumplido"]
    d = owner.get(f"/api/requests/{rid}").json()
    assert d["visit"]["customer_confirmed"] == 0 and d["work_order"]["status"] == "material_listo"  # visit still NOT confirmed
    kinds = [m["status"] for m in d["messages"]]
    assert kinds.count("preparado_no_enviado") == 2 and set(kinds) <= {"recibido", "enviado_chat", "preparado_no_enviado"}  # visit proposal + material notice, nothing sent externally
    t = types(owner, run_id)
    for needed in ("commitment.created", "task.assigned", "task.completed", "agent.delegated", "approval.requested", "approval.decided", "run.waiting_human", "run.completed"):
        assert needed in t, needed
    ev = events(owner, run_id)
    assert any(e["type"] == "agent.delegated" and e["agent"] == "tech_assistant" and e["data"]["to"] == "admin_assistant" for e in ev)
    assert "collab.request_material" in [e["data"]["tool"] for e in ev if e["type"] == "tool.called" and e["agent"] == "tech_assistant"]


def test_d_double_requests_do_not_duplicate(app, anon, owner, tech):
    run_id, rid = full_d_until_approval(anon, owner, tech)
    again = tech_asks(tech)  # same request twice
    assert "No se duplicó nada" in again["reply"]
    assert count(app, "material_requests") == 1 and count(app, "tasks", "kind='material'") == 1 and count(app, "approvals", "subject LIKE 'Material%'") == 1
    ap = pending_approval(owner)
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).json()["deduplicated"] is False
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).json()["deduplicated"] is True
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "rejected"}).status_code == 409
    assert count(app, "tasks", "kind='recepcion'") == 0 and stock(app) == 12
    late = tech_asks(tech, "necesito un motor de ventilador para la visita")  # run no longer waits for the technician
    assert late["tool_calls"] == [] and "No se duplicó nada" in late["reply"]
    assert count(app, "material_requests") == 1


def test_d_owner_rejects_material(app, anon, owner, admin, tech):
    run_id, rid = full_d_until_approval(anon, owner, tech)
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "rejected", "comment": "muy caro"})
    run = owner.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "completed" and run["results"][-1]["kind"] == "material_rechazado"
    assert stock(app) == 12 and run["materials"][0]["status"] == "rechazado"
    mat = next(t for t in run["tasks"] if t["kind"] == "material")
    assert mat["status"] == "cancelada"
    assert admin.post(f"/api/tasks/{mat['id']}/complete", json={"evidence": "intento tras el rechazo"}).status_code == 400
    assert stock(app) == 12
    assert any(t["kind"] == "aviso" for t in tech.get("/api/tasks").json()["tasks"])  # technician is told in his own session
    d = owner.get(f"/api/requests/{rid}").json()
    assert [m for m in d["messages"] if "material adicional" in m["body"]] == []  # no customer notice about a rejected spend


def test_d_shortage_blocks_and_resolutions(app, anon, owner, tech):
    run_id, rid = start_d(anon, owner)
    r = tech_asks(tech, "necesito COMPRESOR para la visita de Marta")
    assert r["tool_calls"][0]["ok"]
    run = owner.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "blocked" and {a["id"] for a in run["alternatives"]} == {"reschedule_after_restock", "proceed_without_material"}
    assert any(t["kind"] == "reposicion" for t in run["tasks"]) and run["approvals"][-1]["status"] == "approved"  # no new spend approval requested
    assert owner.post(f"/api/runs/{run_id}/resolve", json={"action": "reschedule_after_restock"}).status_code == 200
    assert owner.post(f"/api/runs/{run_id}/resolve", json={"action": "reschedule_after_restock"}).status_code == 409
    run = owner.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "completed" and run["results"][-1]["kind"] == "reprogramacion_pendiente"
    assert sorted(c["status"] for c in run["commitments"] if c["title"] != "Entregar") .count("cancelado") == 2
    assert stock(app, "COMPRESOR") == 0


def test_commitments_process_due_is_idempotent_and_cancellable(app, anon, owner, admin, tech):
    run_id, rid = full_d_until_approval(anon, owner, tech)
    assert tech.post("/api/commitments/process-due", json={}).status_code == 403
    before = count(app, "tasks", "kind='recordatorio'")
    assert owner.post("/api/commitments/process-due", json={"as_of": "2000-01-01T00:00:00+00:00"}).json()["processed"] == []  # nothing due yet (fixed past clock: the real clock made this test fail after 09:00 UTC)
    far = "2099-01-01T00:00:00+00:00"
    p1 = owner.post("/api/commitments/process-due", json={"as_of": far}).json()["processed"]
    assert len(p1) == 3 and count(app, "tasks", "kind='recordatorio'") == before + 3
    assert [x for x in p1 if x["followup_draft"]] and count(app, "messages", "idem_key LIKE 'fu:%'") == 1  # customer-facing one only
    p2 = owner.post("/api/commitments/process-due", json={"as_of": far}).json()["processed"]
    assert p2 == [] and count(app, "tasks", "kind='recordatorio'") == before + 3 and count(app, "messages", "idem_key LIKE 'fu:%'") == 1  # reentrant
    assert count(app, "messages", "idem_key LIKE 'fu:%' AND status='preparado_no_enviado'") == 1  # drafted, NOT sent
    ev = types(owner, run_id)
    assert ev.count("reminder.created") == 3 and "followup.drafted" in ev
    # the customer really confirms → pending reminder + draft for that commitment no longer apply
    box = anon.get(f"/api/public/clima-sur/outbox?token={chat(anon, body=MANT, mid='d-0001', secret='d-0001-secreto-abcdefghijk')['conversation_token']}").json()
    code = [m for m in box["messages"] if "Código de confirmación" in m["body"]][0]["body"].split("confirmación: ")[1].strip(".")
    assert anon.post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": code}).json()["status"] == "confirmada"
    assert count(app, "messages", "idem_key LIKE 'fu:%' AND status='cancelado'") == 1
    assert count(app, "tasks", "kind='recordatorio' AND status='cancelada'") == 1
    assert count(app, "commitments", "title='Confirmar la visita con el cliente' AND status='cumplido'") == 1


def test_reassign_cancels_obsolete_reminders(app, anon, owner, admin, tech):
    run_id, rid = full_d_until_approval(anon, owner, tech)
    owner.post("/api/commitments/process-due", json={"as_of": "2099-01-01T00:00:00+00:00"})
    team = owner.get("/api/team").json()["users"]
    adm, own = next(u for u in team if u["role"] == "admin"), next(u for u in team if u["role"] == "owner")
    cm = next(c for c in owner.get("/api/commitments").json()["commitments"] if c["title"] == "Confirmar la visita con el cliente")
    assert tech.post(f"/api/commitments/{cm['id']}/reassign", json={"new_owner_user_id": own["id"]}).status_code == 403
    r = owner.post(f"/api/commitments/{cm['id']}/reassign", json={"new_owner_user_id": own["id"]}).json()
    assert r["reminders_cancelled"] == 1
    assert owner.post(f"/api/commitments/{cm['id']}/reassign", json={"new_owner_user_id": own["id"]}).json()["deduplicated"] is True
    p = owner.post("/api/commitments/process-due", json={"as_of": "2099-01-01T00:00:00+00:00"}).json()["processed"]
    assert len(p) == 1 and p[0]["commitment_id"] == cm["id"]  # regenerated once, for the NEW owner
    assert count(app, "tasks", f"commitment_id='{cm['id']}' AND status='pendiente'") == 1


def test_isolation_and_personal_scoping_for_v03(anon, owner, admin, tech, norte):
    run_id, rid = full_d_until_approval(anon, owner, tech)
    mat = next(t for t in owner.get(f"/api/runs/{run_id}").json()["tasks"] if t["kind"] == "material")
    assert norte.get("/api/commitments").json()["commitments"] == [] and norte.get("/api/tasks").json()["tasks"] == []
    assert norte.post(f"/api/tasks/{mat['id']}/complete", json={"evidence": "otra empresa intenta cerrar"}).status_code == 400
    assert norte.post(f"/api/commitments/x/reassign", json={"new_owner_user_id": "x"}).status_code == 400
    assert norte.get(f"/api/runs/{run_id}").status_code == 404
    mine = tech.get("/api/commitments").json()["commitments"]
    assert mine and all(c["owner"] == "Matías Fuentes" for c in mine)  # technician: own commitments only
    assert all(t["assignee"] == "Matías Fuentes" for t in tech.get("/api/tasks").json()["tasks"])
    assert tech.post("/api/tools/materials.update_status/invoke", json={"args": {"material_request_id": "x", "status": "aprobado"}}).status_code == 403
    att = owner.get("/api/attention").json()["items"]
    assert any(i["kind"] == "approval" for i in att)
    assert not any(i["kind"] == "approval" for i in tech.get("/api/attention").json()["items"])
    assert norte.get("/api/attention").json()["items"] == []


def test_state_survives_server_restart_mid_collaboration(dbfile, app, anon, owner, tech):
    run_id, rid = start_d(anon, owner)
    app2 = create_app(dbfile)  # restart while waiting for the technician
    tech2, owner2 = login(app2, "tecnico@climasur.test"), login(app2, "dueno@climasur.test")
    assert owner2.get(f"/api/runs/{run_id}").json()["status"] == "waiting_technician"
    assert tech_asks(tech2)["tool_calls"][0]["ok"]
    app3 = create_app(dbfile)  # restart again while waiting for the owner's approval
    owner3, admin3, tech3 = login(app3, "dueno@climasur.test"), login(app3, "admin@climasur.test"), login(app3, "tecnico@climasur.test")
    owner3.post(f"/api/approvals/{pending_approval(owner3)['id']}/decide", json={"decision": "approved"})
    mat = next(t for t in admin3.get("/api/tasks").json()["tasks"] if t["kind"] == "material")
    admin3.post(f"/api/tasks/{mat['id']}/complete", json={"evidence": "entrega registrada tras reinicio"})
    recv = next(t for t in tech3.get("/api/tasks").json()["tasks"] if t["kind"] == "recepcion")
    tech3.post(f"/api/tasks/{recv['id']}/complete", json={"evidence": "recibido tras segundo reinicio"})
    assert owner3.get(f"/api/runs/{run_id}").json()["status"] == "completed"
    assert stock(app3) == 10


def test_no_automatic_or_manual_retry_exists(anon, owner):
    r = chat(anon)
    assert owner.post(f"/api/runs/{r['run_id']}/retry", json={}).status_code in (404, 405)


def test_failed_run_needs_review_in_attention(anon, owner, monkeypatch):
    from app.tools.registry import REGISTRY
    monkeypatch.setattr(REGISTRY["quotes.prepare"], "handler", lambda ctx, a: (_ for _ in ()).throw(RuntimeError("boom")))
    chat(anon, mid="d-fail-001")
    item = next(i for i in owner.get("/api/attention").json()["items"] if i["kind"] == "failed")
    assert "requiere revisión" in item["text"] and "boom" not in str(item)


def test_scoped_reset_requires_confirmation_and_keeps_other_company(app, anon, owner, norte, tech):
    full_d_until_approval(anon, owner, tech)
    anon.post("/api/public/motor-norte/chat", json={"message": "cambio de aceite", "name": "Luis Herrera", "phone": "+56 9 5555 0201", "client_msg_id": "norte-reset-1"})
    assert tech.post("/api/demo/reset-scenarios", json={"confirm": "REINICIAR"}).status_code == 403
    assert owner.post("/api/demo/reset-scenarios", json={"confirm": "si"}).status_code == 400
    assert count(app, "runs") == 2
    r = owner.post("/api/demo/reset-scenarios", json={"confirm": "REINICIAR"}).json()
    assert r["runs"] == 1
    assert count(app, "runs") == 1 and stock(app) == 12  # Clima Sur cleared, Motor Norte's run kept
    assert count(app, "users") == 6 and count(app, "customers", "name='Marta Rojas'") == 1  # people and seed customers survive
    assert norte.get("/api/runs").json()["runs"][0]["status"] in ("waiting_approval", "running")


def test_schema_migration_keeps_data(dbfile, app, owner):
    with app.state.eng.begin() as c:
        c.execute(text("ALTER TABLE tasks DROP COLUMN evidence"))
        c.execute(text("DROP TABLE commitments"))
    n = count(app, "requests")
    app2 = create_app(dbfile)  # additive migration on startup
    assert count(app2, "requests") == n
    with app2.state.eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM pragma_table_info('tasks') WHERE name='evidence'")).scalar() == 1
        assert c.execute(text("SELECT COUNT(*) FROM commitments")).scalar() == 0


def test_llm_material_parsing_is_verified(monkeypatch):
    import httpx, json
    from app.providers.base import validate_material_need
    from app.providers.ollama import OllamaProvider
    cat, mine = [{"sku": "GAS-R410", "name": "Gas"}], [{"id": "req_aaaaaaaaaaaa", "customer": "Marta"}]

    class R:
        def __init__(self, d): self.d = d
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": json.dumps(self.d)}}
    p = OllamaProvider("http://x", "m")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R({"sku": "GAS-R410", "qty": 2, "request_id": "req_aaaaaaaaaaaa", "reason": "x"}))
    assert validate_material_need(p.parse_material_need("dos gas", cat, mine), cat, mine, "necesito dos botellas de gas").qty == 2
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R({"sku": "INVENTADO", "qty": 2, "request_id": "req_aaaaaaaaaaaa", "reason": "x"}))
    assert validate_material_need(p.parse_material_need("x", cat, mine), cat, mine) is None  # invented SKU
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R({"sku": "GAS-R410", "qty": 999, "request_id": "req_aaaaaaaaaaaa", "reason": "x"}))
    assert validate_material_need(p.parse_material_need("x", cat, mine), cat, mine) is None  # absurd quantity
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R({"sku": "GAS-R410", "qty": 1, "request_id": "req_otraempresa1", "reason": "x"}))
    assert validate_material_need(p.parse_material_need("x", cat, mine), cat, mine).request_id == "req_aaaaaaaaaaaa"  # foreign id replaced by the technician's only order


def test_llm_pick_must_be_grounded_in_the_technician_text(monkeypatch):
    """Regression (real qwen run): 'necesito una pizza' made both models pick GAS-R410 because the schema forced a choice."""
    import httpx, json
    from app.providers.base import MaterialNeed, validate_material_need
    cat = [{"sku": "GAS-R410", "name": "Gas refrigerante R410A (carga)"}, {"sku": "MOTOR-VENT", "name": "Motor ventilador unidad exterior"}]
    mine = [{"id": "req_aaaaaaaaaaaa", "customer": "Marta"}]
    pick = MaterialNeed("GAS-R410", 1, "req_aaaaaaaaaaaa", "", "llm:x")
    assert validate_material_need(pick, cat, mine, "necesito una pizza para almorzar") is None
    assert validate_material_need(pick, cat, mine, "necesito el refrigerante para la carga") is not None  # paraphrase grounded by the part name
    assert validate_material_need(pick, cat, mine, "llévame GAS-R410") is not None
    det = MaterialNeed("GAS-R410", 1, "req_aaaaaaaaaaaa", "", "deterministic")
    assert validate_material_need(det, cat, mine, "lo que sea") is not None  # deterministic parser already matched on the text itself


def test_db_seeded_by_older_version_gets_new_agent_permissions(dbfile, app):
    """Regression (found live on the Mac): a V0.2-seeded DB had no commitments.write for the coordinator → D failed with Denied."""
    import json
    with app.state.eng.begin() as c:
        for role in ("coordinator", "admin_assistant", "tech_assistant"):
            row = c.execute(text("SELECT id,permissions FROM agents WHERE role=:r"), {"r": role}).first()
            old = [p for p in json.loads(row[1]) if p not in ("commitments.write", "commitments.read", "tasks.complete", "materials.manage")]
            c.execute(text("UPDATE agents SET permissions=:p WHERE role=:r"), {"p": json.dumps(old), "r": role})
    app2 = create_app(dbfile)  # startup syncs permissions from code
    anon2, owner2, tech2 = TestClient(app2), login(app2, "dueno@climasur.test"), login(app2, "tecnico@climasur.test")
    r = chat(anon2, body=MANT, mid="upgrade-d-001")
    owner2.post(f"/api/approvals/{pending_approval(owner2)['id']}/decide", json={"decision": "approved"})
    assert owner2.get(f"/api/runs/{r['run_id']}").json()["status"] == "waiting_technician"  # would be 'failed' without the sync
    assert tech_asks(tech2)["tool_calls"][0]["ok"]
