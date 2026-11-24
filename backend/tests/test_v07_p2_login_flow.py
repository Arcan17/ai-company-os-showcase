"""V0.7 P2 (Issue #23) — flujo de acceso con el proveedor de identidad SIMULADO: inicio (state + PKCE + nonce + cookie de correlación), callback, vínculo identidad↔empresa↔empleado,
inicio posterior por `subject` (nunca por correo), y fallo CERRADO con una única respuesta genérica. Escritas EN ROJO antes de implementar.
Todo en bases temporales y con el proveedor simulado inyectado: no hay red, ni cuentas, ni correos.
LO QUE NO DEMUESTRAN: que WorkOS (u otro proveedor real) verifique el correo, exija MFA o reporte `auth_time` como lo hace el simulado — eso se comprueba en P3/P4."""
import base64
import hashlib
from datetime import datetime

import pytest

from app.db import q

from .idp_util import FAIL_LOCATION, GENERIC_START_FAIL, OK_LOCATION, FrozenClock, issues_session, make_world, query, set_cookies
from .pilot_util import PW


@pytest.fixture
def w(tmp_path, monkeypatch):
    clk = FrozenClock().install(monkeypatch)
    world = make_world(tmp_path, monkeypatch)
    world.clk = clk
    return world


@pytest.fixture
def two(w):
    return w.company("alfa", "Alfa Servicios"), w.company("beta", "Beta Servicios")


def joined(w, owner, email="ana@alfa.test", *, role="technician", name="Ana Técnica"):
    """El dueño invita y la persona acepta con el proveedor simulado. → (navegador de la persona, subject, token)."""
    inv = w.invite(owner, email, name=name, role=role)
    http, resp, subject = w.accept(inv["invite_token"], email, name=name)
    assert resp.status_code == 303 and resp.headers["location"] == OK_LOCATION, resp.text
    return http, subject, inv["invite_token"]


def fingerprint(resp) -> tuple:
    """Huella de lo que ve el navegador. `expires=` de la cookie borrada es la hora REAL (Starlette): se normaliza, o dos respuestas idénticas separadas por un cambio de segundo parecerían distintas."""
    import re
    return (resp.status_code, resp.headers.get("location"), resp.text, tuple(sorted(re.sub(r"expires=[^;]*;", "expires=*;", c) for c in set_cookies(resp))))


# ───────────────────────────── el camino feliz ─────────────────────────────

def test_the_invited_person_completes_the_whole_flow_and_gets_the_role_and_company_of_the_invitation(w, two):
    A, B = two
    inv = w.invite(A, "ana@alfa.test", name="Ana Técnica", role="technician")
    http, resp, subject = w.accept(inv["invite_token"], "ana@alfa.test", name="Ana Técnica")
    assert (resp.status_code, resp.headers["location"]) == (303, OK_LOCATION)
    assert issues_session(resp, w.session_cookie) and not any(c.startswith(w.flow_cookie + "=") and "Max-Age=0" not in c for c in set_cookies(resp))   # la cookie de correlación se borra
    assert resp.headers["cache-control"] == "no-store"
    me = http.get("/api/auth/me").json()
    assert (me["email"], me["name"], me["role"], me["company_id"], me["is_demo"]) == ("ana@alfa.test", "Ana Técnica", "technician", A.company_id, 0)
    assert abs((datetime.fromisoformat(me["auth_time"]) - w.clk.t).total_seconds()) < 1.5, "la sesión debe guardar la hora de autenticación que informó el proveedor"
    user = w.rows("SELECT * FROM users WHERE lower(email)='ana@alfa.test'")[0]
    assert (user["company_id"], user["role"], user["active"], user["is_demo"]) == (A.company_id, "technician", 1, 0)
    ident = w.rows("SELECT * FROM identities")[0]
    assert (ident["provider"], ident["subject"], ident["user_id"], ident["company_id"]) == ("fake", subject, user["id"], A.company_id)
    row = w.rows("SELECT * FROM invitations WHERE id=:i", i=inv["id"])[0]
    assert row["status"] == "accepted" and row["accepted_at"] == w.clk.iso() and row["accepted_user_id"] == user["id"]
    assert w.count("agents", "user_id=:u AND role='tech_assistant' AND enabled=1", u=user["id"]) == 1 and w.count("availability", "technician_id=:u", u=user["id"]) > 0
    assert [r["outcome"] for r in w.rows("SELECT outcome FROM audit_log WHERE company_id=:c AND action='auth.invite_accept'", c=A.company_id)] == ["ok"]
    assert w.count("users", "company_id=:c", c=B.company_id) == 1, "la empresa B ganó una persona"


