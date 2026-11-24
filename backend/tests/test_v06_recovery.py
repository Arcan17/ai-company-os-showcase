"""V0.6 — recuperación segura de ejecuciones. Una ejecución que estaba EN CURSO cuando el proceso murió no puede quedar «en ejecución» para siempre, y reiniciar
nunca puede repetir un efecto: se declara interrumpida y requiere revisión humana. Las pruebas matan procesos de verdad (SIGKILL)."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import lease
from app.api import create_app
from app.seed import DEMO_PASSWORD

BACKEND = Path(__file__).resolve().parents[1]
CHILD = r'''
import sys, time
from app.api import create_app
from app.engine.orchestrator import ChatInput
app = create_app(sys.argv[1])
r = app.state.orch.start_chat("clima-sur", ChatInput(message="Mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", name="Marta Rojas",
                              phone="+56 9 5555 0101", email=None, client_msg_id="crash-0001", retry_secret="crash-secret-0001-abcdefghij"))
print("RUN", r["run_id"], flush=True)
time.sleep(120)
'''


def _sql(url, sql, **p):
    from sqlalchemy import create_engine
    eng = create_engine(url)
    try:
        with eng.begin() as c:
            r = c.execute(text(sql), p)
            return r.fetchall() if r.returns_rows else r.rowcount
    finally:
        eng.dispose()


@pytest.fixture
def child(tmp_path):
    """A REAL server process that starts a run (paced, so it is mid-flight) — the test decides how it dies."""
    url = f"sqlite:///{tmp_path / 'crash.db'}"
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "AICOS_STEP_DELAY": "0.8", "AICOS_SYNC": "0", "AICOS_LLM_PROVIDER": "deterministic"}
    p = subprocess.Popen([sys.executable, "-W", "ignore", "-c", CHILD, url], cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        line = p.stdout.readline()
        assert line.startswith("RUN "), line
        run_id = line.split()[1]
        deadline = time.time() + 40
        while time.time() < deadline and (_sql(url, "SELECT count(*) FROM events WHERE run_id=:r", r=run_id)[0][0] < 2 or _sql(url, "SELECT request_id FROM runs WHERE id=:r", r=run_id)[0][0] == "-"):
            time.sleep(0.1)                                                                          # mid-flight AND already attached to its request
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=run_id)[0][0] == "running"
        yield p, url, run_id
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()


def test_a_run_left_running_by_a_killed_process_is_surfaced_for_review_not_resumed(child):
    p, url, run_id = child
    p.send_signal(signal.SIGKILL); p.wait()
    assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=run_id)[0][0] == "running"          # the orphan: nothing will ever move it
    events_before = _sql(url, "SELECT count(*) FROM events WHERE run_id=:r", r=run_id)[0][0]
    app = create_app(url)                                                                            # the restart
    st, phase = _sql(url, "SELECT status, phase FROM runs WHERE id=:r", r=run_id)[0]
    assert st == "failed"
    last = _sql(url, "SELECT type, data FROM events WHERE run_id=:r ORDER BY seq DESC LIMIT 1", r=run_id)[0]
    assert last[0] == "run.failed" and '"reason": "interrupted"' in last[1] and f'"phase": "{phase}"' in last[1]
    assert _sql(url, "SELECT count(*) FROM events WHERE run_id=:r", r=run_id)[0][0] == events_before + 1   # ONE explanatory event, nothing replayed
    assert _sql(url, "SELECT r.status FROM requests r JOIN runs u ON u.request_id=r.id WHERE u.id=:r", r=run_id) in ([("ejecucion_fallida",)], [])   # never looks healthy
    assert _sql(url, "SELECT outcome FROM audit_log WHERE action='runs.interrupted' AND actor_id=:r", r=run_id) == [("ok",)]
    app.state.eng.dispose()


def test_restarting_again_changes_nothing_and_a_customer_retry_creates_no_duplicates(child):
    p, url, run_id = child
    p.send_signal(signal.SIGKILL); p.wait()
    create_app(url).state.eng.dispose()
    snap = lambda: (_sql(url, "SELECT count(*) FROM events")[0][0], _sql(url, "SELECT count(*) FROM requests")[0][0], _sql(url, "SELECT count(*) FROM runs")[0][0],
                    _sql(url, "SELECT count(*) FROM audit_log WHERE action='runs.interrupted'")[0][0])
    first = snap()
    create_app(url).state.eng.dispose(); create_app(url).state.eng.dispose()
    assert snap() == first                                                                           # idempotent: a restart loop cannot spam or re-run
    app = create_app(url)
    r = TestClient(app).post("/api/public/clima-sur/chat", json={"message": "Mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.",
                                                                  "name": "Marta Rojas", "phone": "+56 9 5555 0101", "client_msg_id": "crash-0001",
                                                                  "retry_secret": "crash-secret-0001-abcdefghij"})
    assert r.status_code == 200 and r.json()["run_id"] == run_id and r.json()["deduplicated"] is True      # the customer pressing "send" again gets the SAME run
    assert snap()[1:3] == first[1:3]
    app.state.eng.dispose()


def test_a_live_second_instance_is_never_recovered_over(child):
    p, url, run_id = child                                                                           # the child is ALIVE and mid-run, holding the lease
    app = create_app(url)                                                                            # e.g. a preflight/tool opening the same database
    assert app.state.orch.keeper.held is False
    assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=run_id)[0][0] == "running"            # the live run was NOT declared dead
    app.state.eng.dispose()
    p.send_signal(signal.SIGKILL); p.wait()
    app2 = create_app(url)                                                                           # owner died: takeover is immediate (no need to wait for the TTL)
    assert app2.state.orch.keeper.held is True and _sql(url, "SELECT status FROM runs WHERE id=:r", r=run_id)[0][0] == "failed"
    app2.state.eng.dispose()


def test_human_wait_states_survive_a_restart_untouched(tmp_path):
    url = f"sqlite:///{tmp_path / 'wait.db'}"
    a = create_app(url); c = TestClient(a)
    r = c.post("/api/public/clima-sur/chat", json={"message": "Mi aire split no enfría, creo que le falta gas.", "name": "Marta", "phone": "+56 9 5555 0101", "client_msg_id": "w-0001"}).json()
    assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r["run_id"])[0][0] == "waiting_approval"
    a.state.eng.dispose()
    b = create_app(url)
    assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r["run_id"])[0][0] == "waiting_approval"       # waiting for a person is NOT an interruption
    assert _sql(url, "SELECT count(*) FROM audit_log WHERE action='runs.interrupted'")[0][0] == 0
    b.state.eng.dispose()


# ------------------------------------------------------------------ the lease itself
@pytest.fixture
def eng(tmp_path):
    return create_app(f"sqlite:///{tmp_path / 'l.db'}").state.eng


def test_lease_rules(eng):
    me, other = os.getpid(), os.getppid()                                                           # the parent process is a different, certainly-alive pid
    with eng.begin() as c:
        c.execute(text("DELETE FROM instance_lease"))
    assert lease.acquire(eng, "A", pid=other) is True                                                # empty → acquired
    assert lease.acquire(eng, "A", pid=other) is True                                                # same owner → still ours
    assert lease.acquire(eng, "B", pid=me) is False                                                  # a different, live process holds it with a fresh heartbeat
    assert lease.heartbeat(eng, "A") is True and lease.heartbeat(eng, "B") is False
    with eng.begin() as c:
        c.execute(text("UPDATE instance_lease SET heartbeat_at='2000-01-01T00:00:00.000+00:00'"))
    assert lease.acquire(eng, "B", pid=me) is True                                                   # stale heartbeat (hung or pid reused) → takeover
    assert lease.heartbeat(eng, "A") is False                                                        # the old owner learns it lost the lease
    with eng.begin() as c:
        c.execute(text("UPDATE instance_lease SET pid=:p"), {"p": 2_000_000_000})
    assert lease.acquire(eng, "C", pid=me) is True                                                   # dead pid → takeover even with a fresh heartbeat
    with eng.begin() as c:
        c.execute(text("UPDATE instance_lease SET host='otro-equipo'"))
    assert lease.acquire(eng, "D", pid=me) is False                                                  # other host: only the heartbeat can tell → fresh = alive
    with eng.begin() as c:
        c.execute(text("UPDATE instance_lease SET host=:h"), {"h": socket.gethostname()})
    assert lease.release(eng, "D") is False                                                          # only the owner can release
    assert lease.release(eng, "C") is True and lease.release(eng, "C") is False


def test_clean_shutdown_releases_the_lease(tmp_path):
    url = f"sqlite:///{tmp_path / 'clean.db'}"
    app = create_app(url)
    assert _sql(url, "SELECT count(*) FROM instance_lease")[0][0] == 1
    with TestClient(app):                                                                           # entering/leaving the context runs lifespan startup/shutdown
        pass
    assert _sql(url, "SELECT count(*) FROM instance_lease")[0][0] == 0


# ====================================================================== revisión del PR #12 (P1 + P2 de revisión automatizada y comentario del dueño)
import json
import threading

import httpx

SERVER = r'''
import os, sys
from app.api import create_app
import uvicorn
uvicorn.run(create_app(sys.argv[1]), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning", proxy_headers=False)
'''
INJECT = r'''
import os, signal, sys
from sqlalchemy import event
from sqlalchemy.engine import Engine
POINT = os.environ["KILL_AT"]
def die(): os.kill(os.getpid(), signal.SIGKILL)
@event.listens_for(Engine, "before_cursor_execute")
def hook(conn, cursor, statement, params, context, executemany):
    s = " ".join(statement.split())
    if POINT == "events" and s.startswith("INSERT INTO events"): die()                                  # run already UPDATEd, event not yet written
    if POINT == "requests" and s.startswith("UPDATE requests SET status='ejecucion_fallida'"): die()    # run UPDATEd + event written, request not yet updated
@event.listens_for(Engine, "commit")
def on_commit(conn):
    if POINT == "commit": die()                                                                          # every statement executed, COMMIT about to happen
from app.api import create_app
create_app(sys.argv[1])
print("SURVIVED", flush=True)
'''


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _spawn_server(url, step_delay="0.0", heartbeat="0.4"):
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "AICOS_STEP_DELAY": step_delay, "AICOS_SYNC": "0", "AICOS_LLM_PROVIDER": "deterministic", "AICOS_LEASE_HEARTBEAT": heartbeat,
           "AICOS_STATIC_DIR": str(BACKEND / "data" / "nonexistent-static")}
    p = subprocess.Popen([sys.executable, "-W", "ignore", "-c", SERVER, url, str(port)], cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if httpx.get(base + "/api/auth/demo-accounts", timeout=1).status_code == 200:
                return p, base
        except httpx.HTTPError:
            time.sleep(0.25)
    p.kill(); raise AssertionError("el servidor de prueba no arrancó")


def _consistent_interruption(url, run_id):
    """The invariant. A run is EITHER untouched (still running, no interruption event, request unchanged, no audit) OR fully interrupted (failed + exactly one event + request flagged + audit)."""
    st = _sql(url, "SELECT status FROM runs WHERE id=:r", r=run_id)[0][0]
    ev = _sql(url, "SELECT count(*) FROM events WHERE run_id=:r AND type='run.failed'", r=run_id)[0][0]
    rq = _sql(url, "SELECT r.status FROM requests r JOIN runs u ON u.request_id=r.id WHERE u.id=:r", r=run_id)
    au = _sql(url, "SELECT count(*) FROM audit_log WHERE action='runs.interrupted' AND actor_id=:r", r=run_id)[0][0]
    flagged = (not rq) or rq[0][0] == "ejecucion_fallida"
    if st == "running":
        return ev == 0 and au == 0 and (not rq or rq[0][0] != "ejecucion_fallida"), (st, ev, rq, au)
    return st == "failed" and ev == 1 and au == 1 and flagged, (st, ev, rq, au)


@pytest.mark.parametrize("kill_at", ["events", "requests", "commit"])
def test_interruption_is_all_or_nothing_even_if_the_process_dies_midway(child, tmp_path, kill_at):
    """revisión automatizada P2 (#12): run status, event, request and audit were four separate commits; a SIGKILL between them left `failed` without its event (or a healthy-looking request) and no startup ever repaired it."""
    p, url, run_id = child
    p.send_signal(signal.SIGKILL); p.wait()                                                           # the original server dies mid-run → an orphan `running`
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "KILL_AT": kill_at}
    r = subprocess.run([sys.executable, "-W", "ignore", "-c", INJECT, url], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == -signal.SIGKILL and "SURVIVED" not in r.stdout, (r.returncode, r.stdout, r.stderr[-400:])     # it really died inside the recovery
    ok, snap = _consistent_interruption(url, run_id)
    assert ok, f"estado parcial tras SIGKILL en «{kill_at}»: {snap}"
    app = create_app(url)                                                                              # a normal restart finishes the job exactly once
    ok, snap = _consistent_interruption(url, run_id)
    assert ok and snap[0] == "failed" and snap[1] == 1 and snap[3] == 1, snap
    app.state.eng.dispose()
    create_app(url).state.eng.dispose()
    assert _consistent_interruption(url, run_id)[1] == snap                                            # and a second restart changes nothing


def test_a_standby_instance_refuses_every_write_and_launches_nothing(tmp_path):
    """revisión automatizada P1 (#12): the second server skipped recovery but still accepted chats, executing runs nobody supervised or would ever recover."""
    url = f"sqlite:///{tmp_path / 'ha.db'}"
    a, base_a = _spawn_server(url)
    b, base_b = _spawn_server(url)
    try:
        assert httpx.get(base_a + "/api/health").json()["active"] is True and httpx.get(base_b + "/api/health").json()["active"] is False
        before = (_sql(url, "SELECT count(*) FROM runs")[0][0], _sql(url, "SELECT count(*) FROM requests")[0][0], _sql(url, "SELECT count(*) FROM messages")[0][0])
        chat = {"message": "Mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas", "phone": "+56 9 5555 0101", "client_msg_id": "ha-0001"}
        r = httpx.post(base_b + "/api/public/clima-sur/chat", json=chat)
        assert r.status_code == 503 and "Retry-After" in r.headers and "en espera" in r.json()["detail"]
        c = httpx.Client(base_url=base_b)                                                              # staff can still sign in and READ on the standby …
        tok = c.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": "demo1234"}).json().get("csrf_token")   # present once the access-hardening PR is merged
        hdr = {"X-CSRF-Token": tok} if tok else {}
        assert c.get("/api/dashboard").status_code == 200
        for path, body in (("/api/plans", {"text": "Revisa las solicitudes pendientes", "client_plan_id": "ha-plan-1"}), ("/api/assistant/chat", {"message": "hola"}),
                           ("/api/approvals/x/decide", {"decision": "approved"}), ("/api/tasks/x/complete", {"evidence": "evidencia suficiente aquí"})):
            assert c.post(path, json=body, headers=hdr).status_code == 503, path          # … but cannot start, continue or finish any work
        assert (_sql(url, "SELECT count(*) FROM runs")[0][0], _sql(url, "SELECT count(*) FROM requests")[0][0], _sql(url, "SELECT count(*) FROM messages")[0][0]) == before
        ok = httpx.post(base_a + "/api/public/clima-sur/chat", json=chat)                              # the holder works normally
        assert ok.status_code == 200
    finally:
        for x in (a, b):
            x.kill(); x.wait()


def test_failover_promotes_the_standby_recovers_once_and_a_third_start_never_touches_live_work(tmp_path):
    """Owner (#12): restarting the lease holder must not declare a LIVE run of another process failed, nor duplicate side effects."""
    url = f"sqlite:///{tmp_path / 'fo.db'}"
    chat = lambda mid: {"message": "Mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas", "phone": "+56 9 5555 0101", "client_msg_id": mid}
    a, base_a = _spawn_server(url, step_delay="0.6")
    b, base_b = _spawn_server(url, step_delay="0.6")
    procs = [a, b]
    base_requests = _sql(url, "SELECT count(*) FROM requests")[0][0]
    try:
        r1 = httpx.post(base_a + "/api/public/clima-sur/chat", json=chat("fo-0001")).json()["run_id"]       # R1 in flight on the holder
        time.sleep(2.0)
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r1)[0][0] == "running"
        a.send_signal(signal.SIGKILL); a.wait()                                                         # the holder dies
        for _ in range(60):                                                                             # the standby notices (dead pid) and is promoted
            if httpx.get(base_b + "/api/health").json()["active"]:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("el standby no fue promovido")
        assert _consistent_interruption(url, r1) == (True, ("failed", 1, _sql(url, "SELECT r.status FROM requests r JOIN runs u ON u.request_id=r.id WHERE u.id=:r", r=r1), 1))
        r2 = httpx.post(base_b + "/api/public/clima-sur/chat", json=chat("fo-0002"))                    # the promoted instance now does the work
        assert r2.status_code == 200 and r2.json()["run_id"] != r1
        r2 = r2.json()["run_id"]
        time.sleep(1.5)
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r2)[0][0] == "running"               # R2 is LIVE on B …
        c, base_c = _spawn_server(url, step_delay="0.6"); procs.append(c)                               # … while a third process (the "restarted holder") starts
        assert httpx.get(base_c + "/api/health").json()["active"] is False                              # it must stand by: B is alive
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r2)[0][0] in ("running", "waiting_approval")
        for _ in range(60):
            if _sql(url, "SELECT status FROM runs WHERE id=:r", r=r2)[0][0] == "waiting_approval":
                break
            time.sleep(0.5)
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r2)[0][0] == "waiting_approval"       # R2 finished its phase untouched
        assert _sql(url, "SELECT count(*) FROM events WHERE run_id=:r AND type='run.failed'", r=r2)[0][0] == 0
        with_request = _sql(url, "SELECT count(*), count(DISTINCT request_id) FROM runs WHERE request_id!='-'")[0]
        assert with_request[0] == with_request[1] and _sql(url, "SELECT count(*) FROM requests")[0][0] == base_requests + with_request[0]   # one request per run that reached one: nothing duplicated
        assert _sql(url, "SELECT count(*) FROM approvals WHERE run_id=:r", r=r2)[0][0] == 1
    finally:
        for x in procs:
            x.kill(); x.wait()


