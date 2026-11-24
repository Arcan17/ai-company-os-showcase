"""V0.7 (Issue #20) — la idempotencia del chat público NO debe regalar la conversación.

ANTES: `start_chat` identificaba un reintento solo por `client_msg_id` y, ante un reintento, devolvía el `conversation_token` de la conversación original a CUALQUIER petición pública de la misma
empresa que repitiera ese id (el widget lo generaba con `Math.random()`, ≈41 bits). El token es la credencial de `GET /api/public/{slug}/outbox`: quien lo tiene lee la conversación de otra persona.
DESPUÉS: el reintento legítimo demuestra POSESIÓN: la clave de idempotencia es (id del mensaje, huella del `retry_secret` que generó el cliente). Quien conoce el id pero no el secreto no encuentra
nada que repetir: su petición es una conversación NUEVA y SUYA. Sin secreto no hay idempotencia por id (cada petición es una conversación).
Datos ficticios, bases temporales; nada se explota fuera de las pruebas.
"""
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from .conftest import GAS

SECRET = "Zk3pQ9vLw2XtR7nBc5YhM1"        # 22 caracteres = 128 bits en un cliente real (CSPRNG); fijo aquí porque es una prueba
OTHER = "Hq8dVf4Ns6Ju0GbPa2TyEe"
MID = "victima-0001-abcdefghijkl"


def post(c, mid=MID, secret=None, slug="clima-sur", body=None):
    j = {**(body or GAS), "client_msg_id": mid}
    if secret is not None:
        j["retry_secret"] = secret
    return c.post(f"/api/public/{slug}/chat", json=j)


def counts(app, slug="clima-sur"):
    with app.state.eng.connect() as c:
        cid = c.execute(text("SELECT id FROM companies WHERE slug=:s"), {"s": slug}).scalar()
        return {t: c.execute(text(f"SELECT COUNT(*) FROM {t} WHERE company_id=:c"), {"c": cid}).scalar() for t in ("runs", "conversations", "messages", "requests")}


def outbox(c, token, slug="clima-sur"):
    return c.get(f"/api/public/{slug}/outbox", params={"token": token})


# ───────────── el fallo (rojo antes del arreglo) ─────────────

def test_knowing_only_the_message_id_does_not_return_the_conversation_token(app):
    victim, attacker = TestClient(app), TestClient(app)
    v = post(victim, secret=SECRET, body={**GAS, "message": "Mensaje PRIVADO de la víctima (ficticio) sobre mi aire"}).json()
    a = post(attacker)                                                  # repite el id; no tiene el secreto
    assert a.status_code == 200
    a = a.json()
    assert a["conversation_token"] != v["conversation_token"] and a["run_id"] != v["run_id"]
    assert a["deduplicated"] is False                                    # para el atacante es una conversación NUEVA y suya
    box = outbox(attacker, a["conversation_token"]).json()
    assert "PRIVADO" not in str(box)                                     # …que no contiene nada de la víctima
    assert outbox(victim, v["conversation_token"]).status_code == 200 and "PRIVADO" in str(outbox(victim, v["conversation_token"]).json())


def test_a_wrong_secret_does_not_return_the_token_either(app):
    victim, attacker = TestClient(app), TestClient(app)
    v = post(victim, secret=SECRET).json()
    a = post(attacker, secret=OTHER).json()
    assert a["conversation_token"] != v["conversation_token"] and a["run_id"] != v["run_id"] and a["deduplicated"] is False


def test_the_attackers_request_never_attaches_to_the_victims_request(app):
    """El motor también usaba el id solo como clave de la SOLICITUD: la ejecución del atacante habría reutilizado la solicitud (y el cliente) de la víctima."""
    victim, attacker = TestClient(app), TestClient(app)
    v = post(victim, secret=SECRET, body={**GAS, "name": "Cliente Víctima", "phone": "+56 9 5555 0301"}).json()
    a = post(attacker, body={**GAS, "name": "Persona Atacante", "phone": "+56 9 5555 0302"}).json()
    with app.state.eng.connect() as c:
        rows = {r[0]: r[1] for r in c.execute(text("SELECT id, request_id FROM runs WHERE id IN (:a,:b)"), {"a": v["run_id"], "b": a["run_id"]})}
    assert len(rows) == 2 and rows[v["run_id"]] != rows[a["run_id"]]
    assert "Cliente Víctima" not in str(outbox(attacker, a["conversation_token"]).json())


