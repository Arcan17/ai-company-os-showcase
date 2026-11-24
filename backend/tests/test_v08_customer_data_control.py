"""V0.8 (spec 003, historias 2 y 3) — registro de consultas, exportación y anonimización de datos de clientes. Datos 100 % ficticios."""
import json

from sqlalchemy import text

from .conftest import GAS, chat, login, pending_approval

PHONE, EMAIL, NAME = GAS["phone"], "marta.rojas@example.test", "Marta Rojas"


def _customer_id(owner):
    return next(c["id"] for c in owner.get("/api/customers").json()["customers"] if c["name"] == NAME)


def _cerrar(app, owner, anon):
    """Deja la conversación con Marta terminada (nada activo) para poder anonimizar."""
    r = chat(anon)
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    with app.state.eng.begin() as c:
        c.execute(text("UPDATE requests SET status='cerrada'"))
        c.execute(text("UPDATE runs SET status='completed' WHERE status!='completed'"))
    return r


# ── historia 2: registro de consultas ──
def test_consulta_de_ficha_queda_registrada_y_solo_la_duena_la_ve(owner, admin, tech):
    kid = _customer_id(owner)
    admin.get("/api/customers")                                        # administración abre las fichas
    log = owner.get(f"/api/customers/{kid}/access-log").json()["accesses"]
    assert {a["role"] for a in log} >= {"owner", "admin"} and all(a["detail"].startswith("campos:") for a in log)
    assert PHONE not in json.dumps(log) and EMAIL not in json.dumps(log)   # el registro NO guarda el dato personal
    assert admin.get(f"/api/customers/{kid}/access-log").status_code == 403
    assert tech.get(f"/api/customers/{kid}/access-log").status_code == 403


def test_recargar_la_misma_ficha_no_llena_el_registro(owner):
    kid = _customer_id(owner)
    for _ in range(5):
        owner.get("/api/customers")
    mine = [a for a in owner.get(f"/api/customers/{kid}/access-log").json()["accesses"] if a["role"] == "owner"]
    assert len(mine) == 1


def test_registro_de_otra_empresa_no_existe(owner, norte):
    kid = _customer_id(owner)
    assert norte.get(f"/api/customers/{kid}/access-log").status_code == 404
    assert norte.get(f"/api/customers/{kid}/export").status_code == 404
    assert norte.post(f"/api/customers/{kid}/anonymize", json={"confirm_name": NAME}).status_code == 404


# ── historia 3: exportar y anonimizar ──
def test_exportar_entrega_los_datos_y_se_audita(owner, admin, tech, app):
    kid = _customer_id(owner)
    d = owner.get(f"/api/customers/{kid}/export").json()
    assert d["customer"]["name"] == NAME and d["customer"]["phone"] == PHONE and "requests" in d
    assert admin.get(f"/api/customers/{kid}/export").status_code == 403 and tech.get(f"/api/customers/{kid}/export").status_code == 403
    with app.state.eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM audit_log WHERE action='customer.export'")).scalar() == 1


def test_anonimizar_exige_confirmar_el_nombre_exacto(owner):
    kid = _customer_id(owner)
    assert owner.post(f"/api/customers/{kid}/anonymize", json={"confirm_name": "otro nombre"}).status_code == 400
    assert owner.get(f"/api/customers/{kid}/export").json()["customer"]["name"] == NAME      # nada cambió


def test_no_se_anonimiza_con_trabajo_activo(anon, owner):
    chat(anon)                                                         # solicitud en curso, aprobación pendiente
    kid = _customer_id(owner)
    r = owner.post(f"/api/customers/{kid}/anonymize", json={"confirm_name": NAME})
    assert r.status_code == 409 and "trabajo activo" in r.json()["detail"]


def test_anonimizar_barre_todas_las_tablas_y_conserva_montos_y_fechas(anon, owner, app):
    _cerrar(app, owner, anon)
    kid = _customer_id(owner)
    with app.state.eng.connect() as c:
        quotes_before = c.execute(text("SELECT COUNT(*), COALESCE(SUM(total),0) FROM quotes")).fetchone()
    r = owner.post(f"/api/customers/{kid}/anonymize", json={"confirm_name": NAME})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["touched"] > 0
    with app.state.eng.connect() as c:
        tables = [t[0] for t in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
        for t in tables:
            cols = [x[1] for x in c.execute(text(f"PRAGMA table_info({t})"))]
            for col in cols:
                for needle in (NAME, PHONE, EMAIL, "5555 0101"):
                    n = c.execute(text(f'SELECT COUNT(*) FROM {t} WHERE CAST("{col}" AS TEXT) LIKE :l'), {"l": f"%{needle}%"}).scalar()
                    if t == "users" or t == "invitations":
                        continue
                    assert n == 0, f"{needle!r} sigue en {t}.{col}"
        assert tuple(c.execute(text("SELECT COUNT(*), COALESCE(SUM(total),0) FROM quotes")).fetchone()) == tuple(quotes_before)   # los montos se conservan
        assert c.execute(text("SELECT COUNT(*) FROM audit_log WHERE action='customer.anonymize'")).scalar() == 1
    d = owner.get(f"/api/customers/{kid}/export").json()
    assert d["customer"]["name"] == "Cliente anonimizado" and d["customer"]["phone"] is None


def test_solo_la_duena_puede_anonimizar(anon, owner, admin, tech, app):
    _cerrar(app, owner, anon)
    kid = _customer_id(owner)
    assert admin.post(f"/api/customers/{kid}/anonymize", json={"confirm_name": NAME}).status_code == 403
    assert tech.post(f"/api/customers/{kid}/anonymize", json={"confirm_name": NAME}).status_code == 403
    assert owner.get(f"/api/customers/{kid}/export").json()["customer"]["name"] == NAME
