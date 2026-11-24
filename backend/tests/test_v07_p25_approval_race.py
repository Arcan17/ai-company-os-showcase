"""V0.7 P2.5 (revisión del PR #26) — CARRERA «aprobación decidida antes de que la ejecución espere»: el fallo real de `frontend/e2e/demo.spec.ts:83` en el CI de GitHub.
`ask_approval` crea la aprobación (ya visible en el panel) y varios pasos después marca la ejecución `waiting_approval`. Si una persona decide en esa ventana, el CAS de `decide()`
(`WHERE status='waiting_approval'`) no encuentra nada, no relanza, y la ejecución se queda esperando para siempre una aprobación YA decidida (lost wake-up). Reproducido 8/8 con el motor en un hilo.
Estas pruebas son DETERMINISTAS: el motor se detiene con `threading.Event` justo en ese punto (sin dormir), se decide, y se le deja seguir. Invariante: exactamente UNA reanudación, sin duplicar efectos,
sin saltarse el fencing, sin reactivar una ejecución fallida/interrumpida. Bases temporales, datos ficticios."""
import threading

import pytest
from sqlalchemy import text

from app.api import create_app
from app.config import settings
from app.engine.orchestrator import Orchestrator

from .conftest import GAS, chat, login, pending_approval
from .test_scenario_d import MANT, TECH_MSG, count, tech_asks

SECRET = "carrera-secreto-abcdefghijk"


class Gate:
    """Detiene al motor JUSTO antes de pasar a «esperando aprobación» (la aprobación ya existe y es visible; la ejecución sigue `running`). Sirve igual con el código viejo (engancha `_set`) y con el nuevo
    (engancha `park_for_approval`), para que la prueba falle por la RAZÓN correcta antes del arreglo."""

    def __init__(self, mp, barrier: threading.Barrier | None = None):
        self.at_park, self.go, self.hits = threading.Event(), threading.Event(), []
        self.barrier = barrier
        if hasattr(Orchestrator, "park_for_approval"):
            orig = Orchestrator.park_for_approval

            def hooked(this, *a, **k):
                self._pause()
                return orig(this, *a, **k)
            mp.setattr(Orchestrator, "park_for_approval", hooked)
        else:
            orig = Orchestrator._set

            def hooked(this, run_id, **kw):
                if kw.get("status") == "waiting_approval":
                    self._pause()
                return orig(this, run_id, **kw)
            mp.setattr(Orchestrator, "_set", hooked)

    def _pause(self):
        self.hits.append(threading.current_thread().name)
        self.at_park.set()
        if self.barrier is not None:
            self.barrier.wait(10)
        else:
            assert self.go.wait(10), "el motor no fue liberado"


