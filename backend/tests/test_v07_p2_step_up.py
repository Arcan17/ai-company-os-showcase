"""V0.7 P2 (Issue #23) — `auth_time` y autenticación reciente («step-up») para las operaciones sensibles. Escritas EN ROJO antes de implementar.
Reloj CONTROLADO (`app.clock.utcnow`): nada duerme ni depende de la hora real.
LO QUE NO DEMUESTRAN: que `auth_time` del simulado equivalga a MFA. El simulado informa lo que la prueba le dice; la garantía real de MFA (obligatorio en cada inicio de sesión del proveedor) es P3/P4 y sigue PENDIENTE."""
import pytest

from app import security
from app.config import settings
from app.db import q

from .conftest import GAS, chat, pending_approval
from app.seed import DEMO_PASSWORD

from .idp_util import FAIL_LOCATION, STAFF_PW, FrozenClock, issues_session, make_world
from .pilot_util import PW, snapshot

SENSITIVE_POSTS = {                                                  # toda ruta que exige `config.manage` o `approvals.decide`, con un cuerpo VÁLIDO (la comprobación va antes de mirar ningún dato)
    ("POST", "/api/config/employees"): ("/api/config/employees", {"name": "Zed Uno", "email": "zed@alfa.test", "role": "technician"}),
    ("POST", "/api/config/assignments"): ("/api/config/assignments", {"user_id": "usr_x", "template": "tech_assistant"}),
    ("POST", "/api/config/agents/{agent_id}/enabled"): ("/api/config/agents/agt_x/enabled", {"enabled": True}),
    ("POST", "/api/config/employees/{user_id}/active"): ("/api/config/employees/usr_x/active", {"active": False}),
    ("POST", "/api/config/employees/{user_id}/role"): ("/api/config/employees/usr_x/role", {"role": "admin"}),
    ("POST", "/api/config/invitations"): ("/api/config/invitations", {"email": "nuevo@alfa.test", "name": "Nuevo", "role": "technician"}),
    ("POST", "/api/config/invitations/{iid}/revoke"): ("/api/config/invitations/inv_x/revoke", None),
    ("POST", "/api/approvals/{aid}/decide"): ("/api/approvals/apr_x/decide", {"decision": "approved"}),
}


@pytest.fixture
def w(tmp_path, monkeypatch):
    clk = FrozenClock().install(monkeypatch)
    world = make_world(tmp_path, monkeypatch)
    world.clk = clk
    return world


def noop_write(owner):
    """Una escritura sensible INOFENSIVA (habilitar un asistente que ya está habilitado): 200 si la sesión es reciente, 401 `reauth_required` si no."""
    aid = owner.get("/api/config/overview").json()["shared_agents"][0]["agent_id"]
    return owner.post(f"/api/config/agents/{aid}/enabled", json={"enabled": True})


# ───────────────────────────── qué se guarda ─────────────────────────────

def test_a_password_sign_in_records_auth_time_now(w):
    http = w.browser()
    r = http.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    me = http.get("/api/auth/me").json()
    assert me["auth_time"] == w.clk.iso()


def test_me_reports_auth_time_and_the_step_up_state(w):
    owner = w.company("alfa")
    me = owner.get("/api/auth/me").json()
    assert me["auth_time"] == w.clk.iso() and me["step_up"] == {"enforced": True, "fresh": True, "max_age_seconds": 900}
    w.clk.advance(minutes=16)
    assert owner.get("/api/auth/me").json()["step_up"]["fresh"] is False


# ───────────────────────────── cuándo se exige ─────────────────────────────

def test_the_requirement_is_enforced_with_an_identity_provider_and_in_pilot_but_not_in_plain_local(tmp_path, monkeypatch):
    clk = FrozenClock().install(monkeypatch)
    results = {}
    for name, kw in (("local_plain", dict(env="local", provider=None)), ("local_with_provider", dict(env="local")), ("pilot_plain", dict(env="pilot", provider=None))):
        (tmp_path / name).mkdir()
        world = make_world(tmp_path / name, monkeypatch, **kw)
        owner = world.company("alfa")
        with world.eng.begin() as c:                                                            # la sesión del dueño se «envejece» 3 horas
            q(c, "UPDATE sessions SET auth_time=:t", t=clk.iso(hours=-3))
        results[name] = noop_write(owner).status_code
        world.app.state.orch.keeper.stop()
    assert results == {"local_plain": 200, "local_with_provider": 401, "pilot_plain": 401}, results


