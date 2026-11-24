import inspect

from sqlalchemy import text

from app.api import create_app
from .conftest import GAS, PLACA, chat, events, login, pending_approval


def types(evs):
    return [e["type"] for e in evs]


def test_happy_path_to_prepared_proposal(anon, owner, app):
    r = chat(anon)
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "waiting_approval"  # paused for HUMAN approval, not auto-approved
    evs = events(owner, r["run_id"])
    t = types(evs)
    assert t[0] == "run.started" and "approval.requested" in t and t[-1] == "run.waiting_approval"
    agents_seen = {e["agent"] for e in evs if e["type"] == "agent.activated"}
    assert agents_seen == {"reception", "coordinator", "admin_assistant", "owner_assistant"}
    tools = [e["data"]["tool"] for e in evs if e["type"] == "tool.called"]
    for needed in ["customers.find", "customers.history", "requests.create", "requests.classify", "availability.technicians",
                   "pricing.lookup", "inventory.check", "quotes.prepare", "approvals.request"]:
        assert needed in tools
    # reception identified the EXISTING customer and saw real history
    hist = next(e for e in evs if e["data"].get("tool") == "customers.history" and e["type"] == "tool.result")
    assert len(hist["data"]["output"]["requests"]) == 1
    ap = pending_approval(owner)
    assert ap["amount"] == 45000 + 18000  # real prices from the DB: labour + R410 gas
    d = owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"})
    assert d.status_code == 200
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "completed"
    detail = owner.get(f"/api/requests/{run['request_id']}").json()
    assert detail["request"]["status"] == "propuesta_preparada"
    assert detail["visit"]["status"] == "propuesta" and detail["visit"]["customer_confirmed"] == 0  # NOT confirmed
    assert detail["work_order"]["status"] == "pendiente_confirmacion_cliente"
    out = [m for m in detail["messages"] if m["status"] == "preparado_no_enviado"]
    assert len(out) == 1 and "NO está reservada" in out[0]["body"]
    assert detail["results"][0]["kind"] == "propuesta_preparada"
    assert {t["title"] for t in detail["tasks"]} >= {"Esperar confirmación del cliente para la visita"}
    evs = events(owner, r["run_id"])
    assert types(evs)[-1] == "run.completed" and evs[-1]["data"]["customer_confirmed"] is False


def test_customer_must_really_confirm(anon, owner, app):
    r = chat(anon)
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    rid = owner.get(f"/api/runs/{r['run_id']}").json()["request_id"]
    assert anon.post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": "WRONG1"}).status_code == 400
    assert owner.get(f"/api/requests/{rid}").json()["visit"]["status"] == "propuesta"
    box = anon.get(f"/api/public/clima-sur/outbox?token={r['conversation_token']}").json()
    code = [m for m in box["messages"] if m["status"] == "preparado_no_enviado"][0]["body"].split("confirmación: ")[1].strip(".")
    assert anon.post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": code}).json()["status"] == "confirmada"
    assert anon.post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": code}).json()["deduplicated"] is True
    d = owner.get(f"/api/requests/{rid}").json()
    assert d["visit"]["customer_confirmed"] == 1 and d["request"]["status"] == "visita_confirmada"
    assert "customer.confirmed" in types(events(owner, r["run_id"]))


def test_rejection_prepares_alternative(anon, owner):
    r = chat(anon)
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "rejected", "comment": "muy caro"})
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "completed"
    d = owner.get(f"/api/requests/{run['request_id']}").json()
    assert d["request"]["status"] == "alternativa_preparada" and d["visit"] is None  # no visit for a rejected quote
    assert {q["kind"]: q["status"] for q in d["quotes"]} == {"principal": "rechazado", "alternativa": "borrador"}
    assert d["results"][0]["kind"] == "rechazado_con_alternativa"
    assert events(owner, r["run_id"])[-1]["data"]["outcome"] == "rechazado_con_alternativa"