def test_without_a_secret_each_request_is_its_own_conversation(app):
    c = TestClient(app)
    r1, r2 = post(c).json(), post(c).json()
    assert r1["conversation_token"] != r2["conversation_token"] and r1["run_id"] != r2["run_id"] and r2["deduplicated"] is False
    assert counts(app)["runs"] == 2


@pytest.mark.parametrize("bad", ["corto", "a" * 21, "x" * 129, "con espacios y ñ aaaaaaaaaaaa", "a" * 30, "ab" * 15])
def test_the_secret_must_look_like_a_generated_secret(app, bad):
    """Longitud, alfabeto y una comprobación de cordura de variedad (no prueba entropía: el cliente DEBE usar un CSPRNG)."""
    assert post(TestClient(app), secret=bad).status_code == 422


# ───────────── lo que debe seguir funcionando (guardas) ─────────────

def test_a_legitimate_retry_with_the_same_id_and_secret_gets_the_same_conversation(app):
    c = TestClient(app)
    first = post(c, secret=SECRET).json()
    before = counts(app)
    retry = post(c, secret=SECRET).json()
    assert retry["deduplicated"] is True
    assert (retry["run_id"], retry["conversation_token"], retry["request_id"]) == (first["run_id"], first["conversation_token"], first["request_id"] or retry["request_id"])
    assert counts(app) == before                                         # ni ejecución, ni conversación, ni mensaje, ni solicitud nuevos
    assert outbox(c, retry["conversation_token"]).status_code == 200


def test_a_retry_after_the_run_finished_still_recovers_the_conversation(app, owner):
    c = TestClient(app)
    first = post(c, secret=SECRET).json()
    assert owner.get(f"/api/runs/{first['run_id']}").json()["status"] == "waiting_approval"
    again = post(c, secret=SECRET).json()
    assert again["deduplicated"] is True and again["conversation_token"] == first["conversation_token"] and again["request_id"] is not None


def test_a_burst_of_identical_requests_creates_exactly_one_run(app):
    results, errors = [], []

    def go():
        try:
            results.append(post(TestClient(app), secret=SECRET))
        except Exception as e:                                           # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and sorted({r.status_code for r in results}) == [200], [r.text[:120] for r in results if r.status_code != 200]
    assert len({r.json()["run_id"] for r in results}) == 1 and len({r.json()["conversation_token"] for r in results}) == 1
    assert sum(not r.json()["deduplicated"] for r in results) == 1
    c = counts(app)
    assert c["runs"] == 1 and c["conversations"] == 1


def test_the_same_id_and_secret_in_another_company_is_another_conversation(app):
    c = TestClient(app)
    a = post(c, secret=SECRET).json()
    b = post(c, secret=SECRET, slug="motor-norte", body={**GAS, "message": "Me suenan los frenos, necesito cambiar pastillas", "name": "Luis Herrera", "phone": "+56 9 5555 0201"}).json()
    assert b["deduplicated"] is False and b["conversation_token"] != a["conversation_token"]
    assert outbox(c, a["conversation_token"], slug="motor-norte").status_code == 404      # el token de una empresa no abre nada en la otra


def test_the_secret_is_never_stored(app):
    post(TestClient(app), secret=SECRET)
    with app.state.eng.connect() as c:
        dump = " ".join(str(v) for t in ("runs", "messages", "conversations", "requests", "audit_log", "events") for row in c.execute(text(f"SELECT * FROM {t}")) for v in row)
    assert SECRET not in dump