def test_losing_the_lease_stops_in_flight_runs_and_they_cannot_overwrite_the_new_owners_verdict(tmp_path, monkeypatch):
    """Owner (#12): contemplate lease loss DURING execution. A hung holder replaced by another must not keep producing effects or resurrect a run the new owner declared failed."""
    from app.config import settings
    monkeypatch.setattr(settings, "sync_runs", False); monkeypatch.setattr(settings, "step_delay", 0.25); monkeypatch.setattr(settings, "lease_heartbeat_seconds", 0.2)
    url = f"sqlite:///{tmp_path / 'loss.db'}"
    app = create_app(url)
    try:
        assert app.state.orch.keeper.held is True
        r = TestClient(app).post("/api/public/clima-sur/chat", json={"message": "Mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas",
                                                                      "phone": "+56 9 5555 0101", "client_msg_id": "loss-0001"}).json()["run_id"]
        time.sleep(0.6)
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r)[0][0] == "running"
        # another (live) instance takes the lease and, as the new owner, declares the orphan interrupted:
        _sql(url, "UPDATE instance_lease SET boot_id='boot_other', pid=:p, host=:h, heartbeat_at=:t", p=os.getppid(), h=socket.gethostname(),
             t=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="milliseconds"))
        _sql(url, "UPDATE runs SET status='failed' WHERE id=:r", r=r)
        for _ in range(40):
            if app.state.orch.keeper.held is False:
                break
            time.sleep(0.1)
        assert app.state.orch.keeper.held is False                                                       # the old holder noticed
        n_events = _sql(url, "SELECT count(*) FROM events WHERE run_id=:r", r=r)[0][0]
        time.sleep(1.5)                                                                                  # the in-flight thread would have emitted several more events by now
        assert _sql(url, "SELECT count(*) FROM events WHERE run_id=:r", r=r)[0][0] <= n_events + 1
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r)[0][0] == "failed"                   # never resurrected to waiting_approval/completed
    finally:
        app.state.orch.keeper.stop()
        app.state.eng.dispose()


