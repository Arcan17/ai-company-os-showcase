"""V0.11 (spec 006) — `/api/demo/info` (ruta que ya existía para la demo comercial) suma `example` sin cambiar `demo`: el banner «DEMO COMERCIAL» NO debe aparecer por tener datos de ejemplo."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.public_demo import VisitorRouter


def test_apagado_por_defecto_no_es_una_demo(app):
    assert TestClient(app).get("/api/demo/info").json() == {"demo": False}                                       # apagado: exactamente la respuesta de siempre


def test_con_el_ejemplo_activado_no_se_declara_demo_comercial(app, monkeypatch):
    monkeypatch.setattr(settings, "demo_example", True)
    assert TestClient(app).get("/api/demo/info").json() == {"demo": False, "example": True}      # `demo` sigue siendo solo la demo comercial: el banner no aparece por el ejemplo


def test_la_demo_comercial_conserva_sus_campos_y_suma_example(app, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "demo_example", True)
    d = TestClient(app).get("/api/demo/info").json()
    assert d["demo"] is True and d["example"] is True and {"mode", "mode_label", "db", "commit"} <= set(d)


def test_no_revela_datos_de_ninguna_empresa(app, monkeypatch):
    monkeypatch.setattr(settings, "demo_example", True)
    r = TestClient(app).get("/api/demo/info")
    assert set(r.json()) == {"demo", "example"} and all(isinstance(v, bool) for v in r.json().values())
    assert "clima" not in r.text.lower() and "marta" not in r.text.lower()


@pytest.mark.parametrize("example", [False, True])
def test_la_demo_publica_responde_sin_crear_un_espacio(tmp_path, monkeypatch, example):
    monkeypatch.setattr(settings, "demo_example", example)
    r = VisitorRouter(tmp_path / "pd", secret=b"s" * 32, max_visitors=3, idle_seconds=60, new_per_source=50, sweep_seconds=3600)
    with TestClient(r) as c:
        resp = c.get("/api/demo/info")
        assert resp.json() == {"demo": False, "public": True, "example": example}
        assert len(r.spaces) == 0                                           # preguntar si es una demo no consume cupo