def test_missing_part_blocks_then_recovers(anon, owner):
    r = chat(anon, body=PLACA, mid="msg-block-001")
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "blocked"
    assert {a["id"] for a in run["alternatives"]} == {"use_alternative_part", "diagnostic_only"}
    evs = events(owner, r["run_id"])
    assert any(e["type"] == "agent.blocked" and "PLACA-INV" in e["title"] for e in evs)
    assert owner.get("/api/dashboard").json()["alerts"][0]["kind"] == "blocked"
    assert owner.post(f"/api/runs/{r['run_id']}/resolve", json={"action": "bogus"}).status_code == 400
    assert owner.post(f"/api/runs/{r['run_id']}/resolve", json={"action": "use_alternative_part"}).status_code == 200
    assert owner.post(f"/api/runs/{r['run_id']}/resolve", json={"action": "use_alternative_part"}).status_code == 409  # no double resume
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "waiting_approval"
    ap = pending_approval(owner)
    assert ap["amount"] == 55000 + 98000  # labour + universal board (alt part)
    owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"})
    assert owner.get(f"/api/runs/{r['run_id']}").json()["status"] == "completed"


def test_persistence_across_restart(anon, owner, dbfile):
    r = chat(anon)
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    n = len(events(owner, r["run_id"]))
    app2 = create_app(dbfile)  # "restart": brand-new app on the same DB file
    c2 = login(app2, "dueno@climasur.test")
    assert len(events(c2, r["run_id"])) == n and c2.get(f"/api/runs/{r['run_id']}").json()["status"] == "completed"
    assert any(x["id"] == r["run_id"] for x in c2.get("/api/runs").json()["runs"])


def test_retries_do_not_duplicate(anon, owner, app):
    a = chat(anon, mid="same-msg-001", secret="retry-secret-same-msg-001-xyz")      # Issue #20: un reintento demuestra posesión con el secreto que generó el cliente
    b = chat(anon, mid="same-msg-001", secret="retry-secret-same-msg-001-xyz")
    assert b["deduplicated"] is True and a["run_id"] == b["run_id"]
    ap = pending_approval(owner)
    assert len(owner.get("/api/dashboard").json()["pending_approvals"]) == 1
    r1 = owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).json()
    r2 = owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).json()
    assert r1["deduplicated"] is False and r2["deduplicated"] is True
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "rejected"}).status_code == 409
    with app.state.eng.connect() as c:
        n = lambda t: c.execute(text(f"SELECT COUNT(*) FROM {t} WHERE idem_key LIKE 'chat:%' OR 1=0")).scalar() if t == "requests" else c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        assert n("requests") == 1 and n("quotes") == 1 and n("approvals") == 1 and n("visits") == 1 and n("work_orders") == 1
        assert c.execute(text("SELECT COUNT(*) FROM tasks")).scalar() == 2


def test_tools_idempotent_directly(admin, anon, owner):
    r = chat(anon)
    rid = owner.get(f"/api/runs/{r['run_id']}").json()["request_id"]
    body = {"args": {"request_id": rid, "kind": "principal", "service_codes": ["DIAG"], "part_skus": []}}
    again = admin.post("/api/tools/quotes.prepare/invoke", json=body).json()
    assert again["deduplicated"] is True and again["total"] == 63000  # returns the ORIGINAL quote, no duplicate
    m1 = admin.post("/api/tools/materials.request/invoke", json={"args": {"sku": "COMPRESOR", "request_id": rid}}).json()
    m2 = admin.post("/api/tools/materials.request/invoke", json={"args": {"sku": "COMPRESOR", "request_id": rid}}).json()
    assert m1["material_request_id"] == m2["material_request_id"] and m2["deduplicated"]


