import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import settings
from app.seed import DEMO_PASSWORD


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(settings, "step_delay", 0)
    monkeypatch.setattr(settings, "sync_runs", True)
    monkeypatch.setattr(settings, "llm_provider", "deterministic")


@pytest.fixture(autouse=True)
def auto_csrf(monkeypatch):
    """The SPA sends `X-CSRF-Token` on every write; the ~150 pre-V0.6 tests were written before that rule. This test-only shim adds the token the server issued to
    the session the client already holds. Tests that exercise CSRF itself send `x-csrf-token` (even wrong/empty) or `x-test-no-csrf` and are left alone."""
    orig = TestClient.request

    def request(self, method, url, **kw):
        h = {k.lower() for k in (kw.get("headers") or {})} | {k.lower() for k in self.headers}   # a client that carries its OWN token header (qa_client.csrf_login) is left alone
        if method.upper() not in ("GET", "HEAD", "OPTIONS") and not h & {"x-csrf-token", "x-test-no-csrf"} and any(c.name == "aicos_session" for c in self.cookies.jar):
            me = orig(self, "GET", "/api/auth/me")
            if me.status_code == 200 and me.json().get("csrf_token"):
                kw["headers"] = {**(kw.get("headers") or {}), "X-CSRF-Token": me.json()["csrf_token"]}
        return orig(self, method, url, **kw)

    monkeypatch.setattr(TestClient, "request", request)


@pytest.fixture
def dbfile(tmp_path):
    return f"sqlite:///{tmp_path / 't.db'}"


@pytest.fixture
def app(dbfile):
    return create_app(dbfile)


def login(app, email):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def owner(app):
    return login(app, "dueno@climasur.test")


@pytest.fixture
def admin(app):
    return login(app, "admin@climasur.test")


@pytest.fixture
def tech(app):
    return login(app, "tecnico@climasur.test")


@pytest.fixture
def norte(app):
    return login(app, "dueno@motornorte.test")


@pytest.fixture
def anon(app):
    return TestClient(app)


GAS = {"message": "Hola, mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas", "phone": "+56 9 5555 0101"}
PLACA = {"message": "Mi aire no enciende y la pantalla muestra error E6", "name": "Pedro Gálvez", "phone": "+56 9 5555 0102"}


def chat(anon, slug="clima-sur", body=None, mid="msg-test-0001", secret=None):
    """`secret` = el `retry_secret` del cliente (Issue #20): solo con él un reintento del mismo `mid` recupera la misma conversación."""
    r = anon.post(f"/api/public/{slug}/chat", json={**(body or GAS), "client_msg_id": mid, **({"retry_secret": secret} if secret else {})})
    assert r.status_code == 200, r.text
    return r.json()


def events(client, run_id):
    return client.get(f"/api/runs/{run_id}/events").json()["events"]


def pending_approval(client):
    return client.get("/api/dashboard").json()["pending_approvals"][0]
