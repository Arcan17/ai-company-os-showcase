"""V0.6 — pruebas de la COMBINACIÓN de #10 (acceso/CSRF/sesiones), #11 (copias) y #12 (una instancia activa por base, recuperación).

Cada incremento tenía su CI verde por separado; estas pruebas fijan lo que solo existe cuando conviven:
  * precedencia 403 (CSRF) vs 503 (standby): la misma, en las dos instancias, sin tocar estado;
  * sesiones y token CSRF emitidos por una instancia siguen valiendo en la otra y tras un relevo por SIGKILL;
  * el lease es estado de ejecución: no viaja en una copia restaurada ni la deja arrancar «en espera».
"""
import os
import re
import signal
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import backup
from app.api import create_app
from app.config import settings
from app.seed import DEMO_PASSWORD

from .test_v06_recovery import _consistent_interruption, _spawn_server, _sql

OWNER = {"email": "dueno@climasur.test", "password": DEMO_PASSWORD}
WORK = ("plans", "runs", "requests", "messages", "approvals", "tasks", "events")        # business state: a refused request must leave all of it untouched
EVIL = {"Origin": "https://evil.example"}


def _work(url):
    return {t: _sql(url, f"SELECT count(*) FROM {t}")[0][0] for t in WORK}


def _plan(n):
    return {"text": "Revisa las solicitudes pendientes", "client_plan_id": "int-plan-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(n))}   # the API only accepts [A-Za-z0-9_-]


def _login(app):
    c = TestClient(app)
    r = c.post("/api/auth/login", json=OWNER)
    assert r.status_code == 200, r.text
    return c, r.json()["csrf_token"]


# --------------------------------------------------------------------------------------------- 403 vs 503, same answer on both instances
CASES = [  # name, headers factory(token), body-less modifiers → (status on the ACTIVE instance, status on the STANDBY instance)
    ("valid session + valid token", lambda t: {"X-CSRF-Token": t}, 200, 503),
    ("no token", lambda t: {"x-test-no-csrf": "1"}, 403, 403),
    ("wrong token", lambda t: {"X-CSRF-Token": "x" * len(t)}, 403, 403),
    ("empty token", lambda t: {"X-CSRF-Token": ""}, 403, 403),
    ("foreign Origin + valid token", lambda t: {"X-CSRF-Token": t, **EVIL}, 403, 403),
    ("Sec-Fetch-Site cross-site + valid token", lambda t: {"X-CSRF-Token": t, "Sec-Fetch-Site": "cross-site"}, 403, 403),
]


@pytest.mark.parametrize("role", ["active", "standby"])
@pytest.mark.parametrize("name,headers,want_active,want_standby", CASES, ids=[c[0] for c in CASES])
def test_csrf_and_standby_precedence_is_identical_on_both_instances_and_a_refusal_never_mutates(tmp_path, role, name, headers, want_active, want_standby):
    url = f"sqlite:///{tmp_path / 'prec.db'}"
    app = create_app(url)
    try:
        c, tok = _login(app)
        if role == "standby":
            app.state.orch.keeper.held = False                                                           # another live instance holds the lease
        before = _work(url)
        r = c.post("/api/plans", json=_plan(name), headers=headers(tok))
        want = want_active if role == "active" else want_standby
        assert r.status_code == want, (role, name, r.status_code, r.text)
        after = _work(url)
        if want == 200:
            assert after["plans"] == before["plans"] + 1                                               # the authorized write on the active instance really happened
        else:
            assert after == before, f"{role}/{name}: una petición rechazada ({r.status_code}) cambió el estado: {before} → {after}"
        if want == 503:
            assert r.headers.get("retry-after") and "en espera" in r.json()["detail"]
        if want == 403:
            assert "retry-after" not in r.headers and r.json()["detail"].startswith("csrf")           # a 403 is never presented as «try again later»
    finally:
        app.state.orch.keeper.stop(); app.state.eng.dispose()


def test_an_unauthenticated_write_is_401_on_the_active_and_503_on_the_standby_and_neither_touches_state(tmp_path):
    """No cookie → CSRF has nothing to forge and lets it through; the standby then refuses before auth. The role is already public (`/api/health`), nothing else is revealed."""
    url = f"sqlite:///{tmp_path / 'anon.db'}"
    app = create_app(url)
    try:
        anon = TestClient(app); before = _work(url)
        assert anon.post("/api/plans", json=_plan("anon")).status_code == 401
        app.state.orch.keeper.held = False
        r = anon.post("/api/plans", json=_plan("anon"))
        assert r.status_code == 503 and r.json()["detail"].startswith("Esta instancia está en espera")
        assert _work(url) == before
    finally:
        app.state.orch.keeper.stop(); app.state.eng.dispose()


def test_the_public_channel_is_refused_by_a_standby_but_never_needs_a_csrf_token(tmp_path):
    url = f"sqlite:///{tmp_path / 'pub.db'}"
    app = create_app(url)
    try:
        body = {"message": "Mi aire split no enfría, creo que le falta gas.", "name": "Marta", "phone": "+56 9 5555 0101", "client_msg_id": "int-pub-1"}
        anon = TestClient(app)
        assert anon.post("/api/public/clima-sur/chat", json=body).status_code == 200               # active: no cookie, no token (customer channel)
        app.state.orch.keeper.held = False
        before = _work(url)
        r = anon.post("/api/public/clima-sur/chat", json={**body, "client_msg_id": "int-pub-2"})
        assert r.status_code == 503 and _work(url) == before                                          # standby: refused with 503 (never 403), nothing started
    finally:
        app.state.orch.keeper.stop(); app.state.eng.dispose()


def test_a_standby_serves_reads_health_and_login_logout_without_creating_work_or_privileges(tmp_path):
    url = f"sqlite:///{tmp_path / 'sb.db'}"
    app = create_app(url)
    try:
        app.state.orch.keeper.held = False
        c = TestClient(app)
        assert c.get("/api/health").json() == {"ok": True, "active": False}
        before, sessions0 = _work(url), _sql(url, "SELECT count(*) FROM sessions")[0][0]
        assert c.post("/api/auth/login", json=OWNER, headers=EVIL).status_code == 403                  # login-CSRF protection is also in force on the standby
        assert _sql(url, "SELECT count(*) FROM sessions")[0][0] == sessions0
        r = c.post("/api/auth/login", json=OWNER)
        assert r.status_code == 200 and r.json()["csrf_token"]
        assert _sql(url, "SELECT count(*) FROM sessions")[0][0] == sessions0 + 1                     # a session is identity, not work
        assert c.get("/api/auth/me").status_code == 200 and c.get("/api/dashboard").status_code == 200
        assert c.post("/api/auth/login", json={**OWNER, "password": "no-es"}).status_code == 401       # wrong password: normal answer, not 503
        assert c.post("/api/auth/logout").status_code == 200 and c.get("/api/dashboard").status_code == 401
        assert _work(url) == before                                                                    # login/logout created no work
    finally:
        app.state.orch.keeper.stop(); app.state.eng.dispose()


def test_a_session_created_on_the_standby_is_valid_after_promotion_with_the_same_token(tmp_path):
    url = f"sqlite:///{tmp_path / 'promo.db'}"
    app = create_app(url)
    try:
        app.state.orch.keeper.held = False
        c, tok = _login(app)
        h = {"X-CSRF-Token": tok}
        assert c.post("/api/plans", json=_plan("p1"), headers=h).status_code == 503
        app.state.orch.keeper.held = True                                                            # promoted
        assert c.post("/api/plans", json=_plan("p1"), headers={"x-test-no-csrf": "1"}).status_code == 403   # still needs the token …
        assert c.post("/api/plans", json=_plan("p1"), headers=h).status_code == 200                       # … and the one issued while standby is the real one
    finally:
        app.state.orch.keeper.stop(); app.state.eng.dispose()


# --------------------------------------------------------------------------------------------- two real processes
def test_two_real_processes_share_sessions_and_csrf_and_a_sigkill_failover_keeps_both_intact(tmp_path):
    url = f"sqlite:///{tmp_path / 'two.db'}"
    a, base_a = _spawn_server(url, step_delay="0.6")
    b, base_b = _spawn_server(url, step_delay="0.6")
    procs = [a, b]
    chat = {"message": "Mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas", "phone": "+56 9 5555 0101", "client_msg_id": "two-0001"}
    try:
        assert httpx.get(base_a + "/api/health").json()["active"] is True and httpx.get(base_b + "/api/health").json()["active"] is False
        ca = httpx.Client(base_url=base_a)
        tok = ca.post("/api/auth/login", json=OWNER).json()["csrf_token"]
        cookie = {"aicos_session": ca.cookies["aicos_session"]}
        cb = httpx.Client(base_url=base_b, cookies=cookie)                                            # the SAME session presented to the other process
        base0 = _work(url)
        assert cb.get("/api/dashboard").status_code == 200                                              # shared session: reads work on the standby
        assert cb.post("/api/plans", json=_plan("b1"), headers={"X-CSRF-Token": tok}).status_code == 503
        assert cb.post("/api/plans", json=_plan("b2")).status_code == 403                               # no token: 403 on the standby too (not 503)
        assert cb.post("/api/plans", json=_plan("b3"), headers={"X-CSRF-Token": tok, **EVIL}).status_code == 403
        assert _work(url) == base0
        assert ca.post("/api/plans", json=_plan("a1"), headers={"X-CSRF-Token": tok}).status_code == 200  # the holder does authorized work
        run = httpx.post(base_a + "/api/public/clima-sur/chat", json=chat).json()["run_id"]            # and a run is in flight on it
        time.sleep(2.0)
        assert _sql(url, "SELECT status FROM runs WHERE id=:r", r=run)[0][0] == "running"
        a.send_signal(signal.SIGKILL); a.wait()
        for _ in range(60):
            if httpx.get(base_b + "/api/health").json()["active"]:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("el standby no fue promovido")
        ok, snap = _consistent_interruption(url, run)
        assert ok and snap[0] == "failed" and snap[1] == 1 and snap[3] == 1, snap                      # reconciled once, atomically
        assert cb.post("/api/plans", json=_plan("b4")).status_code == 403                              # promoted: CSRF still enforced …
        assert cb.post("/api/plans", json=_plan("b5"), headers={"X-CSRF-Token": tok}).status_code == 200   # … and the session/token of the dead instance keep working
        assert _sql(url, "SELECT count(*) FROM plans")[0][0] == base0["plans"] + 2                       # exactly a1 (holder) and b5 (promoted): b1/b2/b3 left nothing behind
    finally:
        for x in procs:
            x.kill(); x.wait()


# --------------------------------------------------------------------------------------------- backups of a database that has a live lease
def _other_live_owner(url):
    """Make the lease look held by a DIFFERENT live process (the parent of this test process) with a fresh heartbeat — what a copy of a running server's database carries."""
    _sql(url, "UPDATE instance_lease SET boot_id='boot_source', pid=:p, host=:h, heartbeat_at=:t", p=os.getppid(), h=socket.gethostname(),
         t=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))