def test_the_boundary_is_fifteen_minutes_and_it_uses_the_controlled_clock(w):
    owner = w.company("alfa")
    w.clk.advance(minutes=14, seconds=59)
    assert noop_write(owner).status_code == 200
    w.clk.advance(seconds=1)                                                                    # exactamente 15:00 = todavía válido
    assert noop_write(owner).status_code == 200
    w.clk.advance(seconds=1)
    r = noop_write(owner)
    assert r.status_code == 401 and r.json()["detail"] == "reauth_required"


def test_the_window_is_configurable(w, monkeypatch):
    owner = w.company("alfa")
    monkeypatch.setattr(settings, "reauth_max_age_seconds", 60)
    w.clk.advance(seconds=61)
    assert noop_write(owner).status_code == 401


@pytest.mark.parametrize("auth_time,expected", [(None, 401), ("future_30s", 200), ("future_2min", 401), ("garbage", 401), ("", 401)])
def test_a_missing_future_or_unreadable_auth_time_is_never_fresh(w, auth_time, expected):
    owner = w.company("alfa")
    value = {"future_30s": w.clk.iso(seconds=30), "future_2min": w.clk.iso(minutes=2)}.get(auth_time, auth_time)
    with w.eng.begin() as c:
        q(c, "UPDATE sessions SET auth_time=:t", t=value)
    assert noop_write(owner).status_code == expected


def test_a_stale_request_changes_nothing_is_audited_and_never_reaches_the_handler(w):
    owner = w.company("alfa")
    w.clk.advance(minutes=20)
    before = snapshot(w.eng, owner.company_id)
    r = owner.post("/api/config/employees", json={"name": "Zed Uno", "email": "zed@alfa.test", "role": "technician"})
    assert r.status_code == 401 and r.json()["detail"] == "reauth_required"
    after = snapshot(w.eng, owner.company_id)
    quiet = lambda snap: {t: h for t, h in snap.items() if t not in ("audit_log", "sessions")}              # (la auditoría y el `last_seen_at` de la sesión SÍ cambian: es lo esperado)
    assert quiet(after) == quiet(before)
    assert w.count("users", "lower(email)='zed@alfa.test'") == 0
    assert w.count("audit_log", "company_id=:c AND outcome='reauth_required'", c=owner.company_id) == 1


def test_reads_are_not_gated(w):
    owner = w.company("alfa")
    w.clk.advance(minutes=100)                                                                  # muy pasada de los 15 min y aún dentro de la inactividad máxima (120 min)
    assert owner.get("/api/config/overview").status_code == 200 and owner.get("/api/approvals").status_code == 200 and owner.get("/api/dashboard").status_code == 200


def test_sliding_session_activity_never_refreshes_auth_time(w):
    """Un panel abierto (SSE, sondeos) renueva `last_seen_at`, NUNCA la autenticación."""
    owner = w.company("alfa")
    for _ in range(3):
        w.clk.advance(minutes=6)
        assert owner.get("/api/dashboard").status_code == 200
    assert noop_write(owner).status_code == 401


def test_approvals_decide_is_gated_and_the_approval_is_left_untouched(w):
    owner = w.session("dueno@climasur.test")
    chat(w.browser(), body=GAS)
    ap = pending_approval(owner.http)
    w.clk.advance(minutes=20)
    r = owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"})
    assert r.status_code == 401 and r.json()["detail"] == "reauth_required"
    assert w.rows("SELECT status FROM approvals WHERE id=:i", i=ap["id"])[0]["status"] == "pendiente"
    assert owner.post("/api/auth/reauth", json={"password": DEMO_PASSWORD}).status_code == 200
    assert owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 200


def test_every_sensitive_post_route_is_gated_and_none_was_forgotten(w):
    """Generada del OpenAPI vivo: toda ruta POST de /api/config/* y de decidir aprobaciones debe tener su caso aquí, y cada caso debe pedir reautenticación antes de hacer nada."""
    owner = w.company("alfa")
    spec = w.app.openapi()["paths"]
    discovered = {("POST", p) for p, ops in spec.items() if "post" in ops and (p.startswith("/api/config/") or p.endswith("/decide"))}
    assert discovered == set(SENSITIVE_POSTS), f"rutas sensibles sin caso (o casos de rutas que ya no existen): {sorted(discovered ^ set(SENSITIVE_POSTS))}"
    w.clk.advance(minutes=20)
    before = snapshot(w.eng, owner.company_id)
    for key, (url, body) in SENSITIVE_POSTS.items():
        r = owner.post(url, json=body) if body is not None else owner.post(url)
        assert (r.status_code, r.json().get("detail")) == (401, "reauth_required"), f"{key}: {r.status_code} {r.text}"
    quiet = lambda snap: {t: h for t, h in snap.items() if t not in ("audit_log", "sessions")}
    assert quiet(snapshot(w.eng, owner.company_id)) == quiet(before)


