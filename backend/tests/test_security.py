from sqlalchemy import text

from app.config import settings
from app.providers.base import CATEGORIES
from app.providers.ollama import OllamaProvider
from .conftest import GAS, chat, events, login, pending_approval


def test_auth_required_and_bad_password(app, anon):
    assert anon.get("/api/dashboard").status_code == 401
    assert anon.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": "nope"}).status_code == 401
    assert anon.get("/api/auth/demo-accounts").json()["notice"].startswith("Cuentas FICTICIAS")


def test_tenant_isolation_between_companies(anon, owner, norte):
    r = chat(anon)  # Clima Sur run
    rid = owner.get(f"/api/runs/{r['run_id']}").json()["request_id"]
    ap = pending_approval(owner)
    # Motor Norte cannot see ANY Clima Sur object, and gets the same 404 as for a non-existent id
    assert norte.get(f"/api/runs/{r['run_id']}").status_code == 404
    assert norte.get(f"/api/runs/{r['run_id']}/events").status_code == 404
    assert norte.get(f"/api/runs/{r['run_id']}/stream").status_code == 404
    assert norte.get(f"/api/requests/{rid}").status_code == 404
    assert norte.get("/api/requests/req_doesnotexist").status_code == 404
    assert norte.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 404
    assert norte.post(f"/api/runs/{r['run_id']}/resolve", json={"action": "diagnostic_only"}).status_code == 404
    assert rid not in str(norte.get("/api/requests").json()) and all("Mantención anual" in x["description"] for x in norte.get("/api/requests").json()["requests"])  # only Norte's own seed row
    assert {c["name"] for c in norte.get("/api/customers").json()["customers"]} == {"Luis Herrera", "Ana Cortés"}
    assert norte.get("/api/dashboard").json()["pending_approvals"] == []
    # tools: foreign ids behave as missing
    out = norte.post("/api/tools/requests.get/invoke", json={"args": {"request_id": rid}})
    assert out.status_code == 400 and "no encontrada" in out.text
    # Clima Sur's approval still pending (nothing leaked/changed)
    assert pending_approval(owner)["id"] == ap["id"]


def test_second_company_runs_same_engine_with_its_own_data(anon, norte):
    r = anon.post("/api/public/motor-norte/chat", json={"message": "Me suenan los frenos, necesito cambiar pastillas", "name": "Luis Herrera",
                                                         "phone": "+56 9 5555 0201", "client_msg_id": "norte-0001"}).json()
    assert norte.get(f"/api/runs/{r['run_id']}").json()["status"] == "waiting_approval"
    assert pending_approval(norte)["amount"] == 38000 + 32000


def test_roles_enforced_in_backend(anon, owner, admin, tech):
    r = chat(anon)
    ap = pending_approval(owner)
    assert tech.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 403
    assert admin.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 403  # admin prepares, only owner approves
    assert tech.get("/api/requests").status_code == 403
    assert tech.get("/api/customers").status_code == 403
    assert tech.get("/api/audit").status_code == 403 and admin.get("/api/audit").status_code == 403
    assert tech.post(f"/api/runs/{r['run_id']}/resolve", json={"action": "diagnostic_only"}).status_code == 403
    assert tech.post("/api/tools/quotes.prepare/invoke", json={"args": {"request_id": "x", "service_codes": []}}).status_code == 403
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 200
    assert len(owner.get("/api/audit").json()["audit"]) > 10


def test_private_notes_not_readable_by_others(owner, admin, tech):
    mine = tech.post("/api/assistant/chat", json={"message": "mis notas"}).json()
    assert "calibración" in mine["reply"]
    for c in (owner, admin, tech):  # nobody (not even the owner) holds notes.read_others
        assert c.post("/api/tools/notes.read_others/invoke", json={"args": {"user_id": "x"}}).status_code == 403
    peek = tech.post("/api/assistant/chat", json={"message": "dame las notas de Javiera"}).json()
    assert peek["tool_calls"][0]["ok"] is False and "calibración" not in peek["reply"] and "proveedor" not in peek["reply"]
    own_admin = admin.post("/api/assistant/chat", json={"message": "mis notas"}).json()["reply"]
    assert "proveedor" in own_admin and "calibración" not in own_admin