def test_the_account_created_by_an_invitation_has_no_usable_password(w, two):
    A, _ = two
    joined(w, A)
    stored = w.rows("SELECT password_hash FROM users WHERE lower(email)='ana@alfa.test'")[0]["password_hash"]
    assert not stored.startswith("$argon2") and "$" not in stored, "el hash de una cuenta de proveedor no debe ser verificable con ninguna contraseña"
    for pw in (PW, "", stored, "!", "password"):
        assert w.browser().post("/api/auth/login", json={"email": "ana@alfa.test", "password": pw}).status_code == 401


# ───────────────────────────── un solo uso ─────────────────────────────

def test_a_callback_cannot_be_replayed_and_an_accepted_token_cannot_start_again(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    http = w.browser()
    r = w.start(http, {"invite_token": inv["invite_token"]})
    back = w.prov.login(r.json()["authorize_url"], w.prov.account("ana@alfa.test"))
    flow_cookie = http.cookies.get(w.flow_cookie)
    first = http.get("/api/auth/callback", params=back, follow_redirects=False)
    assert first.headers["location"] == OK_LOCATION
    replay = w.browser()
    replay.cookies.set(w.flow_cookie, flow_cookie)                          # el atacante que ve el enlace Y la cookie tampoco puede repetirlo
    again = replay.get("/api/auth/callback", params=back, follow_redirects=False)
    assert again.headers["location"] == FAIL_LOCATION and not issues_session(again, w.session_cookie)
    bogus = w.start(w.browser(), {"invite_token": "no-existe-" * 5})
    used = w.start(w.browser(), {"invite_token": inv["invite_token"]})
    assert used.status_code == bogus.status_code == 400 and used.json() == bogus.json() == GENERIC_START_FAIL, "un token usado no debe distinguirse de uno inventado"
    assert w.count("users", "lower(email)='ana@alfa.test'") == 1


def test_two_flows_started_with_the_same_invitation_produce_exactly_one_account(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    s1, s2 = w.prov.account("ana@alfa.test"), w.prov.account("ana@alfa.test")
    h1, h2 = w.browser(), w.browser()
    u1 = w.start(h1, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    u2 = w.start(h2, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    r1 = h1.get("/api/auth/callback", params=w.prov.login(u1, s1), follow_redirects=False)
    r2 = h2.get("/api/auth/callback", params=w.prov.login(u2, s2), follow_redirects=False)
    assert [r1.headers["location"], r2.headers["location"]] == [OK_LOCATION, FAIL_LOCATION]
    assert w.count("users", "lower(email)='ana@alfa.test'") == 1 and w.count("identities") == 1
    assert w.count("invitations", "status='accepted'") == 1


# ───────────────────────────── inicio posterior: por subject, nunca por correo ─────────────────────────────

def test_later_sign_ins_use_the_subject_not_the_email(w, two):
    A, _ = two
    http, subject, _ = joined(w, A)
    uid = w.rows("SELECT id FROM users WHERE lower(email)='ana@alfa.test'")[0]["id"]
    again = w.browser()
    resp, _ = w.go(again, subject)
    assert resp.headers["location"] == OK_LOCATION and again.get("/api/auth/me").json()["id"] == uid
    w.prov.change_email(subject, "ana.nueva@otro-dominio.test")                                 # el correo cambia en el proveedor: la cuenta NO cambia
    third = w.browser()
    resp, _ = w.go(third, subject)
    me = third.get("/api/auth/me").json()
    assert resp.headers["location"] == OK_LOCATION and me["id"] == uid and me["email"] == "ana@alfa.test" and me["company_id"] == A.company_id
    assert w.count("users", "lower(email)='ana@alfa.test'") == 1 and w.count("identities") == 1


def test_an_identity_with_no_invitation_and_no_link_is_refused_and_creates_nothing(w, two):
    A, _ = two
    users, idents = w.count("users"), w.count("identities")
    stranger = w.prov.account("cualquiera@alfa.test")
    resp, _ = w.go(w.browser(), stranger)
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie)
    assert (w.count("users"), w.count("identities")) == (users, idents)


def test_an_identity_is_never_linked_to_an_existing_account_by_matching_email(w, two):
    """El dueño de Alfa tiene contraseña; alguien con un correo IGUAL al suyo en el proveedor no hereda su cuenta."""
    A, _ = two
    impostor = w.prov.account("dueno@alfa.test")
    resp, _ = w.go(w.browser(), impostor)
    assert resp.headers["location"] == FAIL_LOCATION and w.count("identities") == 0


def test_a_deactivated_person_cannot_sign_in_even_if_the_provider_still_authenticates_them(w, two):
    A, _ = two
    http, subject, _ = joined(w, A)
    uid = w.rows("SELECT id FROM users WHERE lower(email)='ana@alfa.test'")[0]["id"]
    assert A.post(f"/api/config/employees/{uid}/active", json={"active": False}).status_code == 200
    assert http.get("/api/auth/me").status_code == 401, "desactivar no mató la sesión"
    assert w.prov.disabled == {subject}, "no se pidió al proveedor desactivar la identidad"
    w.prov.enforce_disable = False                                                               # el proveedor «se equivoca» y la sigue autenticando: NUESTRA base decide
    resp, _ = w.go(w.browser(), subject)
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie)
    assert w.count("audit_log", "company_id=:c AND action='auth.idp_disable' AND outcome='ok'", c=A.company_id) == 1


def test_if_the_provider_cannot_disable_the_identity_the_local_deactivation_still_holds_and_the_failure_is_audited(w, two, monkeypatch):
    A, _ = two
    http, subject, _ = joined(w, A)
    uid = w.rows("SELECT id FROM users WHERE lower(email)='ana@alfa.test'")[0]["id"]
    monkeypatch.setattr(w.prov, "disable", lambda s: (_ for _ in ()).throw(RuntimeError("caído")))
    assert A.post(f"/api/config/employees/{uid}/active", json={"active": False}).status_code == 200
    assert http.get("/api/auth/me").status_code == 401 and w.rows("SELECT active FROM users WHERE id=:i", i=uid)[0]["active"] == 0
    assert [r["outcome"] for r in w.rows("SELECT outcome FROM audit_log WHERE company_id=:c AND action='auth.idp_disable'", c=A.company_id)] == ["failed"]


def test_the_provider_outage_is_logged_by_class_name_only_never_with_codes_states_or_emails(w, two, caplog):
    import logging
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    http = w.browser()
    url = w.start(http, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    back = w.prov.login(url, w.prov.account("ana@alfa.test"))
    w.prov.down = True
    with caplog.at_level(logging.WARNING, logger="aicos"):
        resp = http.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == FAIL_LOCATION
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "ProviderUnavailable" in text and back["code"] not in text and back["state"] not in text and "ana@alfa.test" not in text


def test_a_role_change_by_the_owner_is_what_the_next_sign_in_gets(w, two):
    A, _ = two
    _, subject, _ = joined(w, A)
    uid = w.rows("SELECT id FROM users WHERE lower(email)='ana@alfa.test'")[0]["id"]
    assert A.post(f"/api/config/employees/{uid}/role", json={"role": "admin"}).status_code == 200
    again = w.browser()
    w.go(again, subject)
    assert again.get("/api/auth/me").json()["role"] == "admin"


# ───────────────────────────── correo verificado y coincidencia exacta ─────────────────────────────

def test_an_unverified_email_never_creates_a_session_nor_consumes_the_invitation(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    _, resp, _ = w.accept(inv["invite_token"], "ana@alfa.test", verified=False)
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie)
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=inv["id"])[0]["status"] == "pending" and w.count("identities") == 0
    _, ok, _ = w.accept(inv["invite_token"], "ana@alfa.test", verified=True)                    # la persona verifica su correo y vuelve a intentarlo
    assert ok.headers["location"] == OK_LOCATION


@pytest.mark.parametrize("claimed", ["true", 1, "yes", None, "True"])
def test_only_a_real_boolean_true_counts_as_a_verified_email(w, two, claimed):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    subject = w.prov.account("ana@alfa.test", email_verified=claimed)
    resp, _ = w.go(w.browser(), subject, token=inv["invite_token"])
    assert resp.headers["location"] == FAIL_LOCATION and w.count("identities") == 0


@pytest.mark.parametrize("email", ["ana+otra@alfa.test", "ana@alfa.test.evil.test", "an@alfa.test", "ana@alfa.test ", "аna@alfa.test"])   # la última lleva una «а» cirílica
def test_the_provider_email_must_be_exactly_the_invited_one(w, two, email):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    subject = w.prov.account(email)
    resp, _ = w.go(w.browser(), subject, token=inv["invite_token"])
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie)
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=inv["id"])[0]["status"] == "pending" and w.count("identities") == 0


