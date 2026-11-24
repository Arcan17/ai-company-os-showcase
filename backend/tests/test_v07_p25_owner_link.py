"""V0.7 P2.5 (Issue #25, PR A) — vincular a un DUEÑO EXISTENTE con una identidad del proveedor (bloqueo B2 de P2). Escritas EN ROJO antes de implementar.
Solo una operación LOCAL del operador (la CLI `company_admin`, con acceso al archivo de la base) puede iniciar el vínculo; el secreto es de un solo uso, caduca pronto, solo existe como hash y se revoca.
El navegador no crea dueños, no eleva roles y no vincula entre empresas. Todo en bases temporales con el proveedor SIMULADO inyectado: ni red, ni cuentas, ni correos.
LO QUE NO DEMUESTRAN: nada sobre un proveedor real ni sobre MFA (P3/P4)."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import company_admin as cfg
from app.api import create_app
from app.pilot import PilotConfigError

from .idp_util import FAIL_LOCATION, GENERIC_START_FAIL, OK_LOCATION, FrozenClock, fake_provider, issues_session, make_world
from .pilot_util import PILOT_HOST, PILOT_URL, PW, pilot_on

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def w(tmp_path, monkeypatch):
    clk = FrozenClock().install(monkeypatch)
    world = make_world(tmp_path, monkeypatch)
    world.clk = clk
    return world


@pytest.fixture
def two(w):
    return w.company("alfa", "Alfa Servicios"), w.company("beta", "Beta Servicios")


def inv_mod():
    from app import invitations
    return invitations


def link(w, slug="alfa", **kw):
    return inv_mod().owner_link_create(w.eng, slug=slug, **kw)


def owner_id(w, slug):
    return w.rows("SELECT u.id FROM users u JOIN companies c ON c.id=u.company_id WHERE c.slug=:s AND u.role='owner'", s=slug)[0]["id"]


def bind(w, slug="alfa", *, email=None, verified=True, token=None, http=None):
    """El dueño crea su cuenta en el proveedor y abre el enlace que el operador le entregó. → (navegador, respuesta del callback, subject)."""
    email = email or f"dueno@{slug}.test"
    token = token or link(w, slug)["token"]
    subject = w.prov.account(email, email_verified=verified)
    http = http or w.browser()
    resp, _ = w.go(http, subject, token=token)
    return http, resp, subject


# ───────────────────────────── el secreto y la operación local ─────────────────────────────

def test_the_operator_creates_a_one_time_token_for_the_existing_owner_stored_only_as_a_hash(w, two):
    res = link(w, "alfa")
    token = res["token"]
    assert len(token) >= 40 and res["company"] == "alfa" and res["owner_email"] == "dueno@alfa.test" and res["expires_at"] == w.clk.iso(minutes=60)
    row = w.rows("SELECT * FROM invitations WHERE id=:i", i=res["invitation_id"])[0]
    assert (row["kind"], row["role"], row["status"], row["email_key"]) == ("owner_link", "owner", "pending", "dueno@alfa.test")
    assert row["company_id"] == two[0].company_id and row["target_user_id"] == owner_id(w, "alfa") and row["expires_at"] == res["expires_at"]
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest() and token not in w.all_text()
    audit = w.rows("SELECT actor_type,actor_id,outcome,detail FROM audit_log WHERE company_id=:c AND action='auth.owner_link_create'", c=two[0].company_id)
    assert [(a["actor_type"], a["actor_id"], a["outcome"]) for a in audit] == [("system", "cli", "ok")] and token not in json.dumps(audit)
    assert w.count("invitations", "kind='owner_link'", ) == 1 and w.count("invitations", "kind='owner_link' AND company_id=:c", c=two[1].company_id) == 0


@pytest.mark.parametrize("minutes,ok", [(5, True), (60, True), (1440, True), (4, False), (0, False), (-1, False), (1441, False)])
def test_the_token_lifetime_is_short_and_bounded(w, two, minutes, ok):
    from app.company_admin import ConfigError
    if ok:
        assert link(w, "alfa", ttl_minutes=minutes)["expires_at"] == w.clk.iso(minutes=minutes)
    else:
        with pytest.raises(ConfigError):
            link(w, "alfa", ttl_minutes=minutes)
        assert w.count("invitations", "kind='owner_link'") == 0


def test_there_is_one_pending_link_per_company_and_an_unknown_company_is_refused(w, two):
    from app.company_admin import ConfigError
    link(w, "alfa")
    with pytest.raises(ConfigError) as e:
        link(w, "alfa")
    assert "pendiente" in str(e.value)
    with pytest.raises(ConfigError):
        link(w, "no-existe")
    assert w.count("invitations", "kind='owner_link'") == 1 and link(w, "beta")["token"]                       # otra empresa tiene su propio enlace


def test_the_cli_prints_the_secret_once_and_exits_cleanly_or_fails_with_an_error(w, two, capsys):
    base = ["--db", w.db_path]
    assert cfg.main(["link-owner", "--company", "alfa", *base]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["token"] and out["company"] == "alfa" and out["owner_email"] == "dueno@alfa.test"
    assert cfg.main(["link-owner", "--company", "alfa", *base]) == 1 and "pendiente" in capsys.readouterr().err
    assert cfg.main(["link-owner", "--company", "no-existe", *base]) == 1 and "ERROR" in capsys.readouterr().err
    assert cfg.main(["link-owner", "--company", "beta", "--ttl-minutes", "0", *base]) == 1
    assert cfg.main(["revoke-owner-link", "--company", "alfa", *base]) == 0 and json.loads(capsys.readouterr().out)["revoked"] == 1
    assert cfg.main(["unlink-owner", "--company", "alfa", *base]) == 1, "desvincular exige confirmarlo con --yes"


def test_the_cli_in_a_real_process_never_loads_the_simulated_provider(w, two):
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}, "AICOS_DB_URL": f"sqlite:///{w.db_path}"}
    code = ("import sys; from app import company_admin as c; rc = c.main(['link-owner', '--company', 'alfa']); "
            "print('IMPORTED' if 'app.idp_fake' in sys.modules else 'ABSENT'); sys.exit(rc)")
    p = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout.strip().endswith("ABSENT") and '"token"' in p.stdout, (p.stdout, p.stderr)


# ───────────────────────────── el vínculo ─────────────────────────────

def test_the_owner_links_their_identity_and_keeps_being_the_same_single_owner(w, two):
    A, B = two
    users_before, owners_before = w.count("users"), w.count("users", "role='owner' AND company_id=:c", c=A.company_id)
    http, resp, subject = bind(w, "alfa")
    assert (resp.status_code, resp.headers["location"]) == (303, OK_LOCATION) and issues_session(resp, w.session_cookie) and resp.headers["cache-control"] == "no-store"
    me = http.get("/api/auth/me").json()
    assert (me["id"], me["role"], me["company_id"], me["email"]) == (owner_id(w, "alfa"), "owner", A.company_id, "dueno@alfa.test") and me["auth_time"]
    ident = w.rows("SELECT * FROM identities")[0]
    assert (ident["provider"], ident["subject"], ident["user_id"], ident["company_id"]) == ("fake", subject, owner_id(w, "alfa"), A.company_id)
    assert w.count("users") == users_before and w.count("users", "role='owner' AND company_id=:c", c=A.company_id) == owners_before == 1, "el vínculo creó otra persona o un segundo dueño"
    row = w.rows("SELECT status,accepted_at,accepted_user_id FROM invitations WHERE kind='owner_link'")[0]
    assert (row["status"], row["accepted_at"], row["accepted_user_id"]) == ("accepted", w.clk.iso(), owner_id(w, "alfa"))
    assert [(a["outcome"], a["actor_id"]) for a in w.rows("SELECT outcome,actor_id FROM audit_log WHERE company_id=:c AND action='auth.owner_link'", c=A.company_id)] == [("ok", owner_id(w, "alfa"))]
    assert w.count("identities", "company_id=:c", c=B.company_id) == 0


def test_after_linking_the_owner_signs_in_by_subject_and_the_password_keeps_working_outside_a_real_pilot(w, two):
    _, _, subject = bind(w, "alfa")
    again = w.browser()
    resp, _ = w.go(again, subject)
    assert resp.headers["location"] == OK_LOCATION and again.get("/api/auth/me").json()["id"] == owner_id(w, "alfa")
    assert w.browser().post("/api/auth/login", json={"email": "dueno@alfa.test", "password": PW}).status_code == 200      # (en un piloto con proveedor real la contraseña se apaga: test_v07_p2_pilot_guard)


def test_the_token_is_single_use_and_a_used_token_answers_like_a_made_up_one(w, two):
    res = link(w, "alfa")
    bind(w, "alfa", token=res["token"])
    used = w.start(w.browser(), {"invite_token": res["token"]})
    made_up = w.start(w.browser(), {"invite_token": "inventado-" * 6})
    assert used.status_code == made_up.status_code == 400 and used.json() == made_up.json() == GENERIC_START_FAIL
    assert w.count("identities") == 1


def test_two_flows_with_the_same_token_link_exactly_one_identity(w, two):
    res = link(w, "alfa")
    s1, s2 = w.prov.account("dueno@alfa.test"), w.prov.account("dueno@alfa.test")
    h1, h2 = w.browser(), w.browser()
    u1 = w.start(h1, {"invite_token": res["token"]}).json()["authorize_url"]
    u2 = w.start(h2, {"invite_token": res["token"]}).json()["authorize_url"]
    r1 = h1.get("/api/auth/callback", params=w.prov.login(u1, s1), follow_redirects=False)
    r2 = h2.get("/api/auth/callback", params=w.prov.login(u2, s2), follow_redirects=False)
    assert [r1.headers["location"], r2.headers["location"]] == [OK_LOCATION, FAIL_LOCATION] and w.count("identities") == 1


@pytest.mark.parametrize("email", ["intruso@alfa.test", "dueno+otro@alfa.test", "dueno@alfa.test.evil.test", "dueno@alfa.test ", "dueno@alfa.tеst"])    # la última lleva una «е» cirílica
def test_only_the_exact_email_of_the_existing_owner_can_link(w, two, email):
    res = link(w, "alfa")
    _, resp, _ = bind(w, "alfa", email=email, token=res["token"])
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie)
    assert w.count("identities") == 0 and w.rows("SELECT status FROM invitations WHERE kind='owner_link'")[0]["status"] == "pending"
    assert [a["outcome"] for a in w.rows("SELECT outcome FROM audit_log WHERE action='auth.owner_link' AND company_id=:c", c=two[0].company_id)] == ["denied"]


def test_email_case_does_not_matter_but_an_unverified_email_never_links(w, two):
    res = link(w, "alfa")
    _, bad, _ = bind(w, "alfa", email="dueno@alfa.test", verified=False, token=res["token"])
    assert bad.headers["location"] == FAIL_LOCATION and w.count("identities") == 0
    _, ok, _ = bind(w, "alfa", email="DUENO@Alfa.TEST", token=res["token"])
    assert ok.headers["location"] == OK_LOCATION


@pytest.mark.parametrize("when", ["before", "mid_flow"])
def test_an_expired_token_does_nothing_at_the_start_or_at_the_callback(w, two, when):
    res = link(w, "alfa", ttl_minutes=30)
    http = w.browser()
    subject = w.prov.account("dueno@alfa.test")
    if when == "before":
        w.clk.advance(minutes=31)
        r = w.start(http, {"invite_token": res["token"]})
        assert (r.status_code, r.json()) == (400, GENERIC_START_FAIL) and w.count("auth_flows") == 0
    else:
        url = w.start(http, {"invite_token": res["token"]}).json()["authorize_url"]
        back = w.prov.login(url, subject)
        w.clk.advance(minutes=31)
        assert http.get("/api/auth/callback", params=back, follow_redirects=False).headers["location"] == FAIL_LOCATION
    assert w.count("identities") == 0


@pytest.mark.parametrize("when", ["before", "mid_flow"])
def test_a_revoked_token_does_nothing_and_revoking_is_idempotent(w, two, when):
    res = link(w, "alfa")
    http = w.browser()
    subject = w.prov.account("dueno@alfa.test")
    if when == "mid_flow":
        url = w.start(http, {"invite_token": res["token"]}).json()["authorize_url"]
        back = w.prov.login(url, subject)
    assert inv_mod().owner_link_revoke(w.eng, slug="alfa") == {"revoked": 1}
    assert inv_mod().owner_link_revoke(w.eng, slug="alfa") == {"revoked": 0} and inv_mod().owner_link_revoke(w.eng, slug="beta") == {"revoked": 0}
    if when == "before":
        assert w.start(http, {"invite_token": res["token"]}).status_code == 400
    else:
        assert http.get("/api/auth/callback", params=back, follow_redirects=False).headers["location"] == FAIL_LOCATION
    assert w.count("identities") == 0 and w.rows("SELECT status FROM invitations WHERE kind='owner_link'")[0]["status"] == "revoked"
    assert w.count("audit_log", "company_id=:c AND action='auth.owner_link_revoke' AND outcome='ok'", c=two[0].company_id) == 1
    assert link(w, "alfa")["token"] != res["token"], "tras revocar se puede emitir otro"


# ───────────────────────────── empresas y roles ─────────────────────────────

def test_a_token_of_one_company_never_links_an_identity_of_the_other(w, two):
    A, B = two
    ta, tb = link(w, "alfa")["token"], link(w, "beta")["token"]
    for token, wrong_email in ((ta, "dueno@beta.test"), (tb, "dueno@alfa.test")):                            # la identidad del dueño de la OTRA empresa con el token de esta
        _, resp, _ = bind(w, "alfa", email=wrong_email, token=token)
        assert resp.headers["location"] == FAIL_LOCATION
    assert w.count("identities") == 0
    _, ok_a, _ = bind(w, "alfa", token=ta)
    _, ok_b, _ = bind(w, "beta", token=tb)
    assert ok_a.headers["location"] == ok_b.headers["location"] == OK_LOCATION
    owners = {r["company_id"]: r["user_id"] for r in w.rows("SELECT company_id,user_id FROM identities")}
    assert owners == {A.company_id: owner_id(w, "alfa"), B.company_id: owner_id(w, "beta")}


def test_a_forged_target_in_the_database_cannot_link_the_other_companys_owner(w, two):
    A, B = two
    res = link(w, "alfa")
    with w.eng.begin() as c:                                                                                   # (alguien con acceso a la base apunta el enlace de A al dueño de B)
        c.exec_driver_sql("UPDATE invitations SET target_user_id=? WHERE id=?", (owner_id(w, "beta"), res["invitation_id"]))
    _, resp, _ = bind(w, "alfa", token=res["token"])
    assert resp.headers["location"] == FAIL_LOCATION and w.count("identities") == 0


def test_a_forged_row_with_the_other_companys_owner_AND_email_still_cannot_cross_companies(w, two):
    """Aislada la defensa de empresa: el objetivo y el correo de la fila apuntan al dueño de B (el correo del proveedor coincide), pero la invitación es de A."""
    A, B = two
    res = link(w, "alfa")
    with w.eng.begin() as c:
        c.exec_driver_sql("UPDATE invitations SET target_user_id=?, email_key=? WHERE id=?", (owner_id(w, "beta"), "dueno@beta.test", res["invitation_id"]))
    _, resp, _ = bind(w, "beta", email="dueno@beta.test", token=res["token"])
    assert resp.headers["location"] == FAIL_LOCATION and w.count("identities") == 0
    assert w.rows("SELECT status FROM invitations WHERE kind='owner_link'")[0]["status"] == "pending"


def test_an_employee_invitation_can_never_create_an_owner_even_if_the_row_is_forged(w, two):
    A, _ = two
    token = "forjado-" + "x" * 40
    with w.eng.begin() as c:
        c.exec_driver_sql("INSERT INTO invitations(id,company_id,email_key,name,role,token_hash,status,created_by,created_at,expires_at,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          ("inv_forged", A.company_id, "segundo.dueno@alfa.test", "Segundo Dueño", "owner", hashlib.sha256(token.encode()).hexdigest(), "pending", owner_id(w, "alfa"),
                           w.clk.iso(), w.clk.iso(days=1), "employee"))
    subject = w.prov.account("segundo.dueno@alfa.test")
    resp, _ = w.go(w.browser(), subject, token=token)
    assert resp.headers["location"] == FAIL_LOCATION
    assert w.count("users", "role='owner' AND company_id=:c", c=A.company_id) == 1 and w.count("users", "lower(email)='segundo.dueno@alfa.test'") == 0 and w.count("identities") == 0


@pytest.mark.parametrize("extra", [{"kind": "owner_link"}, {"target_user_id": "usr_x"}, {"role": "owner", "kind": "owner_link"}])
def test_the_browser_cannot_ask_for_an_owner_link_or_an_owner_invitation(w, two, extra):
    A, _ = two
    r = A.post("/api/config/invitations", json={"email": "x@alfa.test", "name": "Equis", "role": "admin", **extra})
    assert r.status_code in (400, 422) and w.count("invitations") == 0
    r = A.post("/api/config/invitations", json={"email": "x@alfa.test", "name": "Equis", "role": "owner"})
    assert r.status_code in (400, 422) and w.count("invitations") == 0


def test_the_owner_link_is_invisible_and_untouchable_from_the_api(w, two):
    A, B = two
    res = link(w, "alfa")
    assert A.get("/api/config/invitations").json()["invitations"] == [] and B.get("/api/config/invitations").json()["invitations"] == []
    assert A.post(f"/api/config/invitations/{res['invitation_id']}/revoke").status_code == 404 and B.post(f"/api/config/invitations/{res['invitation_id']}/revoke").status_code == 404
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=res["invitation_id"])[0]["status"] == "pending"


def test_forged_callback_parameters_do_not_change_who_is_linked(w, two):
    A, B = two
    res = link(w, "alfa")
    http = w.browser()
    subject = w.prov.account("dueno@alfa.test")
    url = w.start(http, {"invite_token": res["token"]}).json()["authorize_url"]
    back = w.prov.login(url, subject)
    forged = {**back, "role": "owner", "company_id": B.company_id, "user_id": owner_id(w, "beta"), "email": "dueno@beta.test", "next": "https://evil.test/"}
    resp = http.get("/api/auth/callback", params=forged, follow_redirects=False)
    assert resp.headers["location"] == OK_LOCATION
    assert w.rows("SELECT user_id,company_id FROM identities")[0] == {"user_id": owner_id(w, "alfa"), "company_id": A.company_id}


# ───────────────────────────── un segundo vínculo exige una revocación explícita ─────────────────────────────

def test_a_second_link_needs_an_explicit_audited_unlink_first(w, two):
    from app.company_admin import ConfigError
    A, _ = two
    http, _, subject = bind(w, "alfa")
    with pytest.raises(ConfigError) as e:
        link(w, "alfa")
    assert "desvincul" in str(e.value) and w.count("invitations", "kind='owner_link' AND status='pending'") == 0
    res = inv_mod().owner_unlink(w.eng, slug="alfa")
    assert res["unlinked"] == 1 and http.get("/api/auth/me").status_code == 401, "desvincular debe cerrar las sesiones del dueño"
    assert w.count("identities") == 0 and w.count("users", "role='owner' AND company_id=:c", c=A.company_id) == 1
    assert [(a["actor_type"], a["actor_id"], a["outcome"]) for a in w.rows("SELECT actor_type,actor_id,outcome FROM audit_log WHERE company_id=:c AND action='auth.owner_unlink'", c=A.company_id)] == [("system", "cli", "ok")]
    again = w.browser()
    resp, _ = w.go(again, subject)
    assert resp.headers["location"] == FAIL_LOCATION, "la identidad desvinculada ya no entra"
    _, ok, _ = bind(w, "alfa", email="dueno@alfa.test")
    assert ok.headers["location"] == OK_LOCATION


def test_unlinking_when_nothing_is_linked_changes_nothing(w, two):
    assert inv_mod().owner_unlink(w.eng, slug="alfa") == {"unlinked": 0, "sessions_revoked": 0, "links_revoked": 0}
    assert cfg.main(["unlink-owner", "--company", "alfa", "--yes", "--db", w.db_path]) == 0
    assert w.count("audit_log", "action='auth.owner_unlink'") == 0, "un no-op no deja auditoría"


def test_a_token_issued_before_a_link_cannot_add_a_second_identity_to_the_owner(w, two):
    res = link(w, "alfa")
    with w.eng.begin() as c:                                                                                   # el dueño se vinculó por otra vía antes de que este token se usara
        c.exec_driver_sql("INSERT INTO identities(provider,subject,user_id,company_id,created_at) VALUES('fake','sub_previa',?,?,'t')", (owner_id(w, "alfa"), two[0].company_id))
    _, resp, _ = bind(w, "alfa", token=res["token"])
    assert resp.headers["location"] == FAIL_LOCATION and w.count("identities") == 1
    assert w.rows("SELECT status FROM invitations WHERE kind='owner_link'")[0]["status"] == "pending"
    assert w.count("audit_log", "company_id=:c AND action='auth.owner_link' AND outcome='denied'", c=two[0].company_id) == 1, \
        "la negativa debe ser la DISEÑADA y auditada, no un error interno rescatado por la unicidad de la base"


def test_an_owner_link_row_forged_to_point_at_an_employee_is_refused(w, two):
    """Quien edite la base no puede usar el vínculo de dueño para vincular a un EMPLEADO: el objetivo debe ser el dueño."""
    A, _ = two
    admin = w.staff(A, "admin")
    res = link(w, "alfa")
    with w.eng.begin() as c:
        c.exec_driver_sql("UPDATE invitations SET target_user_id=?, email_key=? WHERE id=?", (admin.user_id, "admin1@alfa.test", res["invitation_id"]))
    _, resp, _ = bind(w, "alfa", email="admin1@alfa.test", token=res["token"])
    assert resp.headers["location"] == FAIL_LOCATION and w.count("identities") == 0


@pytest.mark.parametrize("change", ["email", "inactive"])
def test_a_changed_or_inactive_owner_record_cannot_be_linked(w, two, change):
    res = link(w, "alfa")
    with w.eng.begin() as c:                                                                                   # el dueño cambió DESPUÉS de emitir el token
        c.exec_driver_sql("UPDATE users SET email='otro.dueno@alfa.test' WHERE id=?" if change == "email" else "UPDATE users SET active=0 WHERE id=?", (owner_id(w, "alfa"),))
    _, resp, _ = bind(w, "alfa", token=res["token"])                                                           # el proveedor trae el correo de la INVITACIÓN (el viejo): solo la segunda comprobación lo detiene
    assert resp.headers["location"] == FAIL_LOCATION and w.count("identities") == 0


# ───────────────────────────── revisión del PR #26: desvincular también revoca el enlace PENDIENTE (hallazgo P2 de revisión automatizada) ─────────────────────────────

def test_unlinking_before_the_owner_links_revokes_the_pending_token_so_it_no_longer_works(w, two, capsys):
    A, B = two
    res = link(w, "alfa")
    other = link(w, "beta")
    out = inv_mod().owner_unlink(w.eng, slug="alfa")
    assert out == {"unlinked": 0, "sessions_revoked": 0, "links_revoked": 1}
    row = w.rows("SELECT status,revoked_at FROM invitations WHERE id=:i", i=res["invitation_id"])[0]
    assert (row["status"], row["revoked_at"]) == ("revoked", w.clk.iso())
    r = w.start(w.browser(), {"invite_token": res["token"]})
    assert (r.status_code, r.json()) == (400, GENERIC_START_FAIL) and w.count("auth_flows") == 0, "el token anterior seguía iniciando el flujo"
    audit = w.rows("SELECT actor_type,actor_id,outcome,detail FROM audit_log WHERE company_id=:c AND action='auth.owner_unlink'", c=A.company_id)
    assert [(a["actor_type"], a["actor_id"], a["outcome"]) for a in audit] == [("system", "cli", "ok")] and res["token"] not in json.dumps(audit)
    assert w.count("audit_log", "company_id=:c AND action='auth.owner_unlink'", c=B.company_id) == 0
    # aislamiento: el enlace pendiente de la OTRA empresa sigue intacto y sirve
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=other["invitation_id"])[0]["status"] == "pending"
    _, ok_b, _ = bind(w, "beta", token=other["token"])
    assert ok_b.headers["location"] == OK_LOCATION
    # idempotente: repetirlo no cambia nada ni deja otra auditoría
    assert inv_mod().owner_unlink(w.eng, slug="alfa") == {"unlinked": 0, "sessions_revoked": 0, "links_revoked": 0}
    assert w.count("audit_log", "company_id=:c AND action='auth.owner_unlink'", c=A.company_id) == 1
    assert link(w, "alfa")["token"] != res["token"], "tras desvincular se puede emitir otro enlace"


def test_a_flow_already_started_with_the_token_fails_at_the_callback_after_unlinking(w, two):
    res = link(w, "alfa")
    http = w.browser()
    subject = w.prov.account("dueno@alfa.test")
    url = w.start(http, {"invite_token": res["token"]}).json()["authorize_url"]
    back = w.prov.login(url, subject)
    assert inv_mod().owner_unlink(w.eng, slug="alfa")["links_revoked"] == 1
    resp = http.get("/api/auth/callback", params=back, follow_redirects=False)
    assert resp.headers["location"] == FAIL_LOCATION and not issues_session(resp, w.session_cookie) and w.count("identities") == 0


def test_unlinking_with_both_a_linked_identity_and_a_pending_token_removes_both(w, two):
    A, _ = two
    res = link(w, "alfa")
    with w.eng.begin() as c:                                                                                   # (vinculado por otra vía mientras el token seguía pendiente)
        c.exec_driver_sql("INSERT INTO identities(provider,subject,user_id,company_id,created_at) VALUES('fake','sub_previa',?,?,'t')", (owner_id(w, "alfa"), A.company_id))
    sess = w.session("dueno@alfa.test")
    out = inv_mod().owner_unlink(w.eng, slug="alfa")
    assert out["unlinked"] == 1 and out["links_revoked"] == 1 and out["sessions_revoked"] >= 1
    assert sess.get("/api/auth/me").status_code == 401 and w.count("identities") == 0
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=res["invitation_id"])[0]["status"] == "revoked"


def test_the_cli_reports_the_revoked_pending_link(w, two, capsys):
    base = ["--db", w.db_path]
    assert cfg.main(["link-owner", "--company", "alfa", *base]) == 0
    token = json.loads(capsys.readouterr().out)["token"]
    assert cfg.main(["unlink-owner", "--company", "alfa", "--yes", *base]) == 0
    assert json.loads(capsys.readouterr().out) == {"unlinked": 0, "sessions_revoked": 0, "links_revoked": 1}
    assert w.start(w.browser(), {"invite_token": token}).status_code == 400


# ───────────────────────────── el simulado sigue bloqueado en un piloto real ─────────────────────────────

def test_a_pending_owner_link_does_not_let_the_simulated_provider_start_a_pilot(tmp_path, monkeypatch):
    FrozenClock().install(monkeypatch)
    w = make_world(tmp_path, monkeypatch)
    w.company("alfa")
    link(w, "alfa")
    w.app.state.orch.keeper.stop()
    pilot_on(monkeypatch)
    with pytest.raises(PilotConfigError) as e:
        create_app(f"sqlite:///{w.db_path}", identity_provider=fake_provider())
    assert "simulado" in str(e.value).lower() and PILOT_HOST not in str(e.value)


def test_in_a_pilot_with_a_real_provider_the_owner_links_with_host_cookies_and_then_only_the_provider_signs_them_in(tmp_path, monkeypatch):
    from .test_v07_p2_pilot_guard import RealLike
    FrozenClock().install(monkeypatch)
    w = make_world(tmp_path, monkeypatch, env="pilot", provider=RealLike())
    w.company("alfa")
    res = link(w, "alfa")
    http = w.browser()
    subject = w.prov.account("dueno@alfa.test")
    resp, _ = w.go(http, subject, token=res["token"])
    assert resp.headers["location"] == OK_LOCATION
    session = next(c for c in resp.headers.get_list("set-cookie") if c.startswith("__Host-aicos_session=") and "Max-Age=0" not in c)
    assert "Secure" in session and "HttpOnly" in session and "Domain" not in session
    assert http.get("/api/auth/me").json()["role"] == "owner"
    assert password_login(w).status_code == 403                                                              # la contraseña ya no es una puerta


def password_login(w):
    return w.browser().post("/api/auth/login", json={"email": "dueno@alfa.test", "password": PW})


# ───────────────────────────── migración ─────────────────────────────

def test_a_p2_database_without_the_new_columns_migrates_and_its_employee_invitations_keep_working(tmp_path, monkeypatch):
    FrozenClock().install(monkeypatch)
    w0 = make_world(tmp_path, monkeypatch)
    A = w0.company("alfa")
    inv = w0.invite(A, "ana@alfa.test")
    w0.app.state.orch.keeper.stop()
    import sqlite3
    db = sqlite3.connect(w0.db_path)
    db.execute("ALTER TABLE invitations DROP COLUMN kind")
    db.execute("ALTER TABLE invitations DROP COLUMN target_user_id")
    db.commit(); db.close()
    del w0
    w1 = make_world(tmp_path, monkeypatch)
    cols = {r["name"] for r in w1.rows("PRAGMA table_info(invitations)")}
    assert {"kind", "target_user_id"} <= cols
    owner = w1.session("dueno@alfa.test")
    owner.company_id, owner.slug = A.company_id, "alfa"
    assert [i["status"] for i in owner.get("/api/config/invitations").json()["invitations"]] == ["pending"]
    _, resp, _ = w1.accept(inv["invite_token"], "ana@alfa.test")
    assert resp.headers["location"] == OK_LOCATION
    assert inv_mod().owner_link_create(w1.eng, slug="alfa")["token"]