def test_a_role_without_the_permission_still_gets_403_not_401(w):
    """El orden importa: quien no tiene el permiso recibe 403 aunque su sesión esté vencida (no se le pide reautenticar algo que no podría hacer)."""
    owner = w.company("alfa")
    admin = w.staff(owner, "admin")
    w.clk.advance(minutes=20)
    assert admin.post("/api/config/invitations", json={"email": "x@alfa.test", "name": "Equis", "role": "technician"}).status_code == 403


# ───────────────────────────── reautenticar con contraseña ─────────────────────────────

def test_password_reauth_refreshes_auth_time_on_the_same_session(w):
    owner = w.company("alfa")
    token = owner.http.cookies.get(w.session_cookie)
    w.clk.advance(minutes=20)
    assert noop_write(owner).status_code == 401
    r = owner.post("/api/auth/reauth", json={"password": PW})
    assert r.status_code == 200 and r.json()["auth_time"] == w.clk.iso()
    assert noop_write(owner).status_code == 200
    assert owner.http.cookies.get(w.session_cookie) == token
    assert w.count("audit_log", "company_id=:c AND action='auth.reauth' AND outcome='ok'", c=owner.company_id) == 1


def test_password_reauth_checks_the_password_of_the_session_user_and_is_throttled(w):
    owner = w.company("alfa")
    other = w.company("beta")
    w.clk.advance(minutes=20)
    assert owner.post("/api/auth/reauth", json={"password": "incorrecta-123"}).status_code == 401
    assert owner.post("/api/auth/reauth", json={"password": PW + "x"}).status_code == 401
    assert noop_write(owner).status_code == 401
    codes = [owner.post("/api/auth/reauth", json={"password": f"mal-{i}-clave"}).status_code for i in range(6)]
    assert codes[-1] == 429, "no hay límite de intentos de reautenticación"
    locked = owner.post("/api/auth/reauth", json={"password": PW})
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) >= 1, "una cuenta bloqueada no puede reautenticar ni con la clave correcta"
    assert noop_write(owner).status_code == 401
    assert other.post("/api/auth/reauth", json={"password": PW}).status_code == 200            # la otra empresa no siente el bloqueo de Alfa (otro correo)


def test_reauth_needs_a_session_and_the_csrf_token(w):
    owner = w.company("alfa")
    assert w.browser().post("/api/auth/reauth", json={"password": PW}).status_code in (401, 403)
    assert owner.http.post("/api/auth/reauth", json={"password": PW}, headers={"X-CSRF-Token": "mal"}).status_code == 403


def test_a_person_created_by_an_invitation_cannot_reauthenticate_with_a_password_at_all(w):
    owner = w.company("alfa")
    inv = w.invite(owner, "ana@alfa.test", role="admin")
    http, resp, _ = w.accept(inv["invite_token"], "ana@alfa.test")
    csrf = http.get("/api/auth/me").json()["csrf_token"]
    for pw in (PW, STAFF_PW, "", "!"):
        if pw:
            assert http.post("/api/auth/reauth", json={"password": pw}, headers={"X-CSRF-Token": csrf}).status_code == 401


def test_a_staff_member_cannot_reauthenticate_with_the_owners_password(w):
    owner = w.company("alfa")
    admin = w.staff(owner, "admin")
    w.clk.advance(minutes=20)
    assert admin.post("/api/auth/reauth", json={"password": PW}).status_code == 401           # la clave del DUEÑO no sirve a otra persona
    assert admin.get("/api/auth/me").json()["step_up"]["fresh"] is False
    assert admin.post("/api/auth/reauth", json={"password": STAFF_PW}).status_code == 200      # (control) la suya sí


# ───────────────────────────── reautenticar con el proveedor ─────────────────────────────

def link_owner(w, owner, subject):
    """(Solo pruebas) vincula una identidad al dueño: la creación del dueño con proveedor es P3 (la CLI aún no la hace)."""
    with w.eng.begin() as c:
        uid = c.exec_driver_sql("SELECT id FROM users WHERE lower(email)=?", (f"dueno@{owner.slug}.test",)).scalar()
        q(c, "INSERT INTO identities(provider,subject,user_id,company_id,created_at) VALUES('fake',:s,:u,:c,:t)", s=subject, u=uid, c=owner.company_id, t=w.clk.iso())