@pytest.fixture
def running_source(tmp_path):
    """The database of a server that is up right now: lease row present, owner alive, heartbeat fresh, a signed-in user."""
    url = f"sqlite:///{tmp_path / 'src.db'}"
    app = create_app(url)
    _login(app)
    _other_live_owner(url)
    app.state.orch.keeper.stop(); app.state.eng.dispose()
    _other_live_owner(url)                                                                           # stop() released the row: put the foreign owner back
    assert _sql(url, "SELECT count(*) FROM instance_lease")[0][0] == 1
    return tmp_path / "src.db"


def _lease_rows(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT count(*) FROM instance_lease").fetchone()[0]
    finally:
        con.close()


def test_a_restored_copy_never_carries_the_source_servers_lease(running_source, tmp_path):
    m = backup.create(running_source, tmp_path / "bk")
    target = tmp_path / "restored.db"
    backup.restore(tmp_path / "bk" / m["file"], target)
    assert _lease_rows(target) == 0, "la copia restaurada heredó el lease del servidor de origen"
    kept = tmp_path / "restored-keep.db"
    backup.restore(tmp_path / "bk" / m["file"], kept, revoke_sessions=False)                          # --keep-sessions opts out of session revocation, never of the lease
    assert _lease_rows(kept) == 0


def test_the_drill_boots_the_restored_copy_as_the_ACTIVE_instance_even_while_the_source_is_alive(running_source, tmp_path):
    res = backup.drill(running_source, tmp_path / "drill")
    assert res["ok"] and res["boot"]["active"] is True, res["boot"]                                   # it would serve and recover, not sit in standby on a stale foreign lease
    assert res["boot"]["sessions"] == 0                                                                # nothing from the past survives the restore


def test_boot_check_leaves_no_background_thread_and_no_lease_behind(running_source, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sync_runs", False)                                                  # the real server mode: the keeper runs in a background thread
    monkeypatch.setattr(settings, "lease_heartbeat_seconds", 0.1)
    m = backup.create(running_source, tmp_path / "bk")
    target = tmp_path / "restored.db"
    backup.restore(tmp_path / "bk" / m["file"], target)
    before = {t.ident for t in threading.enumerate() if t.name == "lease-keeper"}
    backup.boot_check(target)
    time.sleep(0.4)                                                                                    # a leaked keeper would beat several times by now
    leaked = [t for t in threading.enumerate() if t.name == "lease-keeper" and t.ident not in before]
    assert leaked == [], "boot_check dejó el hilo del lease vivo"
    assert _lease_rows(target) == 0                                                                    # the restored file itself is untouched (boot_check works on a copy)
