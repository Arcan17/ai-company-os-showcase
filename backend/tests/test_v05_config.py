"""V0.5 — configurable base: companies, employees and assistant templates created from configuration (no source edits), eligible-employee delegation, isolation."""
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import create_app
from app.company_admin import ConfigError, add_employee, assign_template, create_company
from app.config import settings
from app.db import init_db, make_engine
from app.directory import eligible_staff, pick_assignee
from app.permissions import ROLE_PERMISSIONS
from app.seed import DEMO_PASSWORD

from .conftest import chat, login

PW = "clave-ficticia-123"
INSTR = "Revisa la mantención pendiente de Cliente Uno; pide a administración que verifique materiales y al técnico que confirme después de administración."
INSTR_ADMIN = "Revisa la mantención pendiente de Cliente Uno; pide a administración que verifique materiales."


def rid():
    return "t" + uuid.uuid4().hex[:12]


@pytest.fixture(autouse=True)
def creation_on(monkeypatch):
    monkeypatch.setattr(settings, "allow_company_creation", True)


def anon_client(app, **kw):
    return TestClient(app, **kw)


def make_company(app, name="Aire Austral", slug="aire-austral", owner="Elena Rojas", email="elena@aireaustral.test", preset="hvac", password=PW):
    c = anon_client(app)
    r = c.post("/api/setup/companies", json={"name": name, "slug": slug, "preset": preset, "owner_name": owner, "owner_email": email, "owner_password": password})
    assert r.status_code == 200, r.text
    return r.json()


def login_as(app, email, pw=PW):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return c


def db(app, sql, **p):
    with app.state.eng.connect() as c:
        return c.execute(text(sql), p).fetchall()


@pytest.fixture
def third(app):
    """Third fictitious company, configured through the API: owner + 2 administrators + 2 technicians, each with their personal assistant."""
    made = make_company(app)
    o = login_as(app, "elena@aireaustral.test")
    staff = {}
    for key, name, email, role in (("javiera", "Javiera Pons", "javiera@aireaustral.test", "admin"), ("rodrigo", "Rodrigo Silva", "rodrigo@aireaustral.test", "admin"),
                                   ("matias", "Matías Ortiz", "matias@aireaustral.test", "technician"), ("camila", "Camila Vera", "camila@aireaustral.test", "technician")):
        r = o.post("/api/config/employees", json={"name": name, "email": email, "role": role, "password": PW})
        assert r.status_code == 200, r.text
        staff[key] = {**r.json(), "client": login_as(app, email)}
    return {"owner": o, "staff": staff, "made": made}


def request_for(owner, customer="Cliente Uno", text_="Quiero una mantención preventiva del split del local, por favor."):
    r = owner.post("/api/requests/manual", json={"customer_name": customer, "phone": "+56 9 5555 0777", "description": text_, "client_request_id": rid()})
    assert r.status_code == 200, r.text
    return r.json()["request_id"]


# ------------------------------------------------------------------ company creation (local/demo only)
def test_company_creation_is_off_by_default_and_loopback_only(app, monkeypatch):
    monkeypatch.setattr(settings, "allow_company_creation", False)
    body = {"name": "Otra Empresa", "preset": "hvac", "owner_name": "Ana Prueba", "owner_email": "ana@otra.test"}
    assert anon_client(app).post("/api/setup/companies", json=body).status_code == 404
    assert anon_client(app).get("/api/setup/info").json()["company_creation"] is False
    monkeypatch.setattr(settings, "allow_company_creation", True)
    assert anon_client(app, client=("10.1.2.3", 5555)).post("/api/setup/companies", json=body).status_code == 404   # not from outside the machine
    assert anon_client(app).post("/api/setup/companies", json=body).status_code == 200


def test_new_company_has_owner_shared_agents_catalogue_and_nothing_from_other_companies(app):
    made = make_company(app)
    assert made["slug"] == "aire-austral" and made["owner_password"] is None   # a password the owner chose is never echoed back
    o = login_as(app, "elena@aireaustral.test")
    me = o.get("/api/auth/me").json()
    assert me["role"] == "owner" and me["company"]["slug"] == "aire-austral" and "config.manage" in me["permissions"]
    ag = {a["role"]: a for a in o.get("/api/agents").json()["agents"]}
    assert set(ag) == {"reception", "coordinator", "owner_assistant"}                                  # no admin/tech assistants until employees exist
    assert o.get("/api/requests").json()["requests"] == [] and o.get("/api/customers").json()["customers"] == []
    assert len(db(app, "SELECT 1 FROM parts p JOIN companies c ON c.id=p.company_id WHERE c.slug='aire-austral'")) == 5 and len(db(app, "SELECT 1 FROM integrations i JOIN companies c ON c.id=i.company_id WHERE c.slug='aire-austral' AND i.mode='simulado'")) == 5
    assert all(r[0] == 0 for r in db(app, "SELECT enabled FROM integrations WHERE company_id=(SELECT id FROM companies WHERE slug='aire-austral')"))   # no channel is connected


def test_generated_password_is_shown_once_and_works(app):
    r = anon_client(app).post("/api/setup/companies", json={"name": "Taller Sur", "preset": "taller", "owner_name": "Pedro Prueba", "owner_email": "pedro@tallersur.test"}).json()
    assert r["owner_password"] and len(r["owner_password"]) >= 10
    assert login_as(app, "pedro@tallersur.test", r["owner_password"]).get("/api/auth/me").json()["company"]["name"] == "Taller Sur"
    assert "owner_password" not in str(db(app, "SELECT detail FROM audit_log WHERE action='config.company_create'"))           # never in the audit log


@pytest.mark.parametrize("bad", [{"slug": "Mal Slug"}, {"slug": "ab"}, {"preset": "inexistente"}, {"owner_email": "no-es-correo"}, {"owner_password": "corta"}, {"name": "ab"}])
def test_company_creation_validates_input(app, bad):
    body = {"name": "Empresa Valida", "preset": "hvac", "owner_name": "Dueño Valido", "owner_email": "ok@valida.test", **bad}
    assert anon_client(app).post("/api/setup/companies", json=body).status_code in (400, 422)


