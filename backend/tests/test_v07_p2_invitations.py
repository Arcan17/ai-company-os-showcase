"""V0.7 P2 (Issue #23) — invitaciones: correo + rol + empresa, 7 días, un solo uso, revocables, solo con `config.manage`, el secreto solo como hash, sin cruce entre empresas.
Escritas EN ROJO antes de implementar. Todo con bases TEMPORALES, el proveedor SIMULADO inyectado y un reloj controlado; no sale nada a la red ni se envía ningún correo.
LO QUE ESTAS PRUEBAS NO DEMUESTRAN: que un proveedor REAL entregue el correo, verifique la dirección o exija MFA (eso es P3/P4)."""
import hashlib
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from app import ratelimit
from app.db import all_, q

from .idp_util import FrozenClock, make_world
from .pilot_util import PW, snapshot


@pytest.fixture
def w(tmp_path, monkeypatch):
    clk = FrozenClock().install(monkeypatch)
    world = make_world(tmp_path, monkeypatch)
    world.clk = clk
    return world


@pytest.fixture
def two(w):
    return w.company("alfa", "Alfa Servicios"), w.company("beta", "Beta Servicios")


def all_text(w) -> str:
    """TODO el contenido de TODAS las tablas como texto (para comprobar que un secreto no está en ninguna parte)."""
    out = []
    with w.eng.connect() as c:
        raw = c.connection.dbapi_connection
        for (t,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in raw.execute(f'SELECT * FROM "{t}"').fetchall():
                out.append(" ".join(str(v) for v in row))
    return "\n".join(out)


# ───────────────────────────── creación ─────────────────────────────

def test_the_owner_creates_an_invitation_bound_to_email_role_and_company_for_seven_days(w, two):
    A, B = two
    r = A.post("/api/config/invitations", json={"email": "Ana.Tecnica@Alfa.test", "name": "Ana Técnica", "role": "technician"})
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["email"] == "ana.tecnica@alfa.test" and inv["role"] == "technician" and inv["status"] == "pending"
    assert inv["expires_at"] == w.clk.iso(days=7)
    assert len(inv["invite_token"]) >= 40
    row = w.rows("SELECT * FROM invitations WHERE id=:i", i=inv["id"])[0]
    assert (row["company_id"], row["email_key"], row["role"], row["status"]) == (A.company_id, "ana.tecnica@alfa.test", "technician", "pending")
    assert row["created_by"] == A.http.get("/api/auth/me").json()["id"] and row["expires_at"] == inv["expires_at"]
    assert w.count("invitations", "company_id=:c", c=B.company_id) == 0


def test_the_secret_is_stored_only_as_a_hash_and_returned_exactly_once(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    token = inv["invite_token"]
    row = w.rows("SELECT * FROM invitations WHERE id=:i", i=inv["id"])[0]
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in all_text(w), "el secreto de la invitación está en claro en alguna tabla"
    listed = A.get("/api/config/invitations")
    assert listed.status_code == 200 and token not in listed.text and row["token_hash"] not in listed.text
    assert all("invite_token" not in i and "token_hash" not in i for i in listed.json()["invitations"])


def test_invitations_are_listed_per_company_and_never_across(w, two):
    A, B = two
    inv = w.invite(A, "ana@alfa.test")
    ids_a = [i["id"] for i in A.get("/api/config/invitations").json()["invitations"]]
    assert ids_a == [inv["id"]]
    assert B.get("/api/config/invitations").json()["invitations"] == []


def test_only_config_manage_may_create_list_or_revoke(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    for who in (w.staff(A, "admin"), w.staff(A, "technician")):
        assert who.post("/api/config/invitations", json={"email": "x@alfa.test", "name": "Equis", "role": "admin"}).status_code == 403
        assert who.get("/api/config/invitations").status_code == 403
        assert who.post(f"/api/config/invitations/{inv['id']}/revoke").status_code == 403
    assert w.browser().get("/api/config/invitations").status_code == 401
    assert w.browser().post("/api/config/invitations", json={"email": "x@alfa.test", "name": "Equis", "role": "admin"}).status_code in (401, 403)
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=inv["id"])[0]["status"] == "pending"
    assert w.count("audit_log", "action='api:config.manage' AND outcome='denied' AND company_id=:c", c=A.company_id) >= 3


def test_creating_an_invitation_needs_the_csrf_token(w, two):
    A, _ = two
    r = A.http.post("/api/config/invitations", json={"email": "ana@alfa.test", "name": "Ana Técnica", "role": "technician"}, headers={"X-CSRF-Token": "mal"})
    assert r.status_code == 403 and w.count("invitations") == 0


@pytest.mark.parametrize("body", [
    {"email": "ana@alfa.test", "name": "Ana Técnica", "role": "owner"},                         # nadie se invita como dueño: hay un solo dueño, creado por el operador
    {"email": "ana@alfa.test", "name": "Ana Técnica", "role": "superuser"},
    {"email": "ana@alfa.test", "name": "Ana Técnica", "role": ""},
    {"email": "sin-arroba", "name": "Ana Técnica", "role": "technician"},
    {"email": "ana@alfa.test", "name": "A", "role": "technician"},
    {"email": "ana@alfa.test", "name": "Ana Técnica", "role": "technician", "company_id": "co_otra"},   # la empresa sale de la sesión, nunca del cuerpo
    {"email": "ana@alfa.test", "name": "Ana Técnica", "role": "technician", "expires_at": "2099-01-01"},
    {"email": "ana@alfa.test", "name": "Ana Técnica", "role": "technician", "status": "accepted"},
])
def test_invalid_or_forged_invitation_bodies_are_rejected_and_create_nothing(w, two, body):
    A, _ = two
    assert A.post("/api/config/invitations", json=body).status_code in (400, 422)
    assert w.count("invitations") == 0


# ───────────────────────────── unicidad, existentes y no revelar ─────────────────────────────

def test_only_one_pending_invitation_per_email_and_company_and_it_can_be_reissued_after_revoking_or_expiring(w, two):
    A, B = two
    first = w.invite(A, "ana@alfa.test")
    assert A.post("/api/config/invitations", json={"email": "ANA@alfa.test", "name": "Ana", "role": "admin"}).status_code == 409
    assert A.post(f"/api/config/invitations/{first['id']}/revoke").status_code == 200
    second = w.invite(A, "ana@alfa.test", role="admin")
    assert second["id"] != first["id"] and second["invite_token"] != first["invite_token"]
    w.clk.advance(days=7, seconds=1)                                                       # vence la segunda…
    A, B = w.relogin(A), w.relogin(B)                                                      # (la sesión del dueño dura 12 h)
    third = w.invite(A, "ana@alfa.test")                                                   # …y ya se puede volver a invitar
    assert third["id"] not in (first["id"], second["id"])
    assert w.invite(B, "ana@alfa.test")["id"]                                              # otra empresa puede invitar el mismo correo (no se ve entre ellas)


def test_the_database_itself_refuses_two_pending_invitations_for_one_email_in_one_company(w, two):
    A, _ = two
    w.invite(A, "ana@alfa.test")
    with pytest.raises(IntegrityError), w.eng.begin() as c:
        q(c, "INSERT INTO invitations(id,company_id,email_key,name,role,token_hash,status,created_by,created_at,expires_at) VALUES('inv_dup',:c,'ana@alfa.test','Ana','admin','h2','pending','x','t','u')", c=A.company_id)


def test_the_database_itself_refuses_a_second_link_for_the_same_subject_or_the_same_user(w, two):
    A, B = two
    ids = {r["company_id"]: r["id"] for r in w.rows("SELECT id,company_id FROM users WHERE role='owner' AND company_id IN (:a,:b)", a=A.company_id, b=B.company_id)}
    link = "INSERT INTO identities(provider,subject,user_id,company_id,created_at) VALUES('fake',:s,:u,:c,'t')"
    with w.eng.begin() as c:
        q(c, link, s="sub_1", u=ids[A.company_id], c=A.company_id)
    with pytest.raises(IntegrityError), w.eng.begin() as c:                                  # el MISMO subject para OTRA persona (y otra empresa)
        q(c, link, s="sub_1", u=ids[B.company_id], c=B.company_id)
    with pytest.raises(IntegrityError), w.eng.begin() as c:                                  # la MISMA persona con OTRO subject del mismo proveedor
        q(c, link, s="sub_2", u=ids[A.company_id], c=A.company_id)
    assert w.count("identities") == 1


def test_an_existing_employee_of_the_same_company_cannot_be_invited_again(w, two):
    A, _ = two
    w.staff(A, "technician")
    r = A.post("/api/config/invitations", json={"email": "technician1@alfa.test", "name": "Otra vez", "role": "technician"})
    assert r.status_code == 409 and w.count("invitations") == 0


def test_the_answer_does_not_reveal_whether_the_email_already_belongs_to_another_company(w, two):
    """«Misma respuesta exista o no el correo» (P0 §7): el dueño de Alfa no puede averiguar quién es cliente de Beta. Y al correo de otra empresa NO se le escribe."""
    A, B = two
    w.staff(B, "technician")                                                                 # technician1@beta.test existe en BETA
    fresh = A.post("/api/config/invitations", json={"email": "nueva@alfa.test", "name": "Nueva", "role": "technician"})
    taken = A.post("/api/config/invitations", json={"email": "technician1@beta.test", "name": "Ajena", "role": "technician"})
    assert fresh.status_code == taken.status_code == 200
    assert set(fresh.json()) == set(taken.json()) and fresh.json()["status"] == taken.json()["status"] == "pending"
    assert w.prov.outbox == ["nueva@alfa.test"], "se le escribió a un correo que ya pertenece a otra empresa"


def test_the_provider_is_asked_to_invite_only_the_invited_address(w, two):
    A, _ = two
    w.invite(A, "ana@alfa.test")
    assert w.prov.outbox == ["ana@alfa.test"]


def test_a_provider_that_fails_to_notify_does_not_undo_the_invitation_and_the_failure_is_audited(w, two, monkeypatch):
    A, _ = two
    def boom(email):
        raise RuntimeError("proveedor caído: ana@alfa.test")
    monkeypatch.setattr(w.prov, "invite", boom)
    r = A.post("/api/config/invitations", json={"email": "ana@alfa.test", "name": "Ana Técnica", "role": "technician"})
    assert r.status_code == 200 and r.json()["invite_token"]                                    # el enlace sigue sirviendo: el dueño puede entregarlo
    rows = w.rows("SELECT outcome,detail FROM audit_log WHERE company_id=:c AND action='config.invitation_notify'", c=A.company_id)
    assert [x["outcome"] for x in rows] == ["failed"] and "ana@" not in rows[0]["detail"]


# ───────────────────────────── límites ─────────────────────────────

def test_creating_invitations_is_rate_limited_per_company(w, two, monkeypatch):
    A, B = two
    monkeypatch.setattr(ratelimit, "clock", lambda: 1_000_000.0)                            # el limitador usa su PROPIO reloj (una costura): aquí no pasa el tiempo
    for i in range(20):
        w.invite(A, f"persona{i}@alfa.test")
    r = A.post("/api/config/invitations", json={"email": "una-mas@alfa.test", "name": "Una más", "role": "technician"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert w.count("invitations", "company_id=:c", c=A.company_id) == 20
    assert w.invite(B, "ana@beta.test")["id"]                                                # el tope es POR empresa: Beta no lo siente


# ───────────────────────────── revocación y caducidad visibles ─────────────────────────────

def test_revoking_marks_the_invitation_and_is_idempotent(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    r = A.post(f"/api/config/invitations/{inv['id']}/revoke")
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    row = w.rows("SELECT status,revoked_at FROM invitations WHERE id=:i", i=inv["id"])[0]
    assert row["status"] == "revoked" and row["revoked_at"] == w.clk.iso()
    again = A.post(f"/api/config/invitations/{inv['id']}/revoke")
    assert again.status_code == 200 and again.json()["deduplicated"] is True
    assert A.get("/api/config/invitations").json()["invitations"][0]["status"] == "revoked"


def test_an_expired_pending_invitation_is_listed_as_expired(w, two):
    A, _ = two
    w.invite(A, "ana@alfa.test")
    w.clk.advance(days=6, hours=23)
    A = w.relogin(A)
    assert A.get("/api/config/invitations").json()["invitations"][0]["status"] == "pending"
    w.clk.advance(hours=1, seconds=1)
    A = w.relogin(A)
    assert A.get("/api/config/invitations").json()["invitations"][0]["status"] == "expired"


def test_an_accepted_invitation_cannot_be_revoked(w, two):
    A, _ = two
    inv = w.invite(A, "ana@alfa.test")
    _, resp, _ = w.accept(inv["invite_token"], "ana@alfa.test")
    assert resp.status_code == 303
    assert A.post(f"/api/config/invitations/{inv['id']}/revoke").status_code == 409
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=inv["id"])[0]["status"] == "accepted"


# ───────────────────────────── auditoría y empresa B ─────────────────────────────

def test_creating_and_revoking_are_audited_in_the_tenant_without_secrets_or_addresses(w, two):
    A, B = two
    inv = w.invite(A, "ana.privada@alfa.test")
    A.post(f"/api/config/invitations/{inv['id']}/revoke")
    rows = all_(w.eng.connect(), "SELECT * FROM audit_log WHERE company_id=:c AND action LIKE 'config.invitation%' ORDER BY id", c=A.company_id)
    assert [(r["action"], r["resource"], r["outcome"]) for r in rows] == [("config.invitation_create", inv["id"], "ok"), ("config.invitation_revoke", inv["id"], "ok")]
    blob = " ".join(str(v) for r in rows for v in r.values())
    assert inv["invite_token"] not in blob and "ana.privada" not in blob, "la auditoría guarda el secreto o el correo de la persona"
    assert w.count("audit_log", "company_id=:c AND resource=:r", c=B.company_id, r=inv["id"]) == 0


def test_company_b_can_neither_see_nor_revoke_nor_reuse_an_invitation_of_company_a(w, two):
    A, B = two
    inv = w.invite(A, "ana@alfa.test")
    before = snapshot(w.eng, A.company_id)
    ghost = B.post("/api/config/invitations/inv_noexiste00/revoke")
    real = B.post(f"/api/config/invitations/{inv['id']}/revoke")
    assert real.status_code == ghost.status_code == 404 and real.json() == ghost.json(), "B distingue una invitación de A de una que no existe"
    assert snapshot(w.eng, A.company_id) == before and inv["id"] not in B.get("/api/config/invitations").text


def test_the_token_of_a_never_lands_a_person_in_b_and_b_cannot_accept_through_it(w, two):
    A, B = two
    inv_a = w.invite(A, "ana@alfa.test", role="admin")
    inv_b = w.invite(B, "ana@alfa.test", role="technician")                                  # el MISMO correo invitado por las dos empresas
    http, resp, subject = w.accept(inv_a["invite_token"], "ana@alfa.test")
    assert resp.status_code == 303
    me = http.get("/api/auth/me").json()
    assert me["company_id"] == A.company_id and me["role"] == "admin"                        # el token de A solo sirve para A, con el rol que fijó A
    assert w.rows("SELECT status FROM invitations WHERE id=:i", i=inv_b["id"])[0]["status"] == "pending"      # la de B sigue intacta
    assert w.count("identities", "subject=:s", s=subject) == 1 and w.count("users", "lower(email)='ana@alfa.test'") == 1


# ───────────────────────────── migración aditiva ─────────────────────────────

def test_a_pre_p2_database_migrates_additively_and_keeps_its_data_and_sessions(tmp_path, monkeypatch):
    w0 = make_world(tmp_path, monkeypatch, provider=None)
    A = w0.company("alfa")
    legacy = A.http.cookies.get(w0.session_cookie)
    path = tmp_path / "p2.db"
    w0.app.state.orch.keeper.stop()
    del w0
    db = sqlite3.connect(path)
    for t in ("identities", "invitations", "auth_flows"):
        db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute("ALTER TABLE sessions DROP COLUMN auth_time")
    users_before = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    db.commit(); db.close()
    w1 = make_world(tmp_path, monkeypatch, provider=None)                                   # arranca sobre la base «de antes»
    cols = {r["name"] for r in w1.rows("PRAGMA table_info(sessions)")}
    assert "auth_time" in cols and {"identities", "invitations", "auth_flows"} <= {r["name"] for r in w1.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert w1.count("users") == users_before
    s = w1.browser()
    s.cookies.set(w1.session_cookie, legacy)
    assert s.get("/api/auth/me").status_code == 200, "la migración cerró una sesión que existía"
    assert s.get("/api/auth/me").json()["auth_time"] is None                                 # sin dato = «sin tiempo de autenticación», nunca «reciente»
