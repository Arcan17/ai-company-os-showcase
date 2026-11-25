"""V0.11 (spec 006, US1) — la demo abre con el producto en uso: una solicitud a medio camino y una decisión pendiente de la dueña, creadas por el motor REAL.
Apagado por defecto; nunca en piloto, nunca en una empresa del operador ni en una base ya usada; idempotente y a prueba de cortes; cada visitante recibe su copia."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import pilot
from app.api import create_app
from app.config import settings
from app.demo_example import MARK, ensure_example
from app.public_demo import VisitorRouter

from .conftest import GAS, login
from .test_v05_config import make_company


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "demo_example", True)


def count(app, table, where="1=1"):
    with app.state.eng.connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar()


def marks(app):
    return count(app, "audit_log", f"action='{MARK}'")


def test_apagado_por_defecto_la_demo_abre_vacia(app):
    assert (count(app, "runs"), count(app, "approvals"), marks(app)) == (0, 0, 0)
    assert login(app, "dueno@climasur.test").get("/api/dashboard").json()["pending_approvals"] == []


def test_con_el_ejemplo_la_duena_ve_una_decision_pendiente_y_una_solicitud_en_curso(on, app):
    assert (count(app, "runs"), count(app, "approvals", "status='pendiente'"), marks(app)) == (1, 1, 1)
    d = login(app, "dueno@climasur.test").get("/api/dashboard").json()
    assert len(d["pending_approvals"]) == 1 and d["pending_approvals"][0]["amount"] == 63000
    assert "Marta Rojas" in d["pending_approvals"][0]["summary"]
    assert d["active_requests"] >= 1 and d["runs"][0]["status"] == "waiting_approval"


def test_el_ejemplo_pasa_por_la_auditoria_y_los_eventos_reales(on, app):
    assert count(app, "events", "type='run.waiting_approval'") == 1
    assert count(app, "audit_log", "action='approvals.request'") == 1                                   # el motor dejó su rastro normal, no filas sueltas
    assert count(app, "tasks") == 0 and count(app, "messages", "status='enviado'") == 0              # nada se ejecutó ni se envió: solo espera la decisión


def test_aprobar_el_ejemplo_sigue_las_mismas_reglas_que_cualquier_aprobacion(on, app):
    o = login(app, "dueno@climasur.test")
    pid = o.get("/api/dashboard").json()["pending_approvals"][0]["id"]
    assert login(app, "admin@climasur.test").post(f"/api/approvals/{pid}/decide", json={"decision": "approved"}).status_code == 403     # solo la dueña decide
    assert o.post(f"/api/approvals/{pid}/decide", json={"decision": "approved"}).status_code == 200
    assert count(app, "approvals", "status='pendiente'") == 0


def test_es_idempotente_dos_arranques_no_duplican(on, dbfile):
    create_app(dbfile)
    second = create_app(dbfile)
    assert (count(second, "runs"), count(second, "approvals"), marks(second)) == (1, 1, 1)


def test_un_fallo_no_deja_marca_ni_rompe_el_arranque_y_el_siguiente_arranque_lo_completa(on, app, monkeypatch):
    with app.state.eng.begin() as c:                                      # volver a «recién abierta»
        for t in ("approvals", "events", "runs", "messages", "conversations", "audit_log", "requests"):
            c.execute(text(f"DELETE FROM {t}"))
    orch = app.state.orch
    monkeypatch.setattr(orch, "start_chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ensure_example(app.state.eng, orch) is False and marks(app) == 0
    monkeypatch.undo()
    monkeypatch.setattr(settings, "demo_example", True)
    assert ensure_example(app.state.eng, orch) is True and marks(app) == 1 and count(app, "approvals", "status='pendiente'") == 1


def test_sin_una_decision_pendiente_no_hay_marca(on, app):
    with app.state.eng.begin() as c:
        for t in ("approvals", "events", "runs", "messages", "conversations", "audit_log", "requests"):
            c.execute(text(f"DELETE FROM {t}"))
    assert ensure_example(app.state.eng, app.state.orch, deadline=0) is False       # no esperó: el ejemplo no está completo → no se marca como creado
    assert marks(app) == 0


def test_nunca_en_piloto(app, monkeypatch):
    monkeypatch.setattr(settings, "demo_example", True)
    monkeypatch.setattr(pilot, "is_pilot", lambda: True)
    assert ensure_example(app.state.eng, app.state.orch) is False
    assert (count(app, "runs"), marks(app)) == (0, 0)


def test_una_base_ya_usada_no_recibe_ejemplo(app, monkeypatch):
    assert TestClient(app).post("/api/public/clima-sur/chat", json={**GAS, "client_msg_id": "uso-0001"}).status_code == 200
    n = count(app, "runs")
    monkeypatch.setattr(settings, "demo_example", True)
    assert ensure_example(app.state.eng, app.state.orch) is False and count(app, "runs") == n and marks(app) == 0


def test_una_empresa_del_operador_nunca_recibe_ejemplo(on, app, monkeypatch):
    monkeypatch.setattr(settings, "allow_company_creation", True)
    make_company(app)
    assert ensure_example(app.state.eng, app.state.orch) is False          # la marca ya existe: idempotente
    with app.state.eng.connect() as c:
        otra = c.execute(text("SELECT id FROM companies WHERE slug='aire-austral'")).scalar()
        assert c.execute(text("SELECT COUNT(*) FROM runs WHERE company_id=:c"), {"c": otra}).scalar() == 0
        assert c.execute(text("SELECT COUNT(*) FROM approvals WHERE company_id=:c"), {"c": otra}).scalar() == 0


def test_con_un_modelo_de_ia_no_se_crea(app, monkeypatch):
    monkeypatch.setattr(settings, "demo_example", True)
    monkeypatch.setattr(app.state.orch.provider, "mode", "llm", raising=False)
    assert ensure_example(app.state.eng, app.state.orch) is False and count(app, "runs") == 0


# ───────────── demo pública: la plantilla se siembra una vez y cada visitante recibe su copia ─────────────
@pytest.fixture
def router(tmp_path, on):
    return VisitorRouter(tmp_path / "pd", secret=b"s" * 32, max_visitors=3, idle_seconds=60, new_per_source=50, sweep_seconds=3600)


def _owner(c):
    assert c.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": "demo1234"}).status_code == 200


def test_cada_visitante_recibe_su_decision_pendiente_y_no_comparten_nada(router):
    with TestClient(router) as a:
        b = TestClient(router)
        _owner(a); _owner(b)
        pa, pb = (x.get("/api/dashboard").json()["pending_approvals"] for x in (a, b))
        assert len(pa) == 1 and len(pb) == 1 and pa[0]["id"] == pb[0]["id"]       # copias del mismo ejemplo… pero independientes:
        assert a.post(f"/api/approvals/{pa[0]['id']}/decide", json={"decision": "approved"}).status_code == 200
        assert len(a.get("/api/dashboard").json()["pending_approvals"]) == 0
        assert len(b.get("/api/dashboard").json()["pending_approvals"]) == 1     # lo que hace A no cambia lo que ve B


def test_una_copia_nueva_conserva_la_decision_pendiente_tras_arrancar_y_se_puede_aprobar(router):
    with TestClient(router) as c:
        _owner(c)
        d = c.get("/api/dashboard").json()
        assert d["runs"][0]["status"] == "waiting_approval"                         # el arranque del visitante no interrumpió la ejecución que espera a una persona
        assert c.post(f"/api/approvals/{d['pending_approvals'][0]['id']}/decide", json={"decision": "approved"}).status_code == 200
