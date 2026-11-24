"""Commercial-demo environment (Issue #3): isolated fixture, safety guards, demo-only endpoints, scripted run through the real API."""
import hashlib
import os
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import create_app
from app.config import settings
from app.db import make_engine
from app.demo_autoplay import play
from app.demo_preflight import main as preflight_main
from app.demo_seed import DemoSafetyError, assert_demo_path, build, main as seed_main, summary
from app.seed import DEMO_PASSWORD

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture
def demo_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "commit", "abc1234")
    return str(tmp_path / "demo_comercial_test.sqlite3")


@pytest.fixture
def demo_app(demo_db):
    build(make_engine(f"sqlite:///{demo_db}"))
    return create_app(f"sqlite:///{demo_db}")


def count(app, sql):
    with app.state.eng.connect() as c:
        return c.execute(text(sql)).scalar()


# ---------------- fixture: clean, ficticious, reproducible, idempotent ----------------
def test_fixture_is_clean_and_matches_the_script_preconditions(demo_db):
    eng = make_engine(f"sqlite:///{demo_db}")
    s = build(eng)
    assert s["open_marta_maintenance"] == 1 and s["open_requests"] == 2 and s["requests"] == 4          # one open maintenance; Pedro's repair; two historical
    assert (s["plans"], s["tasks"], s["runs"], s["failed_runs"], s["pending_approvals"]) == (0, 0, 0, 0, 0)   # no historical failures or alerts
    assert s["users"] == 3 and s["kit_mant_stock"] == 6 and s["gas_stock"] == 12                       # owner/admin/technician, coherent stock
    assert (s["visits_proposed"], s["visits_confirmed"], s["messages_sent"], s["messages_prepared_not_sent"]) == (1, 0, 0, 1)


def test_fixture_is_idempotent_and_never_duplicates(demo_db):
    eng = make_engine(f"sqlite:///{demo_db}")
    first = build(eng)
    tables = ("requests", "customers", "parts", "quotes", "visits", "work_orders", "messages", "conversations", "users", "agents", "audit_log")
    def snap():
        with eng.connect() as c:
            return {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in tables}
    a = snap()
    assert build(eng) == first and build(eng) == first
    assert snap() == a                                                                                   # 3 runs, same rows


def test_destroy_and_recreate_gives_the_same_fixture(tmp_path):
    p = tmp_path / "demo_comercial_x.sqlite3"
    s1 = build(make_engine(f"sqlite:///{p}"))
    os.remove(p)
    assert build(make_engine(f"sqlite:///{p}")) == s1


# ---------------- safety: the lab database can never be touched ----------------
@pytest.mark.parametrize("name", ["aicos.db", "audit.db", "e2e.db", "demo.sqlite3", "mi_demo_comercial.sqlite3", "demo_comercial.sqlite3-wal"])
def test_only_demo_comercial_databases_are_accepted(name):
    with pytest.raises(DemoSafetyError):
        assert_demo_path(f"/x/backend/data/{name}")
    assert assert_demo_path("sqlite:///data/demo_comercial.sqlite3").endswith("demo_comercial.sqlite3")
    assert assert_demo_path("/x/demo_comercial_planb.sqlite3").endswith("planb.sqlite3")


def test_seed_cli_refuses_the_lab_db_without_touching_it(tmp_path):
    lab = tmp_path / "aicos.db"
    lab.write_bytes(b"lab data")
    h = hashlib.sha256(lab.read_bytes()).hexdigest()
    with pytest.raises(DemoSafetyError):
        seed_main(["--db", str(lab)])
    assert hashlib.sha256(lab.read_bytes()).hexdigest() == h and lab.read_bytes() == b"lab data"


def test_demo_mode_refuses_to_boot_on_a_non_demo_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    with pytest.raises(DemoSafetyError):
        create_app(f"sqlite:///{tmp_path / 'aicos.db'}")
    assert not (tmp_path / "aicos.db").exists()


# ---------------- demo-only endpoints ----------------
def test_demo_endpoints_exist_only_in_demo_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    c = TestClient(create_app(f"sqlite:///{tmp_path / 'lab.db'}"))
    assert c.get("/api/demo/info").json() == {"demo": False}
    assert c.get("/demo").status_code == 404 and c.get("/demo-login/owner", follow_redirects=False).status_code == 404