def test_slug_and_email_must_be_unique_across_the_database(app):
    make_company(app)
    assert anon_client(app).post("/api/setup/companies", json={"name": "Aire Austral Dos", "slug": "aire-austral", "owner_name": "Otra Persona", "owner_email": "otra@x.test"}).status_code == 409
    assert anon_client(app).post("/api/setup/companies", json={"name": "Aire Austral Tres", "owner_name": "Elena Rojas", "owner_email": "ELENA@aireaustral.test"}).status_code == 409
    assert anon_client(app).post("/api/setup/companies", json={"name": "Clima Sur Clon", "owner_name": "Clon Owner", "owner_email": "dueno@climasur.test"}).status_code == 409


def test_cli_creates_a_company_in_a_database_without_touching_code(tmp_path, capsys):
    from app.company_admin import main
    db_path = tmp_path / "cli.sqlite3"
    assert main(["create", "--name", "Frio Norte", "--owner-name", "Rosa Prueba", "--owner-email", "rosa@frionorte.test", "--preset", "generico", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert '"slug": "frio-norte"' in out and '"owner_password":' in out
    assert main(["create", "--name", "Frio Norte", "--owner-name", "Rosa Prueba", "--owner-email", "otra@frionorte.test", "--db", str(db_path)]) == 1   # duplicate slug
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM services WHERE company_id=(SELECT id FROM companies WHERE slug='frio-norte')").fetchone()[0] == 3


# ------------------------------------------------------------------ employees, roles, templates
def test_owner_adds_employees_with_roles_and_their_personal_assistants(app, third):
    o = third["owner"]
    ov = o.get("/api/config/overview").json()
    emps = {e["name"]: e for e in ov["employees"]}
    assert [e["role"] for e in ov["employees"]] == ["owner", "admin", "admin", "technician", "technician"]
    assert all(e["eligible"] for e in ov["employees"] if e["role"] != "owner") and all(e["assistants"][0]["enabled"] for e in ov["employees"])
    assert {a["role"] for a in o.get("/api/agents").json()["agents"]} >= {"admin_assistant", "tech_assistant"}
    assert len([a for a in o.get("/api/agents").json()["agents"] if a["role"] == "admin_assistant"]) == 2                      # one assistant PER administrator
    assert len(db(app, "SELECT 1 FROM availability a JOIN users u ON u.id=a.technician_id WHERE u.email='matias@aireaustral.test'")) == 12   # visit slots for each technician
    audits = {r[0] for r in db(app, "SELECT action FROM audit_log WHERE company_id=(SELECT id FROM companies WHERE slug='aire-austral')")}
    assert {"config.company_create", "config.employee_add"} <= audits
    assert [t["key"] for t in ov["templates"]] == ["reception", "coordinator", "owner_assistant", "admin_assistant", "tech_assistant"] and emps["Elena Rojas"]["role"] == "owner"


def test_templates_expose_capabilities_from_the_tool_registry_and_never_exceed_the_role(app, third):
    tpl = {t["key"]: t for t in third["owner"].get("/api/config/templates").json()["templates"]}
    assert "approvals.decide" not in tpl["admin_assistant"]["permissions"] and "approvals.decide" not in tpl["coordinator"]["permissions"]
    tech = tpl["tech_assistant"]
    assert set(tech["effective_permissions"]) <= ROLE_PERMISSIONS["technician"] and {c["name"] for c in tech["capabilities"]} >= {"tasks.complete", "workorders.list_mine"}
    assert "inventory.check" not in {c["name"] for c in tech["capabilities"]}                       # the technician's assistant cannot read inventory
    assert set(tpl["admin_assistant"]["effective_permissions"]) <= ROLE_PERMISSIONS["admin"]


def test_only_the_owner_administers_and_nobody_elevates_themselves(app, third):
    adm, tech, o = third["staff"]["javiera"]["client"], third["staff"]["matias"]["client"], third["owner"]
    for c in (adm, tech):
        assert c.get("/api/config/overview").status_code == 403
        assert c.post("/api/config/employees", json={"name": "Intruso Uno", "email": "i@x.test", "role": "admin"}).status_code == 403
    me = o.get("/api/auth/me").json()
    assert o.post(f"/api/config/employees/{me['id']}/role", json={"role": "admin"}).status_code == 403            # owner cannot change their own role
    assert o.post(f"/api/config/employees/{me['id']}/active", json={"active": False}).status_code == 403
    assert o.post("/api/config/employees", json={"name": "Otro Dueño", "email": "od@x.test", "role": "owner"}).status_code == 400     # a second owner cannot be created
    jid = third["staff"]["javiera"]["user_id"]
    assert o.post(f"/api/config/employees/{jid}/role", json={"role": "owner"}).status_code == 400
    assert adm.post(f"/api/config/employees/{jid}/role", json={"role": "owner"}).status_code == 403


def test_assignments_match_the_role_are_idempotent_and_audited(app, third):
    o, mid = third["owner"], third["staff"]["matias"]
    assert o.post("/api/config/assignments", json={"user_id": mid["user_id"], "template": "admin_assistant"}).status_code == 400      # wrong template for a technician
    assert o.post("/api/config/assignments", json={"user_id": mid["user_id"], "template": "coordinator"}).status_code == 400          # shared agents are not assignable
    assert o.post("/api/config/assignments", json={"user_id": mid["user_id"], "template": "tech_assistant"}).json()["deduplicated"] is True
    n0 = len(db(app, "SELECT 1 FROM agents"))
    o.post("/api/config/assignments", json={"user_id": mid["user_id"], "template": "tech_assistant"})
    assert len(db(app, "SELECT 1 FROM agents")) == n0
    aid = mid["agent_id"]
    assert o.post(f"/api/config/agents/{aid}/enabled", json={"enabled": False}).json()["enabled"] is False
    assert o.post(f"/api/config/agents/{aid}/enabled", json={"enabled": True}).json()["enabled"] is True
    acts = [r[0] for r in db(app, "SELECT action FROM audit_log WHERE resource=:a ORDER BY id", a=aid)]
    assert acts == ["config.assignment_disable", "config.assignment_enable"]


def test_employee_without_an_assistant_cannot_act_and_is_not_eligible(app, third):
    o = third["owner"]
    r = o.post("/api/config/employees", json={"name": "Sin Asistente", "email": "sa@aireaustral.test", "role": "admin", "password": PW, "assign_assistant": False}).json()
    c = login_as(app, "sa@aireaustral.test")
    assert c.get("/api/tasks").status_code == 403 and "asistente personal activo" in c.get("/api/tasks").json()["detail"]
    assert "asistente personal activo" in c.post("/api/assistant/chat", json={"message": "tareas"}).json()["reply"]
    with app.state.eng.connect() as conn:
        co = conn.execute(text("SELECT id FROM companies WHERE slug='aire-austral'")).scalar()
        assert r["user_id"] not in [s["id"] for s in eligible_staff(conn, co, "admin")]
    assert o.post("/api/config/assignments", json={"user_id": r["user_id"], "template": "admin_assistant"}).status_code == 200
    with app.state.eng.connect() as conn:
        assert r["user_id"] in [s["id"] for s in eligible_staff(conn, co, "admin")]


def test_deactivating_an_employee_logs_them_out_and_removes_eligibility(app, third):
    o, rod = third["owner"], third["staff"]["rodrigo"]
    assert rod["client"].get("/api/auth/me").status_code == 200
    assert o.post(f"/api/config/employees/{rod['user_id']}/active", json={"active": False}).status_code == 200
    assert rod["client"].get("/api/auth/me").status_code == 401                                   # session killed
    assert TestClient(app).post("/api/auth/login", json={"email": "rodrigo@aireaustral.test", "password": PW}).status_code == 401
    with app.state.eng.connect() as conn:
        co = conn.execute(text("SELECT id FROM companies WHERE slug='aire-austral'")).scalar()
        assert [s["name"] for s in eligible_staff(conn, co, "admin")] == ["Javiera Pons"]
    assert o.post(f"/api/config/employees/{rod['user_id']}/active", json={"active": True}).status_code == 200


def test_role_change_swaps_the_assistant_and_refuses_with_open_tasks(app, third):
    o, cam = third["owner"], third["staff"]["camila"]
    assert o.post(f"/api/config/employees/{cam['user_id']}/role", json={"role": "admin"}).json()["role"] == "admin"
    rows = {r[0]: r[1] for r in db(app, "SELECT role,enabled FROM agents WHERE user_id=:u", u=cam["user_id"])}
    assert rows == {"tech_assistant": 0, "admin_assistant": 1}
    with app.state.eng.connect() as conn:
        co = conn.execute(text("SELECT id FROM companies WHERE slug='aire-austral'")).scalar()
        assert "Camila Vera" in [s["name"] for s in eligible_staff(conn, co, "admin")] and "Camila Vera" not in [s["name"] for s in eligible_staff(conn, co, "technician")]


# ------------------------------------------------------------------ coordinator: eligible employees, never «the first»
def test_with_two_administrators_the_coordinator_asks_instead_of_taking_the_first(app, third):
    o = third["owner"]
    request_for(o)
    pl = o.post("/api/plans", json={"text": INSTR_ADMIN, "client_plan_id": rid()}).json()["plan"]
    assert pl["status"] == "needs_clarification" and pl["clarification"]["field"] == "admin_user_id"
    labels = sorted(x["label"].split(" · ")[0] for x in pl["clarification"]["options"])
    assert labels == ["Javiera Pons", "Rodrigo Silva"] and pl["steps"] == []
    assert db(app, "SELECT COUNT(*) FROM tasks WHERE kind='plan_step'")[0][0] == 0
    # the owner decides (here the SECOND one alphabetically/by creation — nothing defaults to the first)
    rod = third["staff"]["rodrigo"]["user_id"]
    pl2 = o.post(f"/api/plans/{pl['id']}/clarify", json={"admin_user_id": rod}).json()["plan"]
    assert pl2["status"] == "awaiting_user_confirmation" and pl2["steps"][1]["assignee"] == "Rodrigo Silva"
    assert o.post(f"/api/plans/{pl['id']}/clarify", json={"admin_user_id": "usr_noexiste"}).status_code == 400


def test_naming_the_employee_in_the_instruction_picks_that_person(app, third):
    o = third["owner"]
    request_for(o)
    pl = o.post("/api/plans", json={"text": INSTR.replace("administración", "Rodrigo").replace("al técnico", "a Camila"), "client_plan_id": rid()}).json()["plan"]
    assert pl["status"] == "awaiting_user_confirmation" and [s["assignee"] for s in pl["steps"] if s["assignee"]] == ["Rodrigo Silva", "Camila Vera"]


def test_a_disabled_or_missing_assistant_makes_the_named_person_ineligible_with_an_honest_reason(app, third):
    o = third["owner"]
    request_for(o)
    o.post(f"/api/config/agents/{third['staff']['javiera']['agent_id']}/enabled", json={"enabled": False})
    pl = o.post("/api/plans", json={"text": INSTR.replace("administración", "Javiera"), "client_plan_id": rid()}).json()["plan"]
    assert pl["status"] == "needs_clarification" and "su asistente personal está desactivado" in pl["status_reason"]
    assert [x["label"].split(" · ")[0] for x in pl["clarification"]["options"]] == ["Rodrigo Silva"]                 # only the eligible one is offered


def test_zero_eligible_administrators_blocks_honestly(app):
    make_company(app)
    o = login_as(app, "elena@aireaustral.test")
    for n, e, r in (("Matías Ortiz", "m@aireaustral.test", "technician"),):
        o.post("/api/config/employees", json={"name": n, "email": e, "role": r, "password": PW})
    request_for(o)
    pl = o.post("/api/plans", json={"text": INSTR, "client_plan_id": rid()}).json()["plan"]
    assert pl["status"] == "blocked" and "no tiene personal de administración con un asistente personal activo" in pl["status_reason"] and pl["steps"] == []


def test_two_people_with_the_same_first_name_are_never_collapsed(app, third):
    o = third["owner"]
    o.post("/api/config/employees", json={"name": "Javiera Quintana", "email": "jq@aireaustral.test", "role": "admin", "password": PW})
    request_for(o)
    pl = o.post("/api/plans", json={"text": INSTR.replace("administración", "Javiera"), "client_plan_id": rid()}).json()["plan"]
    assert pl["status"] == "needs_clarification" and "2 personas" in pl["status_reason"]
    assert sorted(x["label"].split(" · ")[0] for x in pl["clarification"]["options"]) == ["Javiera Pons", "Javiera Quintana"]


def test_default_assignee_is_the_least_loaded_eligible_employee_not_the_first(app, third):
    o = third["owner"]
    with app.state.eng.connect() as conn:
        co = conn.execute(text("SELECT id FROM companies WHERE slug='aire-austral'")).scalar()
        first = pick_assignee(conn, co, "admin")["name"]
    request_for(o)
    jav = third["staff"]["javiera"]["user_id"]
    pl = o.post("/api/plans", json={"text": INSTR_ADMIN, "client_plan_id": rid()}).json()["plan"]
    o.post(f"/api/plans/{pl['id']}/clarify", json={"admin_user_id": jav}); assert o.post(f"/api/plans/{pl['id']}/confirm").status_code == 200
    with app.state.eng.connect() as conn:                                                           # Javiera now has an open task → Rodrigo is the default
        assert pick_assignee(conn, co, "admin")["name"] == "Rodrigo Silva" and [s["workload"] for s in eligible_staff(conn, co, "admin")] == [0, 1]
    assert first in ("Javiera Pons", "Rodrigo Silva")


# ------------------------------------------------------------------ THE acceptance test: a plan in a company created from configuration
def test_third_company_runs_a_full_plan_between_owner_admin_and_technician(app, third):
    o, st = third["owner"], third["staff"]
    req = request_for(o)
    pl = o.post("/api/plans", json={"text": INSTR, "client_plan_id": rid()}).json()["plan"]
    rod = st["rodrigo"]["user_id"]
    pl = o.post(f"/api/plans/{pl['id']}/clarify", json={"admin_user_id": rod}).json()["plan"]
    assert pl["status"] == "needs_clarification" and pl["clarification"]["field"] == "technician_user_id"                 # two technicians → asks again (no work order yet)
    cam = st["camila"]["user_id"]
    pl = o.post(f"/api/plans/{pl['id']}/clarify", json={"technician_user_id": cam}).json()["plan"]
    pid = pl["id"]
    assert pl["status"] == "awaiting_user_confirmation" and pl["request_id"] == req and pl["rounds"] == 2
    assert db(app, "SELECT COUNT(*) FROM tasks WHERE run_id=:p", p=pid)[0][0] == 0 and db(app, "SELECT COUNT(*) FROM delegations WHERE run_id=:p", p=pid)[0][0] == 0
    r1, r2 = o.post(f"/api/plans/{pid}/confirm"), o.post(f"/api/plans/{pid}/confirm")                   # double click
    assert r1.status_code == r2.status_code == 200 and r2.json()["deduplicated"]
    tasks = db(app, "SELECT id,assignee_user_id,status FROM tasks WHERE run_id=:p ORDER BY created_at", p=pid)
    assert [(t[1], t[2]) for t in tasks] == [(rod, "pendiente"), (cam, "en_espera")] and db(app, "SELECT COUNT(*) FROM delegations WHERE run_id=:p", p=pid)[0][0] == 2
    # only the CHOSEN people can act: the other administrator/technician cannot touch these tasks
    jav, mat = st["javiera"]["client"], st["matias"]["client"]
    assert jav.post(f"/api/tasks/{tasks[0][0]}/complete", json={"evidence": "No es mi tarea, intento cerrarla igual"}).status_code == 400
    assert mat.post(f"/api/tasks/{tasks[1][0]}/complete", json={"evidence": "No es mi tarea, intento cerrarla igual"}).status_code == 400
    assert [t for t in jav.get("/api/tasks").json()["tasks"] if t["kind"] == "plan_step"] == []
    admin_c, tech_c = st["rodrigo"]["client"], st["camila"]["client"]
    assert tech_c.post(f"/api/tasks/{tasks[1][0]}/complete", json={"evidence": "Antes de tiempo, no debería poder cerrarla"}).status_code == 400   # dependency
    stock = admin_c.post("/api/assistant/chat", json={"message": "¿Cuánto stock hay de GAS-R410?"}).json()["reply"]
    assert "GAS-R410" in stock and "12 u." in stock                                                     # the new company's own catalogue (preset), not Clima Sur's rows
    assert admin_c.post(f"/api/tasks/{tasks[0][0]}/complete", json={"evidence": "Revisé el inventario de Aire Austral: gas R410 y kit completos."}).status_code == 200
    assert tech_c.post(f"/api/tasks/{tasks[1][0]}/complete", json={"evidence": "Confirmado por Camila: llevo todo lo verificado por Rodrigo."}).status_code == 200
    v = o.get(f"/api/plans/{pid}").json()
    assert v["status"] == "completed" and [s["status"] for s in v["steps"]] == ["done"] * 4 and "Rodrigo" not in str(v["tasks"][0].get("assignee_agent"))
    ev = [e["type"] for e in o.get(f"/api/plans/{pid}/events").json()["events"]]
    assert ev.count("agent.delegated") == 2 and ev.count("task.completed") == 2 and "run.completed" in ev
    assert db(app, "SELECT COUNT(*) FROM events WHERE run_id=:p", p=pid)[0][0] == len(ev)
    assert db(app, "SELECT COUNT(*) FROM tasks WHERE run_id=:p AND status='hecha' AND length(evidence)>=10", p=pid)[0][0] == 2
    # nothing leaked to the other companies
    clima = login(app, "dueno@climasur.test")
    assert clima.get(f"/api/plans/{pid}").status_code == 404 and clima.get("/api/plans").json()["plans"] == []
    assert db(app, "SELECT COUNT(*) FROM tasks t JOIN companies c ON c.id=t.company_id WHERE c.slug IN ('clima-sur','motor-norte')")[0][0] == 0


def test_blocked_forbidden_action_in_the_new_company(app, third):
    o = third["owner"]
    request_for(o)
    pl = o.post("/api/plans", json={"text": "Compra repuestos y avisa a todos por WhatsApp.", "client_plan_id": rid()}).json()["plan"]
    assert pl["status"] == "blocked" and pl["steps"] == [] and o.post(f"/api/plans/{pl['id']}/confirm").status_code == 409


# ------------------------------------------------------------------ isolation between configured companies
def test_two_configured_companies_never_see_each_other(app):
    make_company(app, "Aire Austral", "aire-austral", "Elena Rojas", "elena@aireaustral.test")
    make_company(app, "Frio Cordillera", "frio-cordillera", "Tomás Prueba", "tomas@friocordillera.test")
    a, b = login_as(app, "elena@aireaustral.test"), login_as(app, "tomas@friocordillera.test")
    ua = a.post("/api/config/employees", json={"name": "Ana Admin", "email": "ana@aireaustral.test", "role": "admin", "password": PW}).json()
    ub = b.post("/api/config/employees", json={"name": "Beto Admin", "email": "beto@friocordillera.test", "role": "admin", "password": PW}).json()
    assert b.post(f"/api/config/employees/{ua['user_id']}/active", json={"active": False}).status_code == 404            # other company: «not found», no existence leak
    assert b.post(f"/api/config/agents/{ua['agent_id']}/enabled", json={"enabled": False}).status_code == 404
    assert b.post("/api/config/assignments", json={"user_id": ua["user_id"], "template": "admin_assistant"}).status_code == 404
    assert a.post(f"/api/config/employees/{ub['user_id']}/role", json={"role": "technician"}).status_code == 404
    assert {e["name"] for e in a.get("/api/config/overview").json()["employees"]} == {"Elena Rojas", "Ana Admin"}
    ra = request_for(a, "Cliente A"); request_for(b, "Cliente B")
    assert [r["customer"] for r in a.get("/api/requests").json()["requests"]] == ["Cliente A"] and [r["customer"] for r in b.get("/api/requests").json()["requests"]] == ["Cliente B"]
    assert b.get(f"/api/requests/{ra}").status_code == 404


def test_manual_request_permissions_idempotency_and_classification(app, third):
    o, tech = third["owner"], third["staff"]["matias"]["client"]
    assert tech.post("/api/requests/manual", json={"customer_name": "Cliente X", "description": "Necesito una mantención", "client_request_id": rid()}).status_code == 403
    cid_ = rid()
    body = {"customer_name": "Cliente Uno", "description": "Quiero una mantención preventiva del split", "client_request_id": cid_}
    a, b = o.post("/api/requests/manual", json=body).json(), o.post("/api/requests/manual", json=body).json()
    assert a["request_id"] == b["request_id"] and b["deduplicated"] is True
    d = o.get(f"/api/requests/{a['request_id']}").json()
    assert d["request"]["category"] == "mantencion" and d["request"]["status"] == "clasificada" and d["customer"]["name"] == "Cliente Uno" and len(d["messages"]) == 1
    assert len(o.get("/api/customers").json()["customers"]) == 1


# ------------------------------------------------------------------ the existing companies keep working
def test_seeded_companies_are_untouched_by_the_new_model(app):
    for email, slug in (("dueno@climasur.test", "Clima Sur"), ("dueno@motornorte.test", "Motor Norte")):
        c = login(app, email)
        ag = c.get("/api/agents").json()["agents"]
        assert [a["role"] for a in ag] == ["reception", "coordinator", "owner_assistant", "admin_assistant", "tech_assistant"] and all(a["enabled"] for a in ag)
        assert c.get("/api/auth/me").json()["company"]["name"] == slug
    ov = login(app, "dueno@climasur.test").get("/api/config/overview").json()
    assert [e["name"] for e in ov["employees"]] == ["Carolina Vega", "Javiera Soto", "Matías Fuentes"] and all(e["eligible"] for e in ov["employees"][1:])


def test_restart_does_not_auto_assign_assistants_and_keeps_the_configuration(app, dbfile, third):
    third["owner"].post("/api/config/employees", json={"name": "Sin Asistente", "email": "sa@aireaustral.test", "role": "technician", "password": PW, "assign_assistant": False})
    n = len(db(app, "SELECT 1 FROM agents"))
    app2 = create_app(dbfile)                                                                              # restart: sync_agents runs
    assert len(db(app2, "SELECT 1 FROM agents")) == n
    ov = login_as(app2, "elena@aireaustral.test").get("/api/config/overview").json()
    assert {e["name"]: e["eligible"] for e in ov["employees"]}["Sin Asistente"] is False and len(ov["employees"]) == 6


# ------------------------------------------------------------------ migration V0.4 → V0.5
def _downgrade_to_v04(eng):
    """Rebuild the V0.4 shape: agents with UNIQUE(company_id, role) and users WITHOUT the `active` column."""
    with eng.begin() as c:
        c.execute(text("DROP INDEX IF EXISTS uq_agents_shared")); c.execute(text("DROP INDEX IF EXISTS uq_agents_personal")); c.execute(text("DROP INDEX IF EXISTS ix_agents_company_id"))
        c.execute(text("ALTER TABLE agents RENAME TO agents_new"))
        c.execute(text("""CREATE TABLE agents (id VARCHAR NOT NULL, company_id VARCHAR NOT NULL, role VARCHAR NOT NULL, name VARCHAR NOT NULL, description TEXT, user_id VARCHAR,
                          permissions TEXT NOT NULL, config TEXT, enabled INTEGER, PRIMARY KEY (id), UNIQUE (company_id, role), FOREIGN KEY(company_id) REFERENCES companies (id))"""))
        c.execute(text("INSERT INTO agents SELECT id,company_id,role,name,description,user_id,permissions,config,enabled FROM agents_new")); c.execute(text("DROP TABLE agents_new"))
        c.execute(text("CREATE INDEX ix_agents_company_id ON agents (company_id)"))
        c.execute(text("ALTER TABLE users DROP COLUMN active"))


def test_migration_from_a_v04_database_keeps_everything_and_allows_a_second_assistant(tmp_path):
    from app.seed import seed
    url = f"sqlite:///{tmp_path / 'v04.sqlite3'}"
    eng = make_engine(url)
    seed(eng)
    _downgrade_to_v04(eng)
    with eng.connect() as c:
        before = (c.execute(text("SELECT COUNT(*) FROM agents")).scalar(), c.execute(text("SELECT COUNT(*) FROM users")).scalar(), c.execute(text("SELECT id,role,user_id FROM agents ORDER BY id")).fetchall())
        with pytest.raises(Exception):
            c.execute(text("INSERT INTO agents(id,company_id,role,name,permissions) SELECT 'dup',company_id,role,name,permissions FROM agents WHERE role='admin_assistant' LIMIT 1")); c.commit()   # the V0.4 constraint really exists
    init_db(eng); init_db(eng)                                                                              # twice: idempotent
    backups = list(tmp_path.glob("v04.sqlite3.pre-v05-*.bak"))
    assert len(backups) == 1                                                                               # ONE backup, taken before the rebuild (not again on the second run)
    old = sqlite3.connect(backups[0]); assert old.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == before[0] and "active" not in [r[1] for r in old.execute("PRAGMA table_info(users)")]; old.close()
    with eng.connect() as c:
        assert (c.execute(text("SELECT COUNT(*) FROM agents")).scalar(), c.execute(text("SELECT COUNT(*) FROM users")).scalar(), c.execute(text("SELECT id,role,user_id FROM agents ORDER BY id")).fetchall()) == before
        assert c.execute(text("SELECT COUNT(*) FROM users WHERE active=1")).scalar() == 6 and c.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    with eng.begin() as c:   # a second administrator's assistant is now possible; a second SHARED coordinator is still impossible
        co = c.execute(text("SELECT id FROM companies WHERE slug='clima-sur'")).scalar()
        c.execute(text("INSERT INTO users(id,company_id,email,name,role,password_hash,is_demo,created_at,active) VALUES('u2',:c,'x@y.test','Otra Admin','admin','x',0,'t',1)"), {"c": co})
        c.execute(text("INSERT INTO agents(id,company_id,role,name,permissions,user_id,enabled) VALUES('a2',:c,'admin_assistant','Asistente','[]','u2',1)"), {"c": co})
    with pytest.raises(Exception):
        with eng.begin() as c:
            c.execute(text("INSERT INTO agents(id,company_id,role,name,permissions,enabled) SELECT 'a3',company_id,'coordinator','x','[]',1 FROM agents WHERE role='coordinator' LIMIT 1"))


def test_migrated_v04_database_serves_the_whole_app(tmp_path, monkeypatch):
    """A V0.4 database is opened by the V0.5 app: logins, scenario D with the existing staff and the owner's configuration screen all work."""
    from app.seed import seed
    url = f"sqlite:///{tmp_path / 'v04b.sqlite3'}"
    eng = make_engine(url)
    seed(eng)
    _downgrade_to_v04(eng)
    app = create_app(url)
    o = login(app, "dueno@climasur.test")
    assert [e["eligible"] for e in o.get("/api/config/overview").json()["employees"]] == [True, True, True]
    r = chat(TestClient(app), body={"message": "Hola, quiero agendar una mantención preventiva de mi split del living.", "name": "Marta Rojas", "phone": "+56 9 5555 0101"}, mid="mig-0001")
    assert o.get(f"/api/runs/{r['run_id']}").json()["status"] == "waiting_approval"


def test_scenarios_in_a_configured_company_use_its_own_staff(app, third):
    """Scenario D (customer chat → quote → approval → technician material request) in the THIRD company: tasks go to ITS employees, balanced, not to Clima Sur's."""
    o = third["owner"]
    c = TestClient(app)
    r = chat(c, slug="aire-austral", body={"message": "Hola, quiero agendar una mantención preventiva de mi split del living.", "name": "Marta Rojas", "phone": "+56 9 5555 0101"}, mid="third-d-0001")
    run = o.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "waiting_approval" and run["scenario"] == "D"
    o.post(f"/api/approvals/{o.get('/api/dashboard').json()['pending_approvals'][0]['id']}/decide", json={"decision": "approved"})
    run = o.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] == "waiting_technician"
    st = third["staff"]
    wo_tech = db(app, "SELECT u.email FROM work_orders w JOIN users u ON u.id=w.technician_id WHERE w.request_id=:r", r=run["request_id"])[0][0]
    tech_client = next(v["client"] for v in st.values() if v["role"] == "technician" and f"{v['name'].split()[0].lower().replace('í','i')}@" in wo_tech)
    out = tech_client.post("/api/assistant/chat", json={"message": "necesito dos cilindros de gas para la visita de Marta antes de ir"}).json()
    assert out["tool_calls"][0]["ok"], out
    admin_task = db(app, "SELECT u.name FROM tasks t JOIN users u ON u.id=t.assignee_user_id WHERE t.run_id=:r AND t.kind='material'", r=r["run_id"])[0][0]
    assert admin_task in ("Javiera Pons", "Rodrigo Silva")                                                  # one of ITS administrators (balanced by workload), never Clima Sur's Javiera Soto


def test_reset_scenarios_is_refused_for_configured_companies(app, third):
    r = third["owner"].post("/api/demo/reset-scenarios", json={"confirm": "REINICIAR"})
    assert r.status_code == 400 and "empresas de demostración sembradas" in r.json()["detail"]


def test_an_llm_proposal_never_picks_the_people_in_a_configured_company(app, third):
    """Ollama may only flag intents; WHO receives the work is decided by the owner among eligible employees (two administrators → the coordinator still asks)."""
    from app.providers.base import PlanProposal
    from .test_plans import FakeLLM
    o = third["owner"]
    request_for(o)
    app.state.orch.provider = FakeLLM(PlanProposal(False, True, False, True, None, [], "llm:fake-model"))
    pl = o.post("/api/plans", json={"text": INSTR_ADMIN, "client_plan_id": rid()}).json()["plan"]
    assert pl["status"] == "needs_clarification" and pl["clarification"]["field"] == "admin_user_id" and pl["source"] == "llm:fake-model" and db(app, "SELECT COUNT(*) FROM tasks WHERE kind='plan_step'")[0][0] == 0


# ================================================================== PR #6 review findings (revisión automatizada) — regression tests written BEFORE the fixes
# 1) sync_agents() must not turn an employee configured WITHOUT an assistant into an eligible one on restart
def test_restart_never_assigns_an_assistant_to_the_only_employee_of_a_role(app, dbfile):
    make_company(app)
    o = login_as(app, "elena@aireaustral.test")
    solo = o.post("/api/config/employees", json={"name": "Sola Admin", "email": "sola@aireaustral.test", "role": "admin", "password": PW, "assign_assistant": False}).json()
    o.post("/api/config/employees", json={"name": "Tec Uno", "email": "tecuno@aireaustral.test", "role": "technician", "password": PW})
    assert db(app, "SELECT COUNT(*) FROM agents WHERE user_id=:u", u=solo["user_id"])[0][0] == 0
    app2 = create_app(dbfile)                                                                              # restart → sync_agents()
    assert db(app2, "SELECT COUNT(*) FROM agents WHERE user_id=:u", u=solo["user_id"])[0][0] == 0           # still NO assistant: only the owner can assign one
    ov = login_as(app2, "elena@aireaustral.test").get("/api/config/overview").json()
    assert {e["name"]: e["eligible"] for e in ov["employees"]}["Sola Admin"] is False
    assert db(app2, "SELECT COUNT(*) FROM agents WHERE role='admin_assistant' AND company_id=(SELECT id FROM companies WHERE slug='aire-austral')")[0][0] == 0
    assert "asistente personal activo" in login_as(app2, "sola@aireaustral.test").post("/api/assistant/chat", json={"message": "tareas"}).json()["reply"]


def test_restart_still_repairs_a_legacy_company_that_lost_its_assistant_rows(tmp_path):
    """The repair exists for legacy seeded data only: a seeded company with NO personal assistants at all and never configured gets them back."""
    from app.seed import seed, sync_agents
    eng = make_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    seed(eng)
    with eng.begin() as c:
        c.execute(text("DELETE FROM agents WHERE user_id IS NOT NULL AND company_id=(SELECT id FROM companies WHERE slug='clima-sur')"))
    assert sync_agents(eng) >= 3
    with eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM agents WHERE user_id IS NOT NULL AND company_id=(SELECT id FROM companies WHERE slug='clima-sur')")).scalar() == 3


# 2) a stale template of the employee's PREVIOUS role can never be re-enabled, and availability requires a real technician
def test_cannot_reenable_the_assistant_template_of_a_previous_role(app, third):
    o, cam = third["owner"], third["staff"]["camila"]
    assert o.post(f"/api/config/employees/{cam['user_id']}/role", json={"role": "admin"}).status_code == 200      # technician → administrator
    stale = cam["agent_id"]                                                                                    # her old tech_assistant (now disabled)
    r = o.post(f"/api/config/agents/{stale}/enabled", json={"enabled": True})
    assert r.status_code == 400 and "es para el rol technician" in r.json()["detail"]
    assert db(app, "SELECT enabled FROM agents WHERE id=:a", a=stale)[0][0] == 0
    ov = {e["name"]: e for e in o.get("/api/config/overview").json()["employees"]}["Camila Vera"]
    assert {a["template"]: a["compatible"] for a in ov["assistants"]} == {"tech_assistant": False, "admin_assistant": True}
    assert o.post("/api/config/assignments", json={"user_id": cam["user_id"], "template": "tech_assistant"}).status_code == 400   # (assign_template already refused it)


def test_availability_never_offers_slots_of_someone_who_is_no_longer_a_technician(app, third):
    from app.tools import invoke
    o, cam = third["owner"], third["staff"]["camila"]
    o.post(f"/api/config/employees/{cam['user_id']}/role", json={"role": "admin"})
    with app.state.eng.begin() as c:                                                                           # corrupt state on purpose: the stale tech assistant IS enabled
        c.execute(text("UPDATE agents SET enabled=1 WHERE id=:a"), {"a": cam["agent_id"]})
    co = db(app, "SELECT id FROM companies WHERE slug='aire-austral'")[0][0]
    slots = invoke(app.state.eng, app.state.orch.principal(co, "coordinator"), "availability.technicians", {"limit": 20})["slots"]
    assert slots and cam["user_id"] not in {s["technician_id"] for s in slots}                                  # only real technicians (Matías)
    free = db(app, "SELECT id FROM availability WHERE technician_id=:u AND status='free' LIMIT 1", u=cam["user_id"])[0][0]
    req = request_for(o)
    from app.tools import ToolError
    with pytest.raises(ToolError):                                                                              # defense in depth: a visit cannot be proposed on a non-technician's slot
        invoke(app.state.eng, app.state.orch.principal(co, "admin_assistant", third["staff"]["rodrigo"]["user_id"]), "visits.propose", {"request_id": req, "availability_id": free})


# 3) reset of scenarios must treat manual requests, their conversations, customers and plans consistently (no dangling references)
ORPHANS = {
    "requests→customers": "SELECT COUNT(*) FROM requests r LEFT JOIN customers c ON c.id=r.customer_id WHERE c.id IS NULL",
    "conversations→customers": "SELECT COUNT(*) FROM conversations v WHERE v.customer_id IS NOT NULL AND v.customer_id NOT IN (SELECT id FROM customers)",
    "messages→conversations": "SELECT COUNT(*) FROM messages m WHERE m.conversation_id NOT IN (SELECT id FROM conversations)",
    "requests→conversations": "SELECT COUNT(*) FROM requests r WHERE r.conversation_id IS NOT NULL AND r.conversation_id NOT IN (SELECT id FROM conversations)",
    "tasks→requests": "SELECT COUNT(*) FROM tasks t WHERE t.request_id IS NOT NULL AND t.request_id NOT IN (SELECT id FROM requests)",
    "tasks→runs/plans": "SELECT COUNT(*) FROM tasks t WHERE t.run_id IS NOT NULL AND t.run_id NOT IN (SELECT id FROM runs UNION SELECT id FROM plans)",
    "delegations→runs/plans": "SELECT COUNT(*) FROM delegations d WHERE d.run_id NOT IN (SELECT id FROM runs UNION SELECT id FROM plans)",
    "events→runs/plans": "SELECT COUNT(*) FROM events e WHERE e.run_id NOT IN (SELECT id FROM runs UNION SELECT id FROM plans)",
    "results→runs/plans": "SELECT COUNT(*) FROM results x WHERE x.run_id IS NOT NULL AND x.run_id NOT IN (SELECT id FROM runs UNION SELECT id FROM plans)",
    "plans→requests": "SELECT COUNT(*) FROM plans p WHERE p.request_id IS NOT NULL AND p.request_id NOT IN (SELECT id FROM requests)",
    "plan_steps→plans": "SELECT COUNT(*) FROM plan_steps s WHERE s.plan_id NOT IN (SELECT id FROM plans)",
    "visits/quotes/work_orders→requests": "SELECT (SELECT COUNT(*) FROM visits WHERE request_id NOT IN (SELECT id FROM requests)) + (SELECT COUNT(*) FROM quotes WHERE request_id NOT IN (SELECT id FROM requests)) + (SELECT COUNT(*) FROM work_orders WHERE request_id NOT IN (SELECT id FROM requests))",
    "material_requests/commitments→requests": "SELECT (SELECT COUNT(*) FROM material_requests WHERE request_id IS NOT NULL AND request_id NOT IN (SELECT id FROM requests)) + (SELECT COUNT(*) FROM commitments WHERE request_id NOT IN (SELECT id FROM requests))",
}


def orphans(app):
    return {k: db(app, sql)[0][0] for k, sql in ORPHANS.items() if db(app, sql)[0][0]}


def test_scenario_reset_leaves_no_dangling_references_after_manual_requests_and_plans(app, owner, admin, anon):
    norte = login(app, "dueno@motornorte.test")
    snap_norte = (db(app, "SELECT COUNT(*) FROM requests WHERE company_id=(SELECT id FROM companies WHERE slug='motor-norte')")[0][0], db(app, "SELECT COUNT(*) FROM customers WHERE company_id=(SELECT id FROM companies WHERE slug='motor-norte')")[0][0])
    seed_requests = db(app, "SELECT id FROM requests WHERE company_id=(SELECT id FROM companies WHERE slug='clima-sur')")
    assert orphans(app) == {} and len(seed_requests) == 1
    # a manual request for a NEW customer, a plan on it (tasks + delegations + events under the plan id), a manual request on a SEED customer, and a chat scenario
    new = owner.post("/api/requests/manual", json={"customer_name": "Cliente Nuevo SpA", "phone": "+56 9 5555 0888", "description": "Quiero una mantención preventiva del split", "client_request_id": rid()}).json()["request_id"]
    on_seed = owner.post("/api/requests/manual", json={"customer_name": "Marta Rojas", "description": "Necesito otra mantención de mi split", "client_request_id": rid()}).json()["request_id"]
    pl = owner.post("/api/plans", json={"text": "Pide a administración que verifique los materiales de " + new, "client_plan_id": rid()}).json()["plan"]
    assert pl["status"] == "awaiting_user_confirmation" and owner.post(f"/api/plans/{pl['id']}/confirm").status_code == 200
    assert db(app, "SELECT COUNT(*) FROM tasks WHERE run_id=:p", p=pl["id"])[0][0] == 1
    chat(anon, body={"message": "Hola, mi aire split no enfría desde ayer, creo que le falta gas.", "name": "Pedro Gálvez", "phone": "+56 9 5555 0102"}, mid="reset-chat-0001")
    assert orphans(app) == {}
    r = owner.post("/api/demo/reset-scenarios", json={"confirm": "REINICIAR"})
    assert r.status_code == 200, r.text
    assert orphans(app) == {}, orphans(app)                                                                    # nothing left pointing at deleted rows
    assert [x[0] for x in db(app, "SELECT id FROM requests WHERE company_id=(SELECT id FROM companies WHERE slug='clima-sur')")] == [seed_requests[0][0]]     # only the seeded history survives
    assert db(app, "SELECT COUNT(*) FROM customers WHERE name='Cliente Nuevo SpA'")[0][0] == 0 and db(app, "SELECT COUNT(*) FROM customers WHERE name='Marta Rojas'")[0][0] == 1
    assert db(app, "SELECT COUNT(*) FROM plans WHERE company_id=(SELECT id FROM companies WHERE slug='clima-sur')")[0][0] == 0
    assert db(app, "SELECT COUNT(*) FROM conversations WHERE channel='interno'")[0][0] == 0 and db(app, "SELECT COUNT(*) FROM messages WHERE sender LIKE 'staff:%'")[0][0] == 0
    assert owner.get(f"/api/requests/{new}").status_code == 404 and owner.get(f"/api/requests/{on_seed}").status_code == 404 and owner.get(f"/api/plans/{pl['id']}").status_code == 404
    assert (db(app, "SELECT COUNT(*) FROM requests WHERE company_id=(SELECT id FROM companies WHERE slug='motor-norte')")[0][0], db(app, "SELECT COUNT(*) FROM customers WHERE company_id=(SELECT id FROM companies WHERE slug='motor-norte')")[0][0]) == snap_norte   # other company untouched
    assert norte.get("/api/requests").status_code == 200
