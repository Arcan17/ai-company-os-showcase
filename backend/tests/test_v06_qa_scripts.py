"""V0.6 — los scripts de QA independientes (qa/evidence.py, qa/llm_audit.py) deben funcionar CON la defensa CSRF activa, sin debilitarla.
Hallazgo de la revisión del PR #10: ambos hacían POST con la cookie pero sin `X-CSRF-Token` → 403 (llm_audit: AssertionError; evidence: KeyError 'plan').
Aquí corren los scripts REALES (subprocesos) sobre una base ficticia temporal y un servidor real en un puerto libre. Nunca se abre una base del usuario."""
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import make_engine
from app.demo_seed import build

ROOT = Path(__file__).resolve().parents[2]
QA = ROOT / "qa"
sys.path.insert(0, str(QA))
import qa_client  # noqa: E402

OWNER = "dueno@climasur.test"


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


@pytest.fixture
def qa_db(tmp_path):
    """The audited shape of the data: Marta has a prepared maintenance proposal AND another open request (the ambiguity llm_audit.py probes)."""
    path = tmp_path / "demo_comercial_qa.sqlite3"
    eng = make_engine(f"sqlite:///{path}"); build(eng); eng.dispose()
    con = sqlite3.connect(path)
    con.execute("UPDATE requests SET status='propuesta_preparada' WHERE id=(SELECT r.id FROM requests r JOIN customers c ON c.id=r.customer_id "
                "WHERE c.name='Marta Rojas' AND r.category='mantencion' AND r.status='clasificada')")
    co, cu, cv = con.execute("SELECT r.company_id, r.customer_id, r.conversation_id FROM requests r JOIN customers c ON c.id=r.customer_id WHERE c.name='Marta Rojas' LIMIT 1").fetchone()
    con.execute("INSERT INTO requests(id,company_id,customer_id,conversation_id,description,category,urgency,status,idem_key,created_at,updated_at) "
                "VALUES('req_qa_gas_0001',?,?,?,'El aire no enfría, creo que le falta gas','reparacion_gas','media','clasificada','qa:marta-gas','2026-11-22T10:00:00.000+00:00','2026-11-22T10:00:00.000+00:00')", (co, cu, cv))
    con.commit(); con.close()
    return path


@pytest.fixture
def live(qa_db, tmp_path):
    port = _free_port()
    (tmp_path / "static").mkdir()
    env = {**os.environ, "AICOS_DB_URL": f"sqlite:///{qa_db}", "AICOS_STEP_DELAY": "0.12", "AICOS_SYNC": "0", "AICOS_LLM_PROVIDER": "deterministic", "AICOS_STATIC_DIR": str(tmp_path / "static")}
    p = subprocess.Popen([sys.executable, "-W", "ignore", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-proxy-headers"], cwd=ROOT / "backend", env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(base + "/api/auth/demo-accounts", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            raise AssertionError("el servidor de prueba no arrancó")
        yield base
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


def test_qa_login_helper_keeps_the_token_and_the_protection_stays_intact(app):
    plain = TestClient(app)
    assert plain.post("/api/auth/login", json={"email": OWNER, "password": "demo1234"}).status_code == 200
    bare = plain.post("/api/plans", json={"text": "Revisa las solicitudes pendientes", "client_plan_id": "qa-bare-0001"}, headers={"x-test-no-csrf": "1"})
    assert bare.status_code == 403 and bare.json()["detail"].startswith("csrf")       # what the scripts hit: still refused — nothing was relaxed
    c = qa_client.csrf_login(TestClient(app), OWNER)
    assert c.headers["X-CSRF-Token"] and c.post("/api/plans", json={"text": "Revisa las solicitudes pendientes", "client_plan_id": "qa-ok-0001"}).status_code == 200
    first = c.headers["X-CSRF-Token"]
    qa_client.csrf_login(c, OWNER)                                                    # a second login = a new session = a new token, and the client follows it
    assert c.headers["X-CSRF-Token"] != first
    assert c.post("/api/plans", json={"text": "Revisa las solicitudes pendientes", "client_plan_id": "qa-ok-0002"}).status_code == 200
    c.headers["X-CSRF-Token"] = first
    assert c.post("/api/plans", json={"text": "Revisa las solicitudes pendientes", "client_plan_id": "qa-old-0003"}).status_code == 403   # the previous session's token is dead


def test_a_script_that_forgets_the_token_is_refused_by_a_live_server(live):
    c = httpx.Client(base_url=live, timeout=30)
    assert c.post("/api/auth/login", json={"email": OWNER, "password": "demo1234"}).status_code == 200
    r = c.post("/api/plans", json={"text": "Revisa las solicitudes pendientes", "client_plan_id": "qa-live-0001"})
    assert r.status_code == 403 and r.json() == {"detail": "csrf: token inválido o ausente"}
    assert qa_client.csrf_login(httpx.Client(base_url=live, timeout=30), OWNER).post("/api/plans", json={"text": "Revisa las solicitudes pendientes", "client_plan_id": "qa-live-0002"}).status_code == 200


def test_llm_audit_script_runs_end_to_end_with_csrf_on(qa_db, tmp_path):
    out = tmp_path / "out"
    env = {**os.environ, "AICOS_QA_SOURCE_DB": str(qa_db), "AICOS_QA_OUT": str(out), "AICOS_OLLAMA_URL": "http://127.0.0.1:1"}      # no Ollama: every LLM call falls back, the WRITE path is what matters here
    r = subprocess.run([sys.executable, "-W", "ignore", str(QA / "llm_audit.py")], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, (r.stdout[-1500:], r.stderr[-1500:])
    report = next(out.glob("llm-audit-*.json")).read_text()
    assert '"e1"' in report and '"final": "completed"' in report and '"any_task_without_confirmation": 0' in report   # clarify → confirm → both humans complete tasks → plan completed
    assert "csrf" not in r.stdout.lower()


def test_evidence_script_runs_end_to_end_with_csrf_on(live, qa_db, tmp_path):
    h = qa_client.csrf_login(httpx.Client(base_url=live, timeout=30), OWNER)
    p = h.post("/api/plans", json={"text": "Pide a Javiera que verifique los materiales de la visita de Marta Rojas", "client_plan_id": "qa-ev-0001"}).json()["plan"]
    if p["status"] == "needs_clarification":
        p = h.post(f"/api/plans/{p['id']}/clarify", json={p["clarification"]["field"]: p["clarification"]["options"][0]["id"]}).json()["plan"]
    assert h.post(f"/api/plans/{p['id']}/confirm").status_code == 200
    out = tmp_path / "out"
    env = {**os.environ, "AICOS_QA_DB": str(qa_db), "AICOS_QA_URL": live, "AICOS_QA_OUT": str(out)}
    r = subprocess.run([sys.executable, "-W", "ignore", str(QA / "evidence.py"), p["id"]], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, (r.stdout[-1500:], r.stderr[-1500:])
    sse = (out / "evidence-sse.md").read_text()
    assert "COINCIDEN" in sse and "NO COINCIDEN" not in sse and "ACCIÓN: dueña confirma el plan" in sse and (out / "evidence-sqlite.md").exists()
