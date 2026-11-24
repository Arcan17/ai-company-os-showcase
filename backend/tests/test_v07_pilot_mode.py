"""V0.7 P1 (Issue #19) — modo piloto `AICOS_ENV=pilot`: arranque seguro, sin demostración, cookie `__Host-`, cabeceras y rechazo de configuraciones inseguras.
Criterios AC1–AC4 de docs/v07/P0_ARQUITECTURA_Y_PLAN.md §6. Todo con bases TEMPORALES y empresas ficticias; nunca se abre una base de laboratorio.
Escritas EN ROJO antes de implementar (el commit rojo las deja fallando por la razón correcta: `AICOS_ENV=pilot` todavía no hace nada)."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import company_admin as cfg
from app.api import create_app
from app.config import settings
from app.pilot import PilotConfigError
from app.seed import DEMO_PASSWORD, seed

from .pilot_util import PILOT_HOST, PILOT_URL, PW, Session, make_company, pilot_on

BACKEND = Path(__file__).resolve().parents[1]
HIDDEN = [("GET", "/api/auth/demo-accounts"), ("GET", "/demo"), ("GET", "/demo-login/owner"), ("GET", "/demo-login/admin"), ("GET", "/api/demo/info"),
          ("POST", "/api/demo/reset-scenarios"), ("GET", "/api/setup/info"), ("POST", "/api/setup/companies")]


def count(app, table):
    with app.state.eng.connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def host_cookie_problems(set_cookie: str, name: str = "__Host-aicos_session") -> list[str]:
    """Las reglas del prefijo `__Host-` (RFC 6265bis §4.1.3) + las que exige el piloto. Lista vacía = cumple."""
    parts = [p.strip() for p in set_cookie.split(";")]
    attrs = {p.split("=")[0].lower(): (p.split("=", 1)[1] if "=" in p else True) for p in parts[1:]}
    bad = []
    if not parts[0].startswith(name + "="):
        bad.append(f"nombre distinto de {name}")
    for want in ("secure", "httponly"):
        if attrs.get(want) is not True:
            bad.append(f"falta {want}")
    if attrs.get("path") != "/":
        bad.append("Path distinto de /")
    if "domain" in attrs:
        bad.append("lleva Domain")
    if str(attrs.get("samesite", "")).lower() != "lax":
        bad.append("SameSite distinto de Lax")
    return bad


# ───────────────────────────── AC1: sin AICOS_ENV (o «local») NADA cambia ─────────────────────────────

@pytest.mark.parametrize("env", ["", "local"])
def test_without_aicos_env_the_behaviour_is_the_current_one(tmp_path, monkeypatch, env):
    """Guarda de regresión (pasa antes y después): la siembra de demostración, la cuenta de demo con su contraseña, la cookie sin prefijo y ninguna cabecera nueva."""
    monkeypatch.setattr(settings, "env", env, raising=False)
    app = create_app(f"sqlite:///{tmp_path / 'local.db'}")
    c = TestClient(app)
    assert count(app, "companies") == 2
    da = c.get("/api/auth/demo-accounts")
    assert da.status_code == 200 and da.json()["password"] == DEMO_PASSWORD
    assert c.get("/api/setup/info").status_code == 200 and c.get("/api/demo/info").json() == {"demo": False}
    r = c.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": DEMO_PASSWORD})
    assert r.status_code == 200
    sc = r.headers.get_list("set-cookie")[0]
    assert sc.startswith("aicos_session=") and "__Host-" not in sc and "Secure" not in sc                  # http, AICOS_COOKIE_SECURE=auto → sin Secure, como hoy
    h = {k.lower() for k in r.headers}
    assert not h & {"strict-transport-security", "content-security-policy", "content-security-policy-report-only", "x-frame-options", "referrer-policy", "x-content-type-options"}


# ───────────────────────────── AC2: piloto = base vacía y sin superficie de demostración ─────────────────────────────

def test_pilot_starts_empty_and_hides_the_demo_surface(tmp_path, monkeypatch):
    pilot_on(monkeypatch)
    app = create_app(f"sqlite:///{tmp_path / 'pilot.db'}")
    assert (count(app, "companies"), count(app, "users"), count(app, "agents")) == (0, 0, 0)                  # ni empresas ni cuentas de demostración
    c = TestClient(app, base_url=PILOT_URL)
    for method, path in HIDDEN:
        r = c.request(method, path, json={"confirm": "REINICIAR"} if method == "POST" else None)
        assert r.status_code == 404, (method, path, r.status_code)
        assert DEMO_PASSWORD not in r.text
    paths = {getattr(r, "path", "") for r in app.routes}                                                   # y no basta con responder 404: las rutas ni siquiera existen
    assert not {p for p in paths if p in ("/demo", "/api/auth/demo-accounts") or p.startswith(("/demo-login", "/api/demo/", "/api/setup/"))}, paths
    assert c.get("/api/health").status_code == 200                                                          # lo demás sigue en pie


def test_pilot_never_seeds_demo_data_even_if_seed_is_called_directly(tmp_path, monkeypatch):
    pilot_on(monkeypatch)
    app = create_app(f"sqlite:///{tmp_path / 'pilot.db'}")
    seed(app.state.eng)                                                                                    # quien llame a seed() por otra vía tampoco puede sembrar la demostración en el piloto
    assert count(app, "companies") == 0 and count(app, "users") == 0


def test_the_operator_cli_creates_the_first_company_and_that_is_the_only_way_in(tmp_path, monkeypatch, capsys):
    pilot_on(monkeypatch)
    db = tmp_path / "pilot.db"
    assert cfg.main(["create", "--db", str(db), "--name", "Empresa Piloto Ficticia", "--slug", "piloto-ficticio", "--owner-name", "Ana Prueba", "--owner-email", "ana@piloto.test",
                     "--owner-password", PW]) == 0
    app = create_app(f"sqlite:///{db}")                                                                    # la base con SU empresa (is_demo=0) sí arranca en piloto
    assert (count(app, "companies"), count(app, "users")) == (1, 1)
    assert Session(app).login("ana@piloto.test").get("/api/auth/me").json()["company"]["slug"] == "piloto-ficticio"


# ───────────────────────────── AC3: el piloto falla cerrado ─────────────────────────────

UNSAFE = [("AICOS_COOKIE_SECURE", dict(cookie_secure="auto")), ("AICOS_COOKIE_SECURE", dict(cookie_secure="0")), ("AICOS_TRUSTED_ORIGINS", dict(trusted_origins=())),
          ("AICOS_DEMO_MODE", dict(demo_mode=True)), ("AICOS_ALLOW_COMPANY_CREATION", dict(allow_company_creation=True)),
          ("AICOS_TRUSTED_PROXIES", dict(trusted_proxies=("0.0.0.0/0",))), ("AICOS_TRUSTED_PROXIES", dict(trusted_proxies=("203.0.113.7",))),
          ("AICOS_TRUSTED_PROXIES", dict(trusted_proxies=("10.0.0.0/8", "::/0"))), ("AICOS_ENV", dict(env="pilto"))]


@pytest.mark.parametrize("var,over", UNSAFE, ids=[f"{v}-{i}" for i, (v, _) in enumerate(UNSAFE)])
def test_pilot_refuses_every_unsafe_configuration_naming_the_variable_and_no_values(tmp_path, monkeypatch, var, over):
    pilot_on(monkeypatch, **over)
    db = tmp_path / "pilot.db"
    with pytest.raises(PilotConfigError) as e:
        create_app(f"sqlite:///{db}")
    msg = str(e.value)
    assert var in msg
    assert not any(v in msg for v in ("203.0.113", "pilto", "0.0.0.0")), msg                                # nombra la variable, NO imprime su valor
    assert not db.exists() and not list(tmp_path.iterdir()), "el rechazo debe ocurrir antes de abrir o crear la base"


def test_pilot_refuses_a_database_with_demo_accounts_and_leaves_it_untouched(tmp_path, monkeypatch):
    db = tmp_path / "lab.db"
    monkeypatch.setattr(settings, "env", "", raising=False)
    create_app(f"sqlite:///{db}")                                                                          # una base de laboratorio normal: dos empresas de demostración (is_demo=1)
    before, files = sha(db), sorted(p.name for p in tmp_path.iterdir())
    pilot_on(monkeypatch)
    with pytest.raises(PilotConfigError) as e:
        create_app(f"sqlite:///{db}")
    msg = str(e.value)
    assert "is_demo" in msg and "AICOS_DB_URL" in msg
    assert not any(s in msg for s in ("climasur", "motornorte", DEMO_PASSWORD, str(db))), msg               # ni correos, ni contraseña, ni la ruta
    assert sha(db) == before and sorted(p.name for p in tmp_path.iterdir()) == files                       # ni una migración, ni un -wal nuevo


def test_no_test_in_this_module_names_the_real_lab_databases():
    """Regla de seguridad del propio archivo: ninguna prueba nombra las bases del laboratorio (si la negativa del piloto fallara, ESA prueba escribiría en la base real). Se prueba con COPIAS en tmp_path."""
    src = Path(__file__).read_text()
    for forbidden in ("aicos" + ".db", "demo_" + "comercial", "BACKEND / " + '"data"', "backend" + "/data"):
        assert forbidden not in src, forbidden


def _run_main(tmp_path, **env):
    full = {**{k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}, "AICOS_DB_URL": f"sqlite:///{tmp_path / 'proc.db'}", "AICOS_STATIC_DIR": str(tmp_path / "nostatic"),
            "AICOS_STEP_DELAY": "0", **env}
    return subprocess.run([sys.executable, "-W", "ignore", "-c", "import app.main"], cwd=BACKEND, env=full, capture_output=True, text=True, timeout=60)


def test_a_refused_pilot_start_is_a_clean_non_zero_process_exit(tmp_path):
    p = _run_main(tmp_path, AICOS_ENV="pilot", AICOS_COOKIE_SECURE="auto", AICOS_TRUSTED_ORIGINS=PILOT_HOST)
    assert p.returncode == 78, (p.returncode, p.stderr)                                                    # EX_CONFIG
    assert "AICOS_COOKIE_SECURE" in p.stderr and "Traceback" not in p.stderr
    assert not (tmp_path / "proc.db").exists()


def test_a_valid_pilot_boots_in_a_real_process_with_an_empty_database(tmp_path):
    p = _run_main(tmp_path, AICOS_ENV="pilot", AICOS_COOKIE_SECURE="1", AICOS_TRUSTED_ORIGINS=PILOT_HOST)
    assert p.returncode == 0, p.stderr
    import sqlite3
    with sqlite3.connect(tmp_path / "proc.db") as db:
        assert db.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0 and db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


@pytest.mark.parametrize("proxies", [(), ("127.0.0.1",), ("10.0.0.0/8", "192.168.1.0/24", "fd00::/8")])
def test_pilot_accepts_only_private_or_loopback_trusted_proxies(tmp_path, monkeypatch, proxies):
    pilot_on(monkeypatch, trusted_proxies=proxies)
    assert create_app(f"sqlite:///{tmp_path / 'ok.db'}")


# ───────────────────────────── AC4: cookie __Host- y cabeceras ─────────────────────────────

@pytest.fixture
def pilot_app(tmp_path, monkeypatch):
    pilot_on(monkeypatch)
    app = create_app(f"sqlite:///{tmp_path / 'pilot.db'}")
    make_company(app, "piloto-uno", "Piloto Uno")
    return app


def test_pilot_cookie_and_headers(pilot_app):
    c = TestClient(pilot_app, base_url=PILOT_URL)
    r = c.post("/api/auth/login", json={"email": "dueno@piloto-uno.test", "password": PW})
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 1 and host_cookie_problems(cookies[0]) == [], cookies
    assert [k for k in c.cookies.keys()] == ["__Host-aicos_session"]
    assert c.get("/api/auth/me").status_code == 200                                                         # y la sesión funciona con ese nombre


def test_pilot_cookie_is_secure_even_over_plain_http_and_forwarded_headers_are_never_trusted(pilot_app):
    c = TestClient(pilot_app, base_url=f"http://{PILOT_HOST}")
    r = c.post("/api/auth/login", json={"email": "dueno@piloto-uno.test", "password": PW}, headers={"X-Forwarded-Proto": "http", "X-Forwarded-For": "198.51.100.9", "Forwarded": "proto=http"})
    assert r.status_code == 200 and host_cookie_problems(r.headers.get_list("set-cookie")[0]) == []        # en el piloto no se relaja ni para pruebas locales por http
    with pilot_app.state.eng.connect() as db:
        detail = db.execute(text("SELECT detail FROM audit_log WHERE action='auth.login' ORDER BY id DESC LIMIT 1")).scalar()
    assert "198.51.100.9" not in detail                                                                     # el registro usa la dirección real de la conexión, no una cabecera de internet


def test_pilot_cookie_stays_secure_even_if_the_setting_changes_after_startup(pilot_app, monkeypatch):
    """Defensa en profundidad: `Secure` no depende de que AICOS_COOKIE_SECURE siga valiendo 1 (el arranque ya lo exigió una vez)."""
    monkeypatch.setattr(settings, "cookie_secure", "0")
    r = TestClient(pilot_app, base_url=f"http://{PILOT_HOST}").post("/api/auth/login", json={"email": "dueno@piloto-uno.test", "password": PW})
    assert r.status_code == 200 and host_cookie_problems(r.headers.get_list("set-cookie")[0]) == []


def test_pilot_rejects_the_unprefixed_session_cookie(pilot_app):
    s = Session(pilot_app).login("dueno@piloto-uno.test")
    token = s.http.cookies.get("__Host-aicos_session")
    assert token
    assert TestClient(pilot_app, base_url=PILOT_URL, cookies={"__Host-aicos_session": token}).get("/api/auth/me").status_code == 200       # control
    assert TestClient(pilot_app, base_url=PILOT_URL, cookies={"aicos_session": token}).get("/api/auth/me").status_code == 401               # el mismo valor, nombre sin prefijo → no autentica


def test_pilot_logout_sends_a_valid_host_prefixed_deletion(pilot_app):
    s = Session(pilot_app).login("dueno@piloto-uno.test")
    r = s.post("/api/auth/logout")
    sc = r.headers.get_list("set-cookie")[0]
    assert sc.startswith("__Host-aicos_session=") and "Max-Age=0" in sc
    assert [p for p in host_cookie_problems(sc)] == []                                                     # un navegador ignora un borrado de `__Host-` que no sea Secure/Path=/: la cookie quedaría viva
    assert s.get("/api/auth/me").status_code == 401


SECURITY = {"strict-transport-security": "max-age=31536000; includeSubDomains", "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "x-frame-options": "DENY",
            "content-security-policy": "frame-ancestors 'none'"}


def test_pilot_security_headers_are_on_every_response(pilot_app):
    s = Session(pilot_app)
    bad_csrf = {"X-CSRF-Token": "no-es-el-token"}
    owner = Session(pilot_app).login("dueno@piloto-uno.test")
    cases = [(s.http, "GET", "/api/health", None, 200), (s.http, "GET", "/api/auth/me", None, 401), (s.http, "POST", "/api/auth/login", {"json": {"email": "x@y.test", "password": "mala-clave"}}, 401),
             (s.http, "POST", "/api/auth/login", {"json": {"email": "x@y.test", "password": "mala-clave"}, "headers": {"Origin": "https://evil.example"}}, 403),   # la rechaza CsrfMiddleware, no una ruta
             (s.http, "GET", "/api/ruta-que-no-existe", None, 404), (s.http, "GET", "/no-es-api", None, 404),
             (owner.http, "GET", "/api/dashboard", None, 200), (owner.http, "POST", "/api/assistant/chat", {"json": {"message": "hola"}, "headers": bad_csrf}, 403)]
    for client, method, path, kw, status in cases:
        r = client.request(method, path, **(kw or {}))
        assert r.status_code == status, (method, path, r.status_code)
        for k, v in SECURITY.items():
            assert r.headers.get(k) == v, (method, path, k, r.headers.get(k))
        ro = r.headers.get("content-security-policy-report-only", "")
        assert all(d in ro for d in ("default-src 'self'", "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'")), (method, path, ro)
        if path.startswith("/api/"):
            assert r.headers.get("cache-control") == "no-store", (method, path, r.headers.get("cache-control"))


def test_pilot_only_answers_to_the_declared_host(pilot_app):
    c = TestClient(pilot_app, base_url=PILOT_URL)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/health", headers={"Host": PILOT_HOST.upper()}).status_code == 200                    # mayúsculas: el nombre de host no distingue
    for bad in ("evil.example", f"{PILOT_HOST}:8443", f"{PILOT_HOST}.evil.example", "localhost"):
        r = c.get("/api/health", headers={"Host": bad})
        assert r.status_code == 400 and "host" in r.text.lower(), (bad, r.status_code)
        assert r.headers.get("strict-transport-security")                                                   # el rechazo también lleva las cabeceras


def test_pilot_host_allowlist_accepts_every_declared_origin_including_a_port(tmp_path, monkeypatch):
    pilot_on(monkeypatch, trusted_origins=(PILOT_HOST, "pilot2.example.test:8443"))
    app = create_app(f"sqlite:///{tmp_path / 'multi.db'}")
    c = TestClient(app, base_url=PILOT_URL)
    assert c.get("/api/health", headers={"Host": "pilot2.example.test:8443"}).status_code == 200
    assert c.get("/api/health", headers={"Host": "pilot2.example.test"}).status_code == 400