def test_demo_info_page_and_one_click_login(demo_app):
    c = TestClient(demo_app)
    info = c.get("/api/demo/info").json()
    assert info["demo"] is True and info["db"] == "demo_comercial_test.sqlite3" and info["mode"] == "deterministic" and info["commit"] == "abc1234" and "/" not in info["db"]
    html = c.get("/demo").text
    assert "DEMO COMERCIAL" in html and "dueno.localhost" in html and "admin.localhost" in html and "tecnico.localhost" in html and "no están conectados" in html
    for persona, role in (("owner", "owner"), ("admin", "admin"), ("tech", "technician")):
        u = TestClient(demo_app)
        r = u.get(f"/demo-login/{persona}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/#/dashboard" and "httponly" in r.headers["set-cookie"].lower()
        assert u.get("/api/auth/me").json()["role"] == role and u.get("/api/auth/me").json()["company"]["slug"] == "clima-sur"
    assert c.get("/demo-login/hacker", follow_redirects=False).status_code == 404
    for bad in ("//evil.example", "/a?b=c", "http://x", "/../etc"):
        assert c.get("/demo-login/owner", params={"next": bad}, follow_redirects=False).status_code == 404
    assert c.get("/demo-login/owner", params={"next": "/coordinator"}, follow_redirects=False).headers["location"] == "/#/coordinator"


def test_demo_login_only_ever_opens_the_ficticious_clima_sur_accounts(demo_app):
    with demo_app.state.eng.connect() as c:
        assert {r[0] for r in c.execute(text("SELECT email FROM users WHERE is_demo=1")).fetchall() if r[0].endswith("@climasur.test")} == {"dueno@climasur.test", "admin@climasur.test", "tecnico@climasur.test"}
    u = TestClient(demo_app)
    u.get("/demo-login/owner", follow_redirects=False)
    assert u.get("/api/auth/me").json()["email"] == "dueno@climasur.test"


def test_json_declares_utf8_for_safari(demo_app):
    r = TestClient(demo_app).get("/api/auth/demo-accounts")
    assert r.headers["content-type"] == "application/json; charset=utf-8" and "demostración" in r.text


# ---------------- the 10-minute script, end to end, verified against SQLite ----------------
def test_guided_script_runs_end_to_end_and_matches_sqlite(demo_app, monkeypatch):
    monkeypatch.setattr(settings, "sync_runs", True); monkeypatch.setattr(settings, "step_delay", 0)
    res = play(demo_app)
    assert res["ok"] and res["proposed_steps"] == ["review_request", "ask_admin_verify_materials", "ask_technician_confirm", "report_to_owner"]
    assert res["tasks_before_confirm"] == 0 and res["final_status"] == "completed" and "KIT-MANT" in res["admin_stock_reply"] and "6 u." in res["admin_stock_reply"]
    pid = res["plan_id"]
    assert count(demo_app, f"SELECT COUNT(*) FROM tasks WHERE run_id='{pid}' AND status='hecha' AND length(evidence)>=10") == 2
    assert count(demo_app, f"SELECT COUNT(*) FROM delegations WHERE run_id='{pid}'") == 2
    assert count(demo_app, f"SELECT COUNT(*) FROM plan_steps WHERE plan_id='{pid}' AND status='done'") == 4
    assert count(demo_app, f"SELECT COUNT(*) FROM events WHERE run_id='{pid}'") == res["events"] > 25
    assert "No faltan materiales" in res["result"] and "necesita 1, stock 6" in res["result"]
    # forbidden request: blocked, no effects
    assert res["forbidden_status"] == "blocked" and count(demo_app, f"SELECT COUNT(*) FROM plan_steps WHERE plan_id='{res['forbidden_plan_id']}'") == 0
    # nothing external, nothing magical: the proposed visit stays proposed, the draft stays unsent, stock untouched
    s = summary(demo_app.state.eng)
    assert (s["visits_proposed"], s["visits_confirmed"], s["messages_sent"], s["messages_prepared_not_sent"], s["kit_mant_stock"], s["gas_stock"]) == (1, 0, 0, 1, 6, 12)
    assert count(demo_app, "SELECT COUNT(*) FROM approvals") == 0


def test_two_independent_sessions_per_hostname_never_mix(demo_app):
    a, b = TestClient(demo_app, base_url="http://dueno.localhost"), TestClient(demo_app, base_url="http://admin.localhost")
    a.get("/demo-login/owner", follow_redirects=False); b.get("/demo-login/admin", follow_redirects=False)
    assert a.get("/api/auth/me").json()["role"] == "owner" and b.get("/api/auth/me").json()["role"] == "admin"
    assert b.post("/api/plans", json={"text": "Revisa las solicitudes pendientes", "client_plan_id": "iso-0001"}).status_code == 403   # admin session has no owner power


# ---------------- preflight + launcher ----------------
def test_preflight_ready_on_fresh_fixture_and_fails_on_a_broken_one(demo_db, demo_app, capsys):
    assert preflight_main(["--db", demo_db]) == 0 and "PREFLIGHT OK" in capsys.readouterr().out
    with demo_app.state.eng.begin() as c:
        c.execute(text("UPDATE requests SET status='cerrada' WHERE category='mantencion' AND status='clasificada'"))
    assert preflight_main(["--db", demo_db]) == 1 and "EXACTAMENTE una mantención abierta" in capsys.readouterr().out
    with pytest.raises(DemoSafetyError):
        preflight_main(["--db", os.path.join(os.path.dirname(demo_db), "aicos.db")])


def test_launcher_reset_asks_for_confirmation_and_cancels_without_changes():
    sh = os.path.join(ROOT, "run_demo_comercial.sh")
    r = subprocess.run(["bash", sh, "reset"], input="no\n", capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 1 and "cancelado" in r.stderr
    st = subprocess.run(["bash", sh, "status"], capture_output=True, text=True, timeout=30, cwd=ROOT)
    assert "laboratorio :8731" in st.stdout and st.returncode == 0
    assert subprocess.run(["bash", sh, "borrar-todo"], capture_output=True, text=True, cwd=ROOT).returncode == 1