def in_thread(fn):
    box = {}

    def run():
        try:
            box["out"] = fn()
        except BaseException as e:                                                                 # noqa: BLE001  (se relanza al unir: una excepción en el hilo NO debe pasar inadvertida)
            box["err"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def join(t, box):
    t.join(15)
    assert not t.is_alive(), "el hilo del motor no terminó"
    if "err" in box:
        raise box["err"]
    return box.get("out")


def run_row(app, run_id):
    with app.state.eng.connect() as c:
        return dict(c.execute(text("SELECT status,phase FROM runs WHERE id=:i"), {"i": run_id}).mappings().first())


def effects(app, run_id):
    """Los efectos que NO deben duplicarse por una reanudación doble."""
    with app.state.eng.connect() as c:
        q = lambda sql, **p: c.execute(text(sql), p).scalar()                                       # noqa: E731
        return {"visits": q("SELECT COUNT(*) FROM visits"), "work_orders": q("SELECT COUNT(*) FROM work_orders"),
                "approval_decided_events": q("SELECT COUNT(*) FROM events WHERE run_id=:r AND type='approval.decided'", r=run_id),
                "decision_phase_events": q("SELECT COUNT(*) FROM events WHERE run_id=:r AND title LIKE 'Decisión humana recibida%'", r=run_id),
                "decide_audits": q("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'approvals.decide:%'")}


@pytest.fixture
def sync(monkeypatch):
    monkeypatch.setattr(settings, "sync_runs", True)                                               # la reanudación corre en el MISMO hilo: `join` espera la ejecución entera
    return monkeypatch


# ───────────────────────────── presupuesto principal ─────────────────────────────

def test_an_approval_decided_before_the_run_waits_still_resumes_the_run_exactly_once(app, anon, owner, sync):
    g = Gate(sync)
    t, box = in_thread(lambda: chat(anon, body=GAS, mid="race-a-0001", secret=SECRET))
    assert g.at_park.wait(10)
    ap = pending_approval(owner)                                                                    # la aprobación YA es visible…
    run_id = owner.get("/api/dashboard").json()["runs"][0]["id"]
    assert run_row(app, run_id)["status"] == "running"                                              # …y la ejecución todavía NO espera
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 200
    assert run_row(app, run_id)["status"] == "running"                                              # el CAS de decide() no encontró «waiting_approval»: no relanzó
    g.go.set()
    join(t, box)
    assert run_row(app, run_id)["status"] == "completed", "la ejecución quedó esperando una aprobación YA decidida (lost wake-up)"
    fx = effects(app, run_id)
    assert fx == {"visits": 1, "work_orders": 1, "approval_decided_events": 1, "decision_phase_events": 1, "decide_audits": 1}, fx


def test_a_rejection_in_the_same_window_also_resumes_exactly_once(app, anon, owner, sync):
    g = Gate(sync)
    t, box = in_thread(lambda: chat(anon, body=GAS, mid="race-r-0001", secret=SECRET))
    assert g.at_park.wait(10)
    ap = pending_approval(owner)
    run_id = owner.get("/api/dashboard").json()["runs"][0]["id"]
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "rejected"}).status_code == 200
    g.go.set()
    join(t, box)
    assert run_row(app, run_id)["status"] == "completed"
    fx = effects(app, run_id)
    assert fx["visits"] == 0 and fx["decision_phase_events"] == 1 and fx["decide_audits"] == 1, fx


def test_a_decision_after_the_run_waits_is_the_normal_path_and_still_resumes_exactly_once(app, anon, owner, sync):
    r = chat(anon, body=GAS, mid="race-b-0001", secret=SECRET)
    assert run_row(app, r["run_id"])["status"] == "waiting_approval"
    ap = pending_approval(owner)
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 200
    assert run_row(app, r["run_id"])["status"] == "completed"
    assert effects(app, r["run_id"]) == {"visits": 1, "work_orders": 1, "approval_decided_events": 1, "decision_phase_events": 1, "decide_audits": 1}


def test_a_double_decision_in_the_window_resumes_once_and_the_opposite_one_is_a_conflict(app, anon, owner, sync):
    g = Gate(sync)
    t, box = in_thread(lambda: chat(anon, body=GAS, mid="race-d-0001", secret=SECRET))
    assert g.at_park.wait(10)
    ap = pending_approval(owner)
    run_id = owner.get("/api/dashboard").json()["runs"][0]["id"]
    first = owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"})
    again = owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"})
    opposite = owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "rejected"})
    assert (first.status_code, again.status_code, opposite.status_code) == (200, 200, 409) and again.json()["deduplicated"] is True
    g.go.set()
    join(t, box)
    assert run_row(app, run_id)["status"] == "completed"
    assert effects(app, run_id) == {"visits": 1, "work_orders": 1, "approval_decided_events": 1, "decision_phase_events": 1, "decide_audits": 1}