def test_gate_and_backstop_without_a_second_process(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'gate.db'}"
    app = create_app(url); c = TestClient(app)
    assert c.get("/api/health").json() == {"ok": True, "active": True}
    app.state.orch.keeper.held = False                                                                   # standby
    body = {"message": "Mi aire split no enfría, creo que le falta gas.", "name": "Marta", "phone": "+56 9 5555 0101", "client_msg_id": "gate-0001"}
    assert c.post("/api/public/clima-sur/chat", json=body).status_code == 503 and _sql(url, "SELECT count(*) FROM runs")[0][0] == 0
    assert c.get("/api/auth/demo-accounts").status_code == 200 and c.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": "demo1234"}).status_code == 200
    # backstop: if the lease is lost in the instant between the HTTP check and the launch, the run is declared interrupted — never left `running` with no thread
    app.state.orch.keeper.held = True
    run = c.post("/api/public/clima-sur/chat", json={**body, "client_msg_id": "gate-0002"}).json()["run_id"]
    _sql(url, "UPDATE runs SET status='running', phase='intake' WHERE id=:r", r=run)
    app.state.orch.keeper.held = False
    app.state.orch._launch(run)
    assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=run)[0][0] == "failed" and _consistent_interruption(url, run)[0]


def test_a_run_declared_failed_by_someone_else_stops_even_before_the_lease_changes(tmp_path, monkeypatch):
    """The window between «another instance took over» and «this keeper notices» (up to one heartbeat): the run row is the fence. Once it is not `running`, the thread stops and never overwrites it."""
    from app.config import settings
    monkeypatch.setattr(settings, "sync_runs", False); monkeypatch.setattr(settings, "step_delay", 0.25); monkeypatch.setattr(settings, "lease_heartbeat_seconds", 60)
    url = f"sqlite:///{tmp_path / 'fence.db'}"
    app = create_app(url)
    try:
        r = TestClient(app).post("/api/public/clima-sur/chat", json={"message": "Mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas",
                                                                      "phone": "+56 9 5555 0101", "client_msg_id": "fence-0001"}).json()["run_id"]
        time.sleep(0.6)
        assert app.state.orch.keeper.held is True and _sql(url, "SELECT status FROM runs WHERE id=:r", r=r)[0][0] == "running"
        _sql(url, "UPDATE runs SET status='failed' WHERE id=:r", r=r)                                    # the verdict of whoever owns the run now (lease untouched here)
        time.sleep(0.5)
        n = _sql(url, "SELECT count(*) FROM events WHERE run_id=:r", r=r)[0][0]
        time.sleep(1.5)
        assert _sql(url, "SELECT count(*) FROM events WHERE run_id=:r", r=r)[0][0] == n               # the thread produced nothing after the verdict …
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=r)[0][0] == "failed"                  # … and did not overwrite it
    finally:
        app.state.orch.keeper.stop(); app.state.eng.dispose()