def test_provider_reauth_rotates_the_session_and_refreshes_auth_time(w):
    owner = w.company("alfa")
    subject = w.prov.account("dueno@alfa.test")
    link_owner(w, owner, subject)
    old = owner.http.cookies.get(w.session_cookie)
    w.clk.advance(minutes=20)
    assert noop_write(owner).status_code == 401
    resp, url = w.go(owner, subject, reauth=True)
    assert resp.headers["location"] == "/#/dashboard" and issues_session(resp, w.session_cookie)
    assert "prompt=login" in url, "el proveedor debe recibir la orden de pedir las credenciales otra vez"
    new = owner.http.cookies.get(w.session_cookie)
    assert new != old
    owner.csrf = owner.get("/api/auth/me").json()["csrf_token"]
    assert noop_write(owner).status_code == 200
    probe = w.browser()
    probe.cookies.set(w.session_cookie, old)
    assert probe.get("/api/auth/me").status_code == 401, "la sesión vieja sobrevivió a la reautenticación"


def test_provider_reauth_requires_a_session(w):
    w.company("alfa")
    r = w.start(w.browser(), {"reauth": True})
    assert r.status_code == 401


def test_provider_reauth_for_a_different_identity_is_refused_and_leaves_the_session_alone(w):
    owner = w.company("alfa")
    link_owner(w, owner, w.prov.account("dueno@alfa.test"))
    w.clk.advance(minutes=20)
    impostor = w.prov.account("impostor@alfa.test")
    token = owner.http.cookies.get(w.session_cookie)
    resp, _ = w.go(owner, impostor, reauth=True)
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie)
    assert owner.http.cookies.get(w.session_cookie) == token and noop_write(owner).status_code == 401


def test_a_provider_that_reuses_its_old_sso_session_does_not_count_as_a_fresh_authentication(w):
    owner = w.company("alfa")
    subject = w.prov.account("dueno@alfa.test")
    link_owner(w, owner, subject)
    w.prov.login(w.start(w.browser(), {}).json()["authorize_url"], subject, auth_age_seconds=0)   # el proveedor registra la última autenticación REAL: ahora
    w.clk.advance(minutes=30)
    resp, _ = w.go(owner, subject, reauth=True, ignore_force=True)                                 # ignora `prompt=login` y devuelve la hora de hace 30 min
    assert resp.headers["location"] == FAIL_LOCATION
    assert noop_write(owner).status_code == 401


def test_a_reauth_flow_cannot_resurrect_a_session_that_was_closed_meanwhile(w):
    owner = w.company("alfa")
    subject = w.prov.account("dueno@alfa.test")
    link_owner(w, owner, subject)
    http = owner.http
    url = w.start(http, {"reauth": True}, headers={"X-CSRF-Token": owner.csrf}).json()["authorize_url"]
    back = w.prov.login(url, subject)
    assert owner.post("/api/auth/logout").status_code == 200
    resp = http.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == FAIL_LOCATION and http.get("/api/auth/me").status_code == 401


def test_an_old_provider_authentication_signs_in_but_is_not_fresh(w):
    owner = w.company("alfa")
    subject = w.prov.account("dueno@alfa.test")
    link_owner(w, owner, subject)
    http = w.browser()
    resp, _ = w.go(http, subject, auth_age=3600)                                                   # SSO de hace una hora: iniciar sesión SÍ, operar lo sensible NO
    assert resp.headers["location"] == "/#/dashboard"
    me = http.get("/api/auth/me").json()
    assert me["step_up"]["fresh"] is False
    aid = http.get("/api/config/overview").json()["shared_agents"][0]["agent_id"]
    r = http.post(f"/api/config/agents/{aid}/enabled", json={"enabled": True}, headers={"X-CSRF-Token": me["csrf_token"]})
    assert (r.status_code, r.json()["detail"]) == (401, "reauth_required")


def test_a_provider_that_claims_an_auth_time_in_the_future_cannot_sign_in(w):
    owner = w.company("alfa")
    subject = w.prov.account("dueno@alfa.test")
    link_owner(w, owner, subject)
    resp, _ = w.go(w.browser(), subject, auth_age=-3600)
    assert resp.headers["location"] == FAIL_LOCATION


def test_a_provider_that_reports_no_auth_time_signs_in_but_every_sensitive_operation_asks_to_reauthenticate(w):
    owner = w.company("alfa")
    subject = w.prov.account("dueno@alfa.test")
    link_owner(w, owner, subject)
    http = w.browser()
    resp, _ = w.go(http, subject, auth_age=None)
    assert resp.headers["location"] == "/#/dashboard"
    me = http.get("/api/auth/me").json()
    assert me["auth_time"] is None and me["step_up"]["fresh"] is False