def test_llm_never_acts_and_modes_are_labelled(anon, owner):
    r = chat(anon)
    evs = events(owner, r["run_id"])
    cl = next(e for e in evs if e["type"] == "tool.result" and e["data"]["tool"] == "requests.classify")
    assert cl["mode"] == "deterministic" and cl["data"]["output"]["source"] == "deterministic"
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    evs = events(owner, r["run_id"])
    msg = next(e for e in evs if e["type"] == "tool.result" and e["data"]["tool"] == "messages.prepare_customer")
    assert msg["mode"] == "simulated"
    assert not any(e["data"].get("tool") == "approvals.decide" for e in evs)  # agents have no way to approve


def test_new_customer_is_created_once(anon, owner):
    body = {"message": "El ventilador de la unidad exterior hace ruido y vibra", "name": "Camila Torres", "phone": "+56 9 5555 0199"}
    r = chat(anon, body=body, mid="new-cust-001")
    assert owner.get(f"/api/runs/{r['run_id']}").json()["status"] == "waiting_approval"
    r2 = chat(anon, body=body, mid="new-cust-002")  # second message from the same phone → same customer, not a duplicate
    names = [c["name"] for c in owner.get("/api/customers").json()["customers"]]
    assert names.count("Camila Torres") == 1
    assert owner.get(f"/api/runs/{r2['run_id']}").json()["status"] == "waiting_approval"
    ev = events(owner, r["run_id"])
    assert any(e["type"] == "tool.result" and e["data"]["tool"] == "customers.create" and e["data"]["output"]["created"] for e in ev)


def test_flow_call_accepts_any_tool_argument_name():
    """Regression: Flow.call(role, tool, name=...) raised 'multiple values for argument name' for new customers."""
    from app.engine.orchestrator import Flow
    params = inspect.signature(Flow.call).parameters
    assert params["role"].kind is inspect.Parameter.POSITIONAL_ONLY and params["tool_name"].kind is inspect.Parameter.POSITIONAL_ONLY
    assert params["args"].kind is inspect.Parameter.VAR_KEYWORD  # tool args (incl. `name`, `role`, `tool_name`) go through **args


def test_new_customer_through_http_creates_customer_and_request(anon, owner):
    r = chat(anon, body={"message": "El ventilador exterior hace ruido y vibra", "name": "Camila Torres", "phone": "+56 9 5555 0199"}, mid="regress-new-01")
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "waiting_approval" and run["request_id"] != "-"
    d = owner.get(f"/api/requests/{run['request_id']}").json()
    assert d["customer"]["name"] == "Camila Torres" and d["history"] == []  # brand-new customer, no history


def test_failed_run_is_not_shown_as_healthy_or_closed(anon, owner, monkeypatch, caplog):
    """A crash mid-run keeps the traceback in the event log and flags the request (never 'cerrada'/'nueva')."""
    from app.tools.registry import REGISTRY

    def boom(ctx, a):
        raise RuntimeError("fallo inyectado")
    monkeypatch.setattr(REGISTRY["quotes.prepare"], "handler", boom)
    r = chat(anon, mid="regress-fail-01")
    run = owner.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "failed"
    fail = events(owner, r["run_id"])[-1]
    assert fail["type"] == "run.failed" and fail["data"]["phase"] == "intake" and fail["data"]["error_class"] == "RuntimeError"
    assert "traceback" not in fail["data"] and "fallo inyectado" not in str(fail)  # operational users only see a safe summary
    assert "RuntimeError: fallo inyectado" in caplog.text and "Traceback" in caplog.text  # full trace only in the server log
    assert owner.get(f"/api/requests/{run['request_id']}").json()["request"]["status"] == "ejecucion_fallida"
    dash = owner.get("/api/dashboard").json()
    assert dash["requests_by_status"].get("ejecucion_fallida") == 1
    assert dash["requests_by_status"].get("cerrada") == 1  # only the seeded 2025 history row, never the failed one
    assert any(a["kind"] == "failed" and a["run_id"] == r["run_id"] for a in dash["alerts"])
    assert dash["pending_approvals"] == []
