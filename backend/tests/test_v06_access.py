"""V0.6 — acceso: sesiones, CSRF, protección del login, Host/proxy. Cada prueba nombra el abuso que cierra."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import security
from app.config import settings
from app.seed import DEMO_PASSWORD

OWNER = "dueno@climasur.test"
NOCSRF = {"x-test-no-csrf": "1"}


def _login(app, email=OWNER, pw=DEMO_PASSWORD, base_url="http://testserver", headers=None):
    c = TestClient(app, base_url=base_url)
    r = c.post("/api/auth/login", json={"email": email, "password": pw}, headers=headers or {})
    return c, r


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="milliseconds")


def _sql(app, sql, **p):
    with app.state.eng.begin() as c:
        r = c.execute(text(sql), p)
        return r.fetchall() if r.returns_rows else r.rowcount


@pytest.fixture
def tight(monkeypatch):
    monkeypatch.setattr(settings, "login_max_pair", 3)
    monkeypatch.setattr(settings, "login_max_email", 6)
    monkeypatch.setattr(settings, "login_max_ip", 8)


# ------------------------------------------------------------------ CSRF
def test_csrf_write_with_cookie_but_without_token_is_rejected(app):
    c, r = _login(app)
    assert r.status_code == 200 and r.json()["csrf_token"]
    denied = c.post("/api/assistant/chat", json={"message": "mis notas"}, headers=NOCSRF)
    assert denied.status_code == 403 and "csrf" in denied.json()["detail"].lower()
    assert c.post("/api/assistant/chat", json={"message": "mis notas"}, headers={"x-csrf-token": "x" * 43}).status_code == 403
    assert c.post("/api/assistant/chat", json={"message": "mis notas"}, headers={"x-csrf-token": ""}).status_code == 403
    assert c.post("/api/assistant/chat", json={"message": "mis notas"}, headers={"x-csrf-token": r.json()["csrf_token"]}).status_code == 200


def test_csrf_token_is_bound_to_its_session(app):
    a, ra = _login(app)
    b, rb = _login(app, "admin@climasur.test")
    assert ra.json()["csrf_token"] != rb.json()["csrf_token"]
    assert a.post("/api/assistant/chat", json={"message": "hola"}, headers={"x-csrf-token": rb.json()["csrf_token"]}).status_code == 403
    # /me hands the same token back to the session that owns it (SPA reload) and never anybody else's
    assert a.get("/api/auth/me").json()["csrf_token"] == ra.json()["csrf_token"]


def test_csrf_not_needed_for_reads_or_cookieless_public_channel(app, anon):
    c, _ = _login(app)
    assert c.get("/api/dashboard").status_code == 200 and c.get("/api/auth/me").status_code == 200
    r = anon.post("/api/public/clima-sur/chat", json={"message": "no enciende", "name": "Pedro", "phone": "+56 9 5555 0102", "client_msg_id": "csrf-pub-0001"})
    assert r.status_code == 200   # no ambient authority → nothing to forge; the customer channel stays open


def test_csrf_public_channel_is_open_even_with_a_stale_session_cookie(app):
    c, _ = _login(app)
    r = c.post("/api/public/clima-sur/chat", json={"message": "no enciende", "name": "Pedro", "phone": "+56 9 5555 0102", "client_msg_id": "csrf-pub-0002"}, headers=NOCSRF)
    assert r.status_code == 200


@pytest.mark.parametrize("hdrs,ok", [
    ({"origin": "http://evil.example"}, False),
    ({"origin": "null"}, False),
    ({"origin": "http://testserver:9999"}, False),                       # same host, other port = other origin
    ({"origin": "http://testserver"}, True),
    ({"sec-fetch-site": "cross-site"}, False),
    ({"sec-fetch-site": "same-site"}, False),                            # sibling sub-domain (the demo's dueno./admin./tecnico.localhost)
    ({"sec-fetch-site": "same-origin"}, True),
    ({"sec-fetch-site": "same-origin", "origin": "http://evil.example"}, False),
    ({}, True),                                                          # non-browser client: the token is still required, the origin signal is just absent
])
def test_csrf_browser_origin_signals(app, hdrs, ok):
    c, r = _login(app)
    res = c.post("/api/assistant/chat", json={"message": "hola"}, headers={"x-csrf-token": r.json()["csrf_token"], **hdrs})
    assert (res.status_code == 200) is ok, (hdrs, res.status_code, res.text)


def test_login_csrf_cross_origin_login_is_rejected(app):
    """An attacker page must not be able to sign the victim into the attacker's account."""
    _, r = _login(app, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    _, r = _login(app, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert _login(app, headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"})[1].status_code == 200


def test_trusted_origins_are_explicit(app, monkeypatch):
    c, r = _login(app)
    h = {"x-csrf-token": r.json()["csrf_token"], "origin": "https://app.aire.example"}
    assert c.post("/api/assistant/chat", json={"message": "hola"}, headers=h).status_code == 403
    monkeypatch.setattr(settings, "trusted_origins", ("app.aire.example",))
    assert c.post("/api/assistant/chat", json={"message": "hola"}, headers=h).status_code == 200


# ------------------------------------------------------------------ sessions
def test_logout_invalidates_the_session_and_its_token(app):
    c, r = _login(app)
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401
    assert _sql(app, "SELECT count(*) FROM sessions WHERE user_id=(SELECT id FROM users WHERE email=:e)", e=OWNER)[0][0] == 0


def test_idle_timeout_and_sliding_activity(app):
    c, _ = _login(app)
    _sql(app, "UPDATE sessions SET last_seen_at=:t", t=_ago(minutes=settings.session_idle_minutes + 5))
    assert c.get("/api/auth/me").status_code == 401                                # idle too long → gone
    c, _ = _login(app)
    _sql(app, "UPDATE sessions SET last_seen_at=:t", t=_ago(minutes=10))
    assert c.get("/api/auth/me").status_code == 200
    assert _sql(app, "SELECT last_seen_at FROM sessions")[0][0] > _ago(minutes=1)    # activity extends it …
    fresh = _sql(app, "SELECT last_seen_at FROM sessions")[0][0]
    c.get("/api/auth/me")
    assert _sql(app, "SELECT last_seen_at FROM sessions")[0][0] == fresh           # … but not on every poll (write amplification)


def test_absolute_expiry_still_applies_to_an_active_session(app):
    c, _ = _login(app)
    _sql(app, "UPDATE sessions SET expires_at=:t", t=_ago(seconds=1))
    assert c.get("/api/auth/me").status_code == 401


def test_new_login_replaces_the_session_presented_by_the_cookie(app):
    c, _ = _login(app)
    old = c.cookies.get("aicos_session")
    assert c.post("/api/auth/login", json={"email": OWNER, "password": DEMO_PASSWORD}).status_code == 200
    assert c.cookies.get("aicos_session") != old
    stale = TestClient(app); stale.cookies.set("aicos_session", old)
    assert stale.get("/api/auth/me").status_code == 401                              # no session fixation / piling up


def test_logout_all_revokes_every_session_of_that_user_only(app):
    a, _ = _login(app); b, _ = _login(app); other, _ = _login(app, "admin@climasur.test")
    assert a.post("/api/auth/logout-all").status_code == 200
    assert a.get("/api/auth/me").status_code == 401 and b.get("/api/auth/me").status_code == 401
    assert other.get("/api/auth/me").status_code == 200
    assert TestClient(app).post("/api/auth/logout-all").status_code == 401


def test_expired_sessions_are_purged(app):
    _login(app)
    _sql(app, "UPDATE sessions SET expires_at=:t", t=_ago(days=2))
    _login(app, "admin@climasur.test")
    assert _sql(app, "SELECT count(*) FROM sessions WHERE expires_at<:n", n=_ago(seconds=0))[0][0] == 0


def test_cookie_flags(app, monkeypatch):
    _, r = _login(app)
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "path=/" in sc and "secure" not in sc
    _, r = _login(app, base_url="https://testserver")                                  # TLS (uvicorn rewrites the scheme only for a trusted proxy)
    assert "secure" in r.headers["set-cookie"].lower()
    monkeypatch.setattr(settings, "cookie_secure", "1")
    assert "secure" in _login(app)[1].headers["set-cookie"].lower()


def test_pre_v06_session_without_csrf_gets_one_from_me(app):
    c, _ = _login(app)
    _sql(app, "UPDATE sessions SET csrf=NULL, last_seen_at=NULL")
    tok = c.get("/api/auth/me").json()["csrf_token"]
    assert tok and c.post("/api/assistant/chat", json={"message": "hola"}, headers={"x-csrf-token": tok}).status_code == 200


# ------------------------------------------------------------------ login protection
def _bad(app, email, c=None):
    return (c or TestClient(app)).post("/api/auth/login", json={"email": email, "password": "mala-clave-1"})


def test_account_locks_after_repeated_failures_even_with_the_right_password(app, tight):
    for _ in range(3):
        assert _bad(app, OWNER).status_code == 401
    r = _bad(app, OWNER)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    right = TestClient(app).post("/api/auth/login", json={"email": OWNER, "password": DEMO_PASSWORD})
    assert right.status_code == 429                                                    # a guess that happens to be right does not unlock it
    assert _sql(app, "SELECT count(*) FROM sessions")[0][0] == 0


def test_lock_response_does_not_reveal_whether_the_account_exists(app, tight):
    for email in (OWNER, "nadie@climasur.test"):
        for _ in range(3):
            _bad(app, email)
    known, unknown = _bad(app, OWNER), _bad(app, "nadie@climasur.test")
    assert known.status_code == unknown.status_code == 429 and known.json() == unknown.json()
    assert _bad(app, "otro-nadie@climasur.test").json()["detail"] == _bad(app, "otro-nadie@climasur.test").json()["detail"]


def test_lock_expires_with_the_rolling_window(app, tight):
    for _ in range(3):
        _bad(app, OWNER)
    assert _bad(app, OWNER).status_code == 429
    _sql(app, "UPDATE login_attempts SET at=:t", t=_ago(seconds=settings.login_window_seconds + 5))
    assert TestClient(app).post("/api/auth/login", json={"email": OWNER, "password": DEMO_PASSWORD}).status_code == 200


def test_success_clears_the_counter_for_that_pair(app, tight):
    for _ in range(2):
        _bad(app, OWNER)
    assert TestClient(app).post("/api/auth/login", json={"email": OWNER, "password": DEMO_PASSWORD}).status_code == 200
    for _ in range(2):
        assert _bad(app, OWNER).status_code == 401                                    # two fresh failures, not 2+2


def test_one_locked_account_does_not_lock_the_others(app, tight):
    for _ in range(3):
        _bad(app, OWNER)
    assert _bad(app, OWNER).status_code == 429
    assert TestClient(app).post("/api/auth/login", json={"email": "admin@climasur.test", "password": DEMO_PASSWORD}).status_code == 200


def test_ip_wide_failures_are_capped_across_many_emails(app, tight):
    for i in range(8):
        assert _bad(app, f"x{i}@nada.test").status_code == 401
    assert _bad(app, "x9@nada.test").status_code == 429
    assert TestClient(app).post("/api/auth/login", json={"email": "admin@climasur.test", "password": DEMO_PASSWORD}).status_code == 429


def test_distributed_guessing_against_one_email_is_capped(app, tight, monkeypatch):
    """Rotating source addresses defeats a per-address limit; the per-email counter still trips."""
    for i in range(6):
        _sql(app, "INSERT INTO login_attempts(at,email_key,ip,ok) VALUES(:t,:e,:ip,0)", t=_ago(seconds=30), e=OWNER, ip=f"203.0.113.{i}")
    assert _bad(app, OWNER).status_code == 429


def test_unknown_email_costs_the_same_hash_work_as_a_known_one(app, monkeypatch):
    calls = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password", lambda pw, stored: calls.append(stored) or real(pw, stored))
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "verify_password", security.verify_password)
    _bad(app, "nadie@climasur.test"); _bad(app, OWNER)
    assert len(calls) == 2 and calls[0] != calls[1]                                   # unknown → dummy hash is still verified


def test_login_events_are_audited(app, tight):
    _bad(app, OWNER)
    TestClient(app).post("/api/auth/login", json={"email": OWNER, "password": DEMO_PASSWORD})
    for _ in range(3):
        _bad(app, OWNER)
    rows = _sql(app, "SELECT action,outcome FROM audit_log WHERE action LIKE 'auth.%' ORDER BY id")
    assert ("auth.login", "denied") in rows and ("auth.login", "ok") in rows and ("auth.locked", "denied") in rows
    assert "mala-clave" not in str(_sql(app, "SELECT * FROM audit_log")) and "mala-clave" not in str(_sql(app, "SELECT * FROM login_attempts"))   # never persist a guessed password
    assert _sql(app, "SELECT count(*) FROM login_attempts WHERE email_key='nadie@climasur.test'")[0][0] == 0
    _bad(app, "nadie@climasur.test")
    assert _sql(app, "SELECT count(*) FROM login_attempts WHERE email_key='nadie@climasur.test'")[0][0] == 1  # unknown emails are throttled too, without a tenant to audit under


def test_deactivated_user_cannot_login_and_counts_as_a_failure(app, tight):
    _sql(app, "UPDATE users SET active=0 WHERE email=:e", e="tecnico@climasur.test")
    assert _bad(app, "tecnico@climasur.test").status_code == 401
    assert TestClient(app).post("/api/auth/login", json={"email": "tecnico@climasur.test", "password": DEMO_PASSWORD}).status_code == 401


# ------------------------------------------------------------------ password hashes
def test_new_hashes_are_argon2id_at_or_above_the_owasp_minimum():
    h = security.hash_password("una-clave-larga-123")
    assert h.startswith("$argon2id$") and security.verify_password("una-clave-larga-123", h) and not security.verify_password("otra", h)
    import re
    m, t, p = (int(x) for x in re.search(r"m=(\d+),t=(\d+),p=(\d+)", h).groups())
    assert m >= 19456 and t >= 2 and p >= 1


def test_legacy_scrypt_hash_still_logs_in_and_is_upgraded_in_place(app):
    import hashlib, secrets
    salt = secrets.token_bytes(16)
    legacy = f"scrypt${salt.hex()}${hashlib.scrypt(DEMO_PASSWORD.encode(), salt=salt, n=2**14, r=8, p=1).hex()}"
    _sql(app, "UPDATE users SET password_hash=:h WHERE email=:e", h=legacy, e=OWNER)
    assert security.verify_password(DEMO_PASSWORD, legacy) and security.needs_rehash(legacy)
    assert TestClient(app).post("/api/auth/login", json={"email": OWNER, "password": DEMO_PASSWORD}).status_code == 200
    now_hash = _sql(app, "SELECT password_hash FROM users WHERE email=:e", e=OWNER)[0][0]
    assert now_hash.startswith("$argon2id$") and not security.needs_rehash(now_hash)
    assert TestClient(app).post("/api/auth/login", json={"email": OWNER, "password": DEMO_PASSWORD}).status_code == 200


# ------------------------------------------------------------------ "local only" endpoints
@pytest.mark.parametrize("hdrs,ok", [
    ({}, True),
    ({"x-forwarded-for": "198.51.100.7"}, False),       # a reverse proxy on this same machine makes every visitor look like 127.0.0.1
    ({"forwarded": "for=198.51.100.7"}, False),
    ({"x-real-ip": "198.51.100.7"}, False),
    ({"host": "app.aire.example"}, False),              # DNS rebinding / public host name
    ({"host": "dueno.localhost:8743"}, True),
])
def test_loopback_only_endpoints_refuse_proxied_or_foreign_host_requests(app, monkeypatch, hdrs, ok):
    monkeypatch.setattr(settings, "allow_company_creation", True)
    body = {"name": "Taller Prueba", "slug": "taller-prueba", "industry": "taller", "preset": "generico", "owner_name": "Pía Soto", "owner_email": "pia@tallerprueba.test"}
    r = TestClient(app).post("/api/setup/companies", json=body, headers=hdrs)
    assert (r.status_code == 200) is ok, (hdrs, r.status_code, r.text)


# ------------------------------------------------------------------ migration from a V0.5 file
def test_v05_database_migrates_additively_and_old_sessions_survive(tmp_path):
    url = f"sqlite:///{tmp_path / 'v05.db'}"
    from app.api import create_app
    first = create_app(url)
    c1 = TestClient(first)
    assert c1.post("/api/auth/login", json={"email": OWNER, "password": DEMO_PASSWORD}).status_code == 200
    with first.state.eng.begin() as c:                                     # rewind the file to the V0.5 shape: no csrf/last_seen_at, no login_attempts
        c.execute(text("ALTER TABLE sessions DROP COLUMN csrf")); c.execute(text("ALTER TABLE sessions DROP COLUMN last_seen_at")); c.execute(text("DROP TABLE login_attempts"))
    first.state.eng.dispose()
    second = create_app(url)                                               # "restart on the new code"
    cols = {r[1] for r in _sql(second, "PRAGMA table_info(sessions)")}
    assert {"csrf", "last_seen_at"} <= cols and _sql(second, "SELECT count(*) FROM login_attempts")[0][0] == 0
    c2 = TestClient(second); c2.cookies.set("aicos_session", c1.cookies.get("aicos_session"))
    me = c2.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["csrf_token"]                # the V0.5 session keeps working and gets its token
    assert c2.post("/api/assistant/chat", json={"message": "hola"}, headers={"x-csrf-token": me.json()["csrf_token"]}).status_code == 200