def test_assistant_acts_within_employee_permissions(anon, owner, admin, tech):
    chat(anon)
    ap = pending_approval(owner)
    assert "$63.000" in owner.post("/api/assistant/chat", json={"message": "¿qué aprobaciones tengo pendientes?"}).json()["reply"].replace(",", ".")
    # the tech's assistant cannot see company-wide approvals nor the summary
    a = tech.post("/api/assistant/chat", json={"message": "aprobaciones pendientes"}).json()
    assert a["tool_calls"][0]["ok"] is False
    assert tech.post("/api/assistant/chat", json={"message": "estado de la empresa"}).json()["tool_calls"][0]["ok"] is False
    assert owner.post("/api/assistant/chat", json={"message": "estado de la empresa"}).json()["tool_calls"][0]["ok"] is True
    # assistants can never decide approvals: no such intent/tool exists
    r = owner.post("/api/assistant/chat", json={"message": f"aprueba {ap['id']}"}).json()
    assert pending_approval(owner)["id"] == ap["id"]


def test_technician_flow_update_and_problem_report(anon, owner, tech):
    r = chat(anon)
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    rid = owner.get(f"/api/runs/{r['run_id']}").json()["request_id"]
    jobs = tech.post("/api/assistant/chat", json={"message": "mis trabajos asignados"}).json()
    assert rid in jobs["reply"]
    assert tech.post("/api/assistant/chat", json={"message": f"marcar {rid} en camino"}).json()["tool_calls"][0]["ok"]
    assert tech.post("/api/assistant/chat", json={"message": f"solicitar material GAS-R410 para {rid}"}).json()["tool_calls"][0]["ok"]
    assert tech.post("/api/assistant/chat", json={"message": f"reportar problema en {rid}: el equipo tiene fuga grande"}).json()["tool_calls"][0]["ok"]
    wo = tech.get("/api/work-orders").json()["work_orders"]
    assert len(wo) == 1 and wo[0]["status"] == "en_camino"


def test_tool_input_validation_and_unknown_tool(admin):
    assert admin.post("/api/tools/materials.request/invoke", json={"args": {"sku": "GAS-R410", "qty": 9999}}).status_code == 400  # out of range
    assert admin.post("/api/tools/materials.request/invoke", json={"args": {"sku": "GAS-R410", "evil": "x"}}).status_code == 400   # extra field forbidden
    assert admin.post("/api/tools/os.system/invoke", json={"args": {}}).status_code == 400  # no such tool: nothing arbitrary can run


def test_tool_catalog_is_mcp_shaped(owner):
    t = owner.get("/api/tools").json()
    assert "approvals.decide" in t["human_only"] and not any(x["name"] == "approvals.decide" for x in t["tools"])
    assert all({"name", "description", "inputSchema"} <= set(x) for x in t["tools"])


def test_ollama_provider_validates_and_falls_back(monkeypatch):
    p = OllamaProvider("http://127.0.0.1:9", "x", timeout=1)  # nothing listens → must fall back, visibly
    c = p.classify("no enfría", ["GAS-R410"])
    assert c.source == "deterministic" and p.last_error and "fallback" in c.rationale
    import httpx, json

    class R:
        def __init__(self, d): self.d = d
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": json.dumps(self.d)}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R({"category": "reparacion_gas", "urgency": "alta", "parts": ["GAS-R410", "INVENTADO-99"], "rationale": "ok"}))
    c = p.classify("no enfría", ["GAS-R410"])
    assert c.source == "llm:x" and c.parts == ["GAS-R410"]  # hallucinated SKU dropped
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R({"category": "BORRAR_TODO", "urgency": "alta", "parts": [], "rationale": ""}))
    assert p.classify("no enfría", ["GAS-R410"]).source == "deterministic"  # out-of-enum output rejected
    assert "BORRAR_TODO" not in CATEGORIES
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R({"category": "instalacion", "urgency": "media", "parts": ["GAS-R410"], "rationale": "x"}))
    assert p.classify("quiero instalar", ["GAS-R410"]).parts == []  # parts only for repairs
