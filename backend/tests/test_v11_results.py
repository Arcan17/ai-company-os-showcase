"""V0.11 (spec 006, US3) — `/api/results`: tres cifras que salen de registros reales, con periodo; sin datos = `null`, nunca 0; solo con permiso; solo de SU empresa."""
from datetime import datetime, timedelta, timezone
from statistics import median

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings

from .conftest import GAS, login
from .test_v05_config import PW, login_as, make_company


def iso(days=0.0, hours=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat(timespec="milliseconds")


def company_id(app, slug="clima-sur"):
    with app.state.eng.connect() as c:
        return c.execute(text("SELECT id FROM companies WHERE slug=:s"), {"s": slug}).scalar()


def customer_id(app, cid):
    with app.state.eng.connect() as c:
        return c.execute(text("SELECT id FROM customers WHERE company_id=:c LIMIT 1"), {"c": cid}).scalar()


def add_request(app, cid, status, created, updated, n=[0]):
    n[0] += 1
    with app.state.eng.begin() as c:
        c.execute(text("INSERT INTO requests(id,company_id,customer_id,description,category,urgency,status,idem_key,created_at,updated_at) VALUES(:i,:c,:k,'prueba','mantencion','media',:s,:ik,:cr,:up)"),
                  {"i": f"req_t{n[0]:08d}", "c": cid, "k": customer_id(app, cid), "s": status, "ik": f"res:{cid}:{n[0]}", "cr": created, "up": updated})


def add_approval(app, cid, status, decided_at, n=[0]):
    n[0] += 1
    with app.state.eng.begin() as c:
        c.execute(text("INSERT INTO approvals(id,company_id,run_id,request_id,subject,summary,amount,status,decided_at,created_at) VALUES(:i,:c,'run_x','req_x',:s,'prueba',1000,:st,:d,:cr)"),
                  {"i": f"apr_t{n[0]:08d}", "c": cid, "s": f"asunto {n[0]}", "st": status, "d": decided_at, "cr": iso(1)})


def get(client):
    r = client.get("/api/results")
    assert r.status_code == 200, r.text
    return r.json()


def test_sin_datos_la_mediana_es_null_y_nunca_cero(app, owner):
    d = get(owner)
    assert d["requests_closed"] == 0 and d["approvals_decided"] == 0 and d["median_hours_to_close"] is None
    assert d["period_days"] == 30 and d["since"]


def test_las_cifras_coinciden_exactamente_con_un_conteo_independiente(app, owner):
    cid = company_id(app)
    horas = [1, 2, 6]
    for h in horas:
        add_request(app, cid, "cerrada", iso(hours=h + 5), iso(hours=5))                 # duran h horas, cerradas hace 5 h
    add_request(app, cid, "clasificada", iso(2), iso(1))                                 # abierta: no cuenta
    add_approval(app, cid, "aprobada", iso(hours=3)); add_approval(app, cid, "rechazada", iso(hours=2)); add_approval(app, cid, "pendiente", None)
    d = get(owner)
    assert d["requests_closed"] == 3
    assert d["approvals_decided"] == 2                                                   # la pendiente no es una decisión
    assert d["median_hours_to_close"] == round(median(horas), 2) == 2


def test_la_ventana_es_de_30_dias_y_una_cerrada_antigua_no_cuenta(app, owner):
    cid = company_id(app)
    add_request(app, cid, "cerrada", iso(300), iso(299.9))                               # histórico de hace 300 días
    add_request(app, cid, "cerrada", iso(31), iso(30.5))                                 # justo fuera
    add_request(app, cid, "cerrada", iso(hours=4), iso(hours=2))                         # dentro: 2 h
    add_approval(app, cid, "aprobada", iso(31)); add_approval(app, cid, "aprobada", iso(1))
    d = get(owner)
    assert (d["requests_closed"], d["approvals_decided"], d["median_hours_to_close"]) == (1, 1, 2.0)


def test_el_flujo_real_se_refleja_sin_intervencion_manual(app, owner):
    assert TestClient(app).post("/api/public/clima-sur/chat", json={**GAS, "client_msg_id": "res-0001"}).status_code == 200
    pid = owner.get("/api/dashboard").json()["pending_approvals"][0]["id"]
    assert get(owner)["approvals_decided"] == 0
    assert owner.post(f"/api/approvals/{pid}/decide", json={"decision": "approved"}).status_code == 200
    assert get(owner)["approvals_decided"] == 1


@pytest.mark.parametrize("email", ["admin@climasur.test", "tecnico@climasur.test"])
def test_sin_el_permiso_no_hay_tarjeta(app, email):
    r = login(app, email).get("/api/results")
    assert r.status_code == 403 and "requests_closed" not in r.text                     # administración y técnico/a no tienen `company.summary`: no hay tarjeta ni cifras


def test_sin_sesion_401(app):
    assert TestClient(app).get("/api/results").status_code == 401


def test_otra_empresa_no_aporta_nada(app, owner, monkeypatch):
    monkeypatch.setattr(settings, "allow_company_creation", True)
    cid = company_id(app)
    add_request(app, cid, "cerrada", iso(hours=4), iso(hours=2)); add_approval(app, cid, "aprobada", iso(hours=1))
    make_company(app)
    otra = login_as(app, "elena@aireaustral.test")
    d = get(otra)
    assert (d["requests_closed"], d["approvals_decided"], d["median_hours_to_close"]) == (0, 0, None)     # lo de Clima Sur no se mezcla
    assert get(owner)["requests_closed"] == 1


