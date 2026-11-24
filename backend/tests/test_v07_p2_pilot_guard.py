"""V0.7 P2 (Issue #23) — el proveedor SIMULADO jamás puede iniciar sesión en un piloto real, y sin proveedor la aplicación no gana ninguna ruta nueva de identidad.
Escritas EN ROJO antes de implementar. Todo en bases temporales; ningún socket (una prueba lo PROHÍBE y recorre el flujo completo).
Garantías comprobadas aquí: (1) no existe NINGÚN valor de configuración que active el simulado; (2) inyectarlo en un arranque `pilot` detiene el proceso antes de abrir la base;
(3) sin proveedor inyectado no hay rutas de identidad (404) y el módulo simulado ni se importa; (4) con un proveedor «real» en el piloto la contraseña local queda desactivada.
LO QUE NO DEMUESTRAN: nada sobre WorkOS ni sobre un despliegue (no hay ninguno)."""
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import settings
from app.pilot import PilotConfigError

from .idp_util import FAIL_LOCATION, OK_LOCATION, FrozenClock, fake_provider, make_world, set_cookies
from .pilot_util import PILOT_HOST, PILOT_URL, PW, pilot_on

BACKEND = Path(__file__).resolve().parents[1]
IDP_ROUTES = {("POST", "/api/auth/idp/start"), ("GET", "/api/auth/callback"), ("POST", "/api/config/invitations"), ("GET", "/api/config/invitations"),
              ("POST", "/api/config/invitations/{iid}/revoke")}
ALWAYS_NEW = {("POST", "/api/auth/reauth")}                       # reautenticar con la contraseña existe SIEMPRE (no depende de ningún proveedor)


def routes(app) -> set:
    return {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops if m.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}}


class RealLike:
    """Un proveedor que NO es el simulado (solo para probar la lógica del piloto: «con un proveedor real operativo, la contraseña local se apaga»). Delegamos en el simulado para no escribir otro."""

    def __new__(cls):
        base = type(fake_provider())
        return type("RealLikeProvider", (base,), {"is_fake": False, "name": "reallike"})()


def no_db_created(path: Path) -> bool:
    return not path.exists()


# ───────────────────────────── nada lo activa por configuración ─────────────────────────────

def test_injecting_the_simulated_provider_into_a_pilot_start_is_refused_before_the_database_is_opened(tmp_path, monkeypatch):
    pilot_on(monkeypatch)
    with pytest.raises(PilotConfigError) as e:
        create_app(f"sqlite:///{tmp_path / 'p.db'}", identity_provider=fake_provider())
    msg = str(e.value)
    assert "simulado" in msg.lower() and PILOT_HOST not in msg
    assert no_db_created(tmp_path / "p.db")


@pytest.mark.parametrize("env", ["", "local", "pilot"])
@pytest.mark.parametrize("value", ["fake", "FAKE", "workos", "valor-raro-123"])
def test_no_configuration_value_selects_a_provider_in_this_version(tmp_path, monkeypatch, env, value):
    if env == "pilot":
        pilot_on(monkeypatch)
    else:
        monkeypatch.setattr(settings, "env", env, raising=False)
    monkeypatch.setattr(settings, "idp_provider", value)
    with pytest.raises(PilotConfigError) as e:
        create_app(f"sqlite:///{tmp_path / 'p.db'}")
    assert "AICOS_IDP_PROVIDER" in str(e.value) and value not in str(e.value)
    assert no_db_created(tmp_path / "p.db")