def test_email_case_does_not_matter(w, two):
    A, _ = two
    inv = w.invite(A, "Ana.Tecnica@Alfa.TEST")
    _, resp, _ = w.accept(inv["invite_token"], "ANA.TECNICA@alfa.test")
    assert resp.headers["location"] == OK_LOCATION


def test_an_invitation_for_an_email_that_belongs_to_another_company_can_never_be_accepted(w, two):
    A, B = two
    w.staff(B, "technician")
    inv = w.invite(A, "technician1@beta.test")                                                   # (la respuesta no lo reveló: ver test de invitaciones)
    _, resp, _ = w.accept(inv["invite_token"], "technician1@beta.test")
    assert resp.headers["location"] == FAIL_LOCATION
    assert w.count("users", "lower(email)='technician1@beta.test'") == 1 and w.count("identities") == 0


def test_an_identity_already_linked_to_one_user_cannot_be_linked_to_another(w, two):
    A, B = two
    _, subject, _ = joined(w, A)                                                                 # subject S ↔ Ana (Alfa)
    w.prov.change_email(subject, "ana@beta.test")                                                # …y ahora S dice tener otro correo, que Beta SÍ invitó
    inv_b = w.invite(B, "ana@beta.test")
    resp, _ = w.go(w.browser(), subject, token=inv_b["invite_token"])
    assert resp.headers["location"] == FAIL_LOCATION
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=inv_b["id"])[0]["status"] == "pending"
    assert w.count("users", "company_id=:c", c=B.company_id) == 1 and w.count("identities") == 1
    assert w.count("audit_log", "company_id=:c AND action='auth.invite_accept' AND outcome='denied' AND resource=:r", c=B.company_id, r=inv_b["id"]) == 1, \
        "la negativa debe ser la DISEÑADA (auditada en la empresa que invitó), no un error interno rescatado por la clave primaria"