@pytest.mark.parametrize("round_", range(8))
def test_a_decision_racing_the_park_at_the_same_instant_never_loses_or_duplicates_the_resume(tmp_path, monkeypatch, round_):
    """El motor y la decisión se liberan A LA VEZ con una barrera (sin dormir); el orden real lo decide el planificador. Sea cual sea, el resultado es el mismo."""
    monkeypatch.setattr(settings, "sync_runs", True)
    monkeypatch.setattr(settings, "step_delay", 0)
    app = create_app(f"sqlite:///{tmp_path / 'r.db'}")
    owner, anon = login(app, "dueno@climasur.test"), login(app, "dueno@climasur.test")
    from fastapi.testclient import TestClient
    anon = TestClient(app)
    barrier = threading.Barrier(2)
    g = Gate(monkeypatch, barrier=barrier)
    t, box = in_thread(lambda: chat(anon, body=GAS, mid=f"race-c-{round_:04d}", secret=SECRET))
    for _ in range(2000):                                                                           # espera a que la aprobación sea visible SIN dormir (reintenta la lectura)
        ap = owner.get("/api/dashboard").json()["pending_approvals"]
        if ap:
            break
    assert ap, "la aprobación nunca fue visible"
    run_id = owner.get("/api/dashboard").json()["runs"][0]["id"]
    t2, box2 = in_thread(lambda: (barrier.wait(10), owner.post(f"/api/approvals/{ap[0]['id']}/decide", json={"decision": "approved"}))[1])
    join(t2, box2)
    join(t, box)
    assert run_row(app, run_id)["status"] == "completed", "se perdió la reanudación"
    assert effects(app, run_id) == {"visits": 1, "work_orders": 1, "approval_decided_events": 1, "decision_phase_events": 1, "decide_audits": 1}
    app.state.orch.keeper.stop()


# ───────────────────────────── material adicional (escenario D) ─────────────────────────────

def test_a_material_approval_decided_before_the_run_waits_still_resumes_exactly_once(app, anon, owner, tech, sync):
    r = chat(anon, body=MANT, mid="race-m-0001", secret=SECRET)
    ap0 = pending_approval(owner)
    assert owner.post(f"/api/approvals/{ap0['id']}/decide", json={"decision": "approved"}).status_code == 200
    run_id = r["run_id"]
    assert run_row(app, run_id)["status"] == "waiting_technician"
    g = Gate(sync)                                                                                   # (el motor se detiene al pasar a esperar el gasto del MATERIAL)
    t, box = in_thread(lambda: tech_asks(tech))
    assert g.at_park.wait(10)
    mat = next(a for a in owner.get("/api/approvals").json()["approvals"] if a["status"] == "pendiente")
    assert run_row(app, run_id)["status"] == "running" and "Material" in mat["subject"]
    assert owner.post(f"/api/approvals/{mat['id']}/decide", json={"decision": "approved"}).status_code == 200
    g.go.set()
    join(t, box)
    assert run_row(app, run_id)["status"] == "waiting_task", "el gasto de material ya estaba aprobado y la ejecución no avanzó a esperar la evidencia"
    with app.state.eng.connect() as c:
        assert c.execute(text("SELECT status FROM material_requests WHERE run_id=:r"), {"r": run_id}).scalar() == "aprobado"
        assert c.execute(text("SELECT COUNT(*) FROM events WHERE run_id=:r AND title LIKE 'Decisión humana recibida%'"), {"r": run_id}).scalar() == 2      # la del presupuesto y la del material: una cada una


# ───────────────────────────── lo que NO debe pasar ─────────────────────────────

def test_a_run_declared_failed_in_the_window_is_never_reactivated_by_a_late_decision(app, anon, owner, sync):
    g = Gate(sync)
    t, box = in_thread(lambda: chat(anon, body=GAS, mid="race-f-0001", secret=SECRET))
    assert g.at_park.wait(10)
    ap = pending_approval(owner)
    run_id = owner.get("/api/dashboard").json()["runs"][0]["id"]
    assert app.state.orch._fail_run(run_id, reason="interrupted", title="Ejecución interrumpida", summary="prueba")           # otra instancia declaró la ejecución interrumpida (el veredicto es FINAL)
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 200
    g.go.set()
    join(t, box)
    assert run_row(app, run_id)["status"] == "failed", "una decisión tardía reactivó una ejecución ya fallida"
    fx = effects(app, run_id)
    assert fx["visits"] == 0 and fx["decision_phase_events"] == 0, fx


def test_the_normal_path_leaves_the_run_waiting_when_nobody_has_decided(app, anon, owner, sync):
    r = chat(anon, body=GAS, mid="race-n-0001", secret=SECRET)
    assert run_row(app, r["run_id"])["status"] == "waiting_approval" and pending_approval(owner)["id"]
    with app.state.eng.connect() as c:
        assert c.execute(text("SELECT status FROM approvals WHERE run_id=:r"), {"r": r["run_id"]}).scalar() == "pendiente"
    assert effects(app, r["run_id"])["decision_phase_events"] == 0