def test_a_refused_provider_start_is_a_clean_process_exit_with_code_78(tmp_path):
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}, "AICOS_DB_URL": f"sqlite:///{tmp_path / 'proc.db'}", "AICOS_STATIC_DIR": str(tmp_path / "nostatic"),
           "AICOS_STEP_DELAY": "0", "AICOS_ENV": "pilot", "AICOS_COOKIE_SECURE": "1", "AICOS_TRUSTED_ORIGINS": PILOT_HOST, "AICOS_IDP_PROVIDER": "fake"}
    p = subprocess.run([sys.executable, "-W", "ignore", "-c", "import app.main"], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 78 and "AICOS_IDP_PROVIDER" in p.stderr and "Traceback" not in p.stderr and not (tmp_path / "proc.db").exists()


@pytest.mark.parametrize("env_vars", [{}, {"AICOS_ENV": "pilot", "AICOS_COOKIE_SECURE": "1", "AICOS_TRUSTED_ORIGINS": PILOT_HOST}])
def test_a_normal_process_never_imports_the_simulated_provider_module(tmp_path, env_vars):
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}, "AICOS_DB_URL": f"sqlite:///{tmp_path / 'proc.db'}", "AICOS_STATIC_DIR": str(tmp_path / "nostatic"),
           "AICOS_STEP_DELAY": "0", **env_vars}
    p = subprocess.run([sys.executable, "-W", "ignore", "-c", "import sys, app.main; print('IMPORTED' if 'app.idp_fake' in sys.modules else 'ABSENT')"], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout.strip().endswith("ABSENT"), (p.stdout, p.stderr)


# ───────────────────────────── sin proveedor: ninguna superficie nueva ─────────────────────────────

@pytest.mark.parametrize("env", ["local", "pilot"])
def test_without_a_provider_there_are_no_identity_routes_and_they_answer_404(tmp_path, monkeypatch, env):
    w = make_world(tmp_path, monkeypatch, env=env, provider=None)
    base = routes(w.app)
    assert not (IDP_ROUTES & base), f"rutas de identidad registradas SIN proveedor: {sorted(IDP_ROUTES & base)}"
    assert ALWAYS_NEW <= base
    c = w.browser()
    assert c.post("/api/auth/idp/start", json={}).status_code in (404, 405)
    assert c.get("/api/auth/callback", params={"code": "x", "state": "y"}, follow_redirects=False).status_code == 404
    assert c.get("/api/config/invitations").status_code in (401, 404)
    owner = w.company("alfa")
    assert owner.get("/api/config/invitations").status_code == 404 and owner.post("/api/config/invitations", json={"email": "a@alfa.test", "name": "Ana", "role": "technician"}).status_code in (404, 405)


def test_with_a_provider_the_route_table_grows_by_exactly_the_documented_routes_and_nothing_fake(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    base = routes(make_world(tmp_path / "a", monkeypatch, provider=None).app)
    with_idp = routes(make_world(tmp_path / "b", monkeypatch).app)
    assert with_idp - base == IDP_ROUTES, sorted((with_idp - base) ^ IDP_ROUTES)
    assert base - with_idp == set()
    assert not [p for _, p in with_idp if "fake" in p.lower() or "simul" in p.lower() or "_test" in p.lower() or "mock" in p.lower()], "una ruta del simulado quedó expuesta"


def test_every_identity_route_with_an_identifier_is_known_to_the_tenant_guard(tmp_path, monkeypatch):
    """Si alguien añade una ruta de identidad con un id, esta lista (y su caso de aislamiento en test_v07_p2_invitations) debe crecer a propósito."""
    from .test_v07_cross_tenant_matrix import id_surface
    w = make_world(tmp_path, monkeypatch)
    surface = id_surface(w.app)
    with_ids = {k for k in IDP_ROUTES if surface[k]["path"] or surface[k]["query"] or surface[k]["body"]}
    assert with_ids == {("POST", "/api/config/invitations/{iid}/revoke")}, with_ids


# ───────────────────────────── ningún socket ─────────────────────────────

def test_the_whole_flow_runs_with_every_network_connection_forbidden(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("el flujo del proveedor simulado intentó abrir una conexión de red")
    w = make_world(tmp_path, monkeypatch)
    owner = w.company("alfa")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    inv = w.invite(owner, "ana@alfa.test")
    _, resp, _ = w.accept(inv["invite_token"], "ana@alfa.test")
    assert resp.headers["location"] == OK_LOCATION


# ───────────────────────────── piloto con un proveedor «real»: la contraseña local se apaga ─────────────────────────────

@pytest.fixture
def pw(tmp_path, monkeypatch):
    FrozenClock().install(monkeypatch)
    return make_world(tmp_path, monkeypatch, env="pilot", provider=RealLike())


def test_a_pilot_with_a_real_provider_disables_local_passwords_without_spending_throttle(pw):
    owner = pw.company("alfa")                                                                 # (sesión creada en la base: la contraseña ya no es una puerta)
    attempts_before = pw.count("login_attempts")
    for body in ({"email": "dueno@alfa.test", "password": PW}, {"email": "nadie@alfa.test", "password": "x" * 12}):
        r = TestClient(pw.app, base_url=PILOT_URL).post("/api/auth/login", json=body)
        assert r.status_code == 403 and "proveedor de identidad" in r.json()["detail"]
    assert pw.count("login_attempts") == attempts_before, "el acceso apagado no debe gastar ni registrar intentos"
    assert owner.post("/api/auth/reauth", json={"password": PW}).status_code == 403


def test_a_pilot_with_a_real_provider_still_runs_the_flow_with_host_cookies_and_headers(pw):
    owner = pw.company("alfa")
    inv = pw.invite(owner, "ana@alfa.test")
    http = pw.browser()
    r = pw.start(http, {"invite_token": inv["invite_token"]})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and "strict-transport-security" in r.headers
    flow = next(c for c in set_cookies(r) if c.startswith("__Host-aicos_flow="))
    assert "Secure" in flow and "HttpOnly" in flow and "Path=/" in flow and "Domain" not in flow and "samesite=lax" in flow.lower()
    back = pw.prov.login(r.json()["authorize_url"], pw.prov.account("ana@alfa.test"))
    resp = http.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == OK_LOCATION
    session = next(c for c in set_cookies(resp) if c.startswith("__Host-aicos_session=") and "Max-Age=0" not in c)
    assert "Secure" in session and "HttpOnly" in session and "Domain" not in session
    assert resp.headers["cache-control"] == "no-store" and resp.headers["referrer-policy"] == "no-referrer"
    assert http.get("/api/auth/me").json()["company_id"] == owner.company_id


def test_a_host_that_is_not_declared_never_reaches_the_identity_routes(pw):
    evil = TestClient(pw.app, base_url="https://otro.example.test")
    assert evil.post("/api/auth/idp/start", json={}).status_code == 400
    assert evil.get("/api/auth/callback", params={"code": "x", "state": "y"}, follow_redirects=False).status_code == 400


def test_the_pilot_without_a_provider_keeps_working_with_passwords_exactly_as_in_p1(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, env="pilot", provider=None)
    w.company("alfa")
    r = TestClient(w.app, base_url=PILOT_URL).post("/api/auth/login", json={"email": "dueno@alfa.test", "password": PW})
    assert r.status_code == 200


# ───────────────────────────── ventana de reautenticación ─────────────────────────────

@pytest.mark.parametrize("value", [0, -5, 901, 3600])
def test_a_pilot_refuses_a_reauthentication_window_longer_than_fifteen_minutes_or_invalid(tmp_path, monkeypatch, value):
    pilot_on(monkeypatch)
    monkeypatch.setattr(settings, "reauth_max_age_seconds", value)
    with pytest.raises(PilotConfigError) as e:
        create_app(f"sqlite:///{tmp_path / 'p.db'}")
    assert "AICOS_REAUTH_MAX_AGE_SECONDS" in str(e.value) and f"={value}" not in str(e.value)
    assert no_db_created(tmp_path / "p.db")


@pytest.mark.parametrize("value", [1, 60, 900])
def test_a_pilot_accepts_a_window_up_to_fifteen_minutes(tmp_path, monkeypatch, value):
    pilot_on(monkeypatch)
    monkeypatch.setattr(settings, "reauth_max_age_seconds", value)
    assert create_app(f"sqlite:///{tmp_path / 'ok.db'}")