# ───────────────────────────── invitación vencida o revocada ─────────────────────────────

@pytest.mark.parametrize("how", ["revoked", "expired"])
def test_a_revoked_or_expired_invitation_cannot_start_and_answers_like_a_made_up_token(w, two, how):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    if how == "revoked":
        assert A.post(f"/api/config/invitations/{inv['id']}/revoke").status_code == 200
    else:
        w.clk.advance(days=7, seconds=1)
    r = w.start(w.browser(), {"invite_token": inv["invite_token"]})
    assert (r.status_code, r.json()) == (400, GENERIC_START_FAIL)
    assert w.count("auth_flows") == 0, "un token inútil no debe dejar un flujo vivo"


@pytest.mark.parametrize("how", ["revoked", "expired"])
def test_an_invitation_revoked_or_expired_while_the_person_is_at_the_provider_fails_at_the_callback(w, two, how):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    http = w.browser()
    url = w.start(http, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    back = w.prov.login(url, w.prov.account("ana@alfa.test"))
    if how == "revoked":
        A.post(f"/api/config/invitations/{inv['id']}/revoke")
    else:
        w.clk.advance(days=7, seconds=1)
    resp = http.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == FAIL_LOCATION and w.count("users", "lower(email)='ana@alfa.test'") == 0 and w.count("identities") == 0


def test_the_last_second_of_the_seventh_day_still_works(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    w.clk.advance(days=7, milliseconds=-1)
    assert w.start(w.browser(), {"invite_token": inv["invite_token"]}).status_code == 200


# ───────────────────────────── atributos forjados ─────────────────────────────

@pytest.mark.parametrize("extra", [{"role": "owner"}, {"company_id": "co_x"}, {"email": "dueno@alfa.test"}, {"user_id": "usr_x"}, {"subject": "s"}, {"auth_time": 1}])
def test_the_start_request_accepts_no_identity_attributes_from_the_browser(w, two, extra):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    r = w.start(w.browser(), {"invite_token": inv["invite_token"], **extra})
    assert r.status_code == 422 and w.count("auth_flows") == 0


def test_forged_callback_parameters_change_nothing(w, two):
    A, B = two
    inv = w.invite(A, "ana@alfa.test", role="technician")
    http = w.browser()
    url = w.start(http, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    back = w.prov.login(url, w.prov.account("ana@alfa.test"))
    forged = {**back, "role": "owner", "company_id": B.company_id, "email": "dueno@beta.test", "user_id": "usr_x", "next": "https://evil.test/", "auth_time": "9999999999"}
    resp = http.get("/api/auth/callback", params=forged, follow_redirects=False)
    assert resp.headers["location"] == OK_LOCATION
    me = http.get("/api/auth/me").json()
    assert (me["role"], me["company_id"], me["email"]) == ("technician", A.company_id, "ana@alfa.test")


def test_the_callback_only_answers_get(w, two):
    assert w.browser().post("/api/auth/callback", params={"code": "x", "state": "y"}).status_code in (403, 405)


# ───────────────────────────── state, PKCE, nonce, cookie de correlación ─────────────────────────────

def test_the_authorize_url_carries_state_nonce_and_an_s256_pkce_challenge(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    http = w.browser()
    r = w.start(http, {"invite_token": inv["invite_token"]})
    qs = query(r.json()["authorize_url"])
    assert r.json()["authorize_url"].startswith("https://") and ".invalid/" in r.json()["authorize_url"], "el simulado jamás apunta a un host real"
    assert len(qs["state"]) >= 32 and len(qs["nonce"]) >= 22 and qs["code_challenge_method"] == "S256" and len(qs["code_challenge"]) == 43
    assert qs["login_hint"] == "ana@alfa.test"
    plain = query(w.start(w.browser(), {}).json()["authorize_url"])
    assert "login_hint" not in plain and plain["state"] != qs["state"] and plain["code_challenge"] != qs["code_challenge"]


def test_the_correlation_cookie_is_httponly_lax_path_root_ten_minutes_and_not_the_state(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    r = w.start(w.browser(), {"invite_token": inv["invite_token"]})
    cookie = next(c for c in set_cookies(r) if c.startswith(w.flow_cookie + "="))
    attrs = {p.split("=")[0].strip().lower(): (p.split("=", 1)[1] if "=" in p else True) for p in cookie.split(";")[1:]}
    assert attrs["httponly"] is True and str(attrs["samesite"]).lower() == "lax" and attrs["path"] == "/" and int(attrs["max-age"]) <= 600 and "domain" not in attrs
    value = cookie.split(";")[0].split("=", 1)[1]
    assert value and value != query(r.json()["authorize_url"])["state"]


def test_neither_the_state_nor_the_flow_cookie_nor_the_pkce_challenge_is_stored_in_clear(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    http = w.browser()
    r = w.start(http, {"invite_token": inv["invite_token"]})
    state = query(r.json()["authorize_url"])["state"]
    cookie = http.cookies.get(w.flow_cookie)
    row = w.rows("SELECT * FROM auth_flows")[0]
    blob = " ".join(str(v) for v in row.values())
    assert state not in blob and cookie not in blob and inv["invite_token"] not in blob
    assert row["state_hash"] == hashlib.sha256(state.encode()).hexdigest() and row["cookie_hash"] == hashlib.sha256(cookie.encode()).hexdigest()


def test_a_callback_from_a_browser_that_did_not_start_the_flow_is_refused_login_csrf(w, two):
    """El atacante inicia un flujo, se identifica y le entrega al navegador de la VÍCTIMA su enlace de callback: sin la cookie de correlación de ESE flujo no hay sesión."""
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    attacker, victim = w.browser(), w.browser()
    url = w.start(attacker, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    back = w.prov.login(url, w.prov.account("ana@alfa.test"))
    resp = victim.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie)
    other = w.browser()
    w.start(other, {})                                                                           # el navegador de la víctima tiene SU propio flujo, con otra cookie
    resp = other.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == FAIL_LOCATION
    assert attacker.get("/api/auth/callback", params=back, follow_redirects=False).headers["location"] == OK_LOCATION, "(control) el dueño legítimo del flujo sí entra"


def test_an_expired_flow_is_refused(w, two, monkeypatch):
    from app import idp_fake
    monkeypatch.setattr(idp_fake, "CODE_TTL_SECONDS", 3600)                                      # el CÓDIGO del proveedor sigue vivo: lo que caduca aquí es NUESTRO flujo (10 min)
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    http = w.browser()
    url = w.start(http, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    back = w.prov.login(url, w.prov.account("ana@alfa.test"))
    w.clk.advance(minutes=10, seconds=1)
    assert http.get("/api/auth/callback", params=back, follow_redirects=False).headers["location"] == FAIL_LOCATION
    assert w.count("users", "lower(email)='ana@alfa.test'") == 0


def _flow(w, A, mutate=None):
    """Prepara un flujo de invitación hasta justo antes del callback; `mutate(estado)` puede dañar algo en medio. → (navegador, params del callback)."""
    inv = w.invite(A, "ana@alfa.test")
    http = w.browser()
    url = w.start(http, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    back = w.prov.login(url, w.prov.account("ana@alfa.test"))
    if mutate:
        mutate(back)
    return http, back, inv


@pytest.mark.parametrize("what", ["unknown_state", "garbage_code", "missing_code", "missing_state", "empty", "wrong_verifier", "wrong_nonce"])
def test_every_malformed_or_tampered_callback_fails_closed(w, two, what):
    A, _ = two
    def mutate(back):
        if what == "unknown_state": back["state"] = "s" * 43
        if what == "garbage_code": back["code"] = "c" * 30
        if what == "missing_code": back.pop("code")
        if what == "missing_state": back.pop("state")
        if what == "empty": back.clear()
        with w.eng.begin() as c:                                                                 # PKCE / nonce: el servidor guardó otra cosa distinta de lo que el proveedor firmó
            if what == "wrong_verifier": q(c, "UPDATE auth_flows SET verifier='v'||verifier")
            if what == "wrong_nonce": q(c, "UPDATE auth_flows SET nonce='n'||nonce")
    http, back, inv = _flow(w, A, mutate)
    resp = http.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie)
    assert w.count("users", "lower(email)='ana@alfa.test'") == 0 and w.rows("SELECT status FROM invitations WHERE id=:i", i=inv["id"])[0]["status"] == "pending"


def test_a_code_issued_for_another_flow_is_refused_pkce_binds_code_to_flow(w, two):
    A, _ = two
    inv1, inv2 = w.invite(A, "ana@alfa.test"), w.invite(A, "beto@alfa.test")
    h1, h2 = w.browser(), w.browser()
    u1 = w.start(h1, {"invite_token": inv1["invite_token"]}).json()["authorize_url"]
    u2 = w.start(h2, {"invite_token": inv2["invite_token"]}).json()["authorize_url"]
    b1, b2 = w.prov.login(u1, w.prov.account("ana@alfa.test")), w.prov.login(u2, w.prov.account("beto@alfa.test"))
    swapped = h1.get("/api/auth/callback", params={"state": b1["state"], "code": b2["code"]}, follow_redirects=False)
    assert swapped.headers["location"] == FAIL_LOCATION and w.count("identities") == 0


def test_a_provider_that_is_down_never_creates_a_session_and_the_flow_is_burned(w, two):
    A, _ = two
    http, back, inv = _flow(w, A)
    flow_cookie = http.cookies.get(w.flow_cookie)                                                # el atacante que CONSERVA la cookie (el servidor la borra del navegador legítimo, pero no de quien la copió)
    w.prov.down = True
    resp = http.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie) and w.count("identities") == 0
    w.prov.down = False
    retry = w.browser()
    retry.cookies.set(w.flow_cookie, flow_cookie)
    assert retry.get("/api/auth/callback", params=back, follow_redirects=False).headers["location"] == FAIL_LOCATION, "un flujo que falló no se puede reintentar con el mismo state"
    assert w.rows("SELECT used_at FROM auth_flows")[0]["used_at"] is not None, "el flujo quedó vivo en la base"
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=inv["id"])[0]["status"] == "pending"
    _, ok, _ = w.accept(inv["invite_token"], "ana@alfa.test")                                    # el proveedor vuelve y la persona empieza de nuevo
    assert ok.headers["location"] == OK_LOCATION


def test_every_failure_looks_exactly_the_same_to_the_browser(w, two):
    """Sin pistas: ni el motivo, ni si el correo existe, ni si la invitación existió. Una sola huella (código, Location, cuerpo, cookies)."""
    A, B = two
    seen = set()
    for kind in ("unknown_state", "unverified", "mismatch", "stranger", "provider_down", "revoked"):
        inv = w.invite(A, f"{kind}@alfa.test".replace("_", "-"))
        http = w.browser()
        if kind == "stranger":
            url = w.start(http, {}).json()["authorize_url"]
            back = w.prov.login(url, w.prov.account("nadie@alfa.test"))
        else:
            url = w.start(http, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
            email = {"mismatch": "otra@alfa.test"}.get(kind, f"{kind}@alfa.test".replace("_", "-"))
            back = w.prov.login(url, w.prov.account(email, email_verified=kind != "unverified"))
        if kind == "unknown_state": back["state"] = "z" * 43
        if kind == "provider_down": w.prov.down = True
        if kind == "revoked": A.post(f"/api/config/invitations/{inv['id']}/revoke")
        seen.add(fingerprint(http.get("/api/auth/callback", params=back, follow_redirects=False)))
        w.prov.down = False
    assert len(seen) == 1, f"el callback distingue motivos de fallo: {seen}"


def test_repeated_bad_invitation_tokens_from_one_address_are_throttled_but_a_valid_one_is_not_counted(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    for _ in range(3):
        assert w.start(w.browser(), {"invite_token": inv["invite_token"]}).status_code == 200      # los intentos VÁLIDOS no gastan el cupo de fallos
    codes = [w.start(w.browser(), {"invite_token": f"mal-{i}" * 8}).status_code for i in range(7)]
    assert codes[:5] == [400] * 5 and codes[5:] == [429, 429], codes
    r = w.start(w.browser(), {"invite_token": inv["invite_token"]})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


# ───────────────────────────── sesiones ─────────────────────────────

def test_completing_the_flow_replaces_the_session_the_browser_presented(w, two):
    """Misma regla que el inicio de sesión con contraseña: una sesión nueva reemplaza a la presentada (sin apilar ni fijación)."""
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    old_cookie = A.http.cookies.get(w.session_cookie)
    subject = w.prov.account("ana@alfa.test")
    http = w.browser()
    http.cookies.set(w.session_cookie, old_cookie)                         # el navegador compartido aún tiene la sesión del dueño
    resp, _ = w.go(http, subject, token=inv["invite_token"])
    assert resp.headers["location"] == OK_LOCATION
    assert http.get("/api/auth/me").json()["email"] == "ana@alfa.test"
    probe = w.browser()
    probe.cookies.set(w.session_cookie, old_cookie)
    assert probe.get("/api/auth/me").status_code == 401, "la sesión anterior del navegador sigue viva"


def test_the_identity_sessions_of_company_a_cannot_read_company_b(w, two):
    A, B = two
    http, _, _ = joined(w, A, role="admin")
    assert http.get("/api/config/overview").status_code == 403                                   # una administradora no administra la configuración
    mine = http.get("/api/requests")
    assert mine.status_code == 200 and B.company_id not in mine.text and "Beta Servicios" not in mine.text


def test_logout_url_of_the_simulated_provider_is_not_a_real_host(w):
    assert w.prov.logout_url().startswith("https://") and ".invalid" in w.prov.logout_url()
