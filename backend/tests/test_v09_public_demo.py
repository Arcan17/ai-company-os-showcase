"""V0.9 (spec 004, fase B) — demo pública: un espacio AISLADO por visitante, con caducidad, tope y límite de espacios nuevos. Datos 100 % ficticios, bases temporales."""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.public_demo import COOKIE, PublicDemoConfigError, VisitorRouter
from app.seed import DEMO_PASSWORD

from .conftest import GAS


@pytest.fixture
def router(tmp_path):
    return VisitorRouter(tmp_path / "pd", secret=b"s" * 32, max_visitors=3, idle_seconds=60, new_per_source=50, sweep_seconds=3600)


def _login(c, email="dueno@climasur.test"):
    r = c.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return r


def _chat(c):
    r = c.post("/api/public/clima-sur/chat", json={**GAS, "client_msg_id": "pd-0001"})
    assert r.status_code == 200, r.text
    return r.json()


def _pending(c):
    return c.get("/api/dashboard").json()["pending_approvals"]


def test_dos_visitantes_no_comparten_nada(router):
    with TestClient(router) as a:
        b = TestClient(router)
        _login(a); _login(b)
        assert a.cookies.get(COOKIE) and b.cookies.get(COOKIE) and a.cookies.get(COOKIE) != b.cookies.get(COOKIE)
        _chat(a)
        assert len(_pending(a)) == 1
        assert _pending(b) == []                                           # lo que hace A no existe en el espacio de B
        d = a.post(f"/api/approvals/{_pending(a)[0]['id']}/decide", json={"decision": "approved"})
        assert d.status_code == 200
        assert b.get("/api/dashboard").json()["requests_by_status"] != a.get("/api/dashboard").json()["requests_by_status"]


def test_la_sesion_de_un_visitante_no_sirve_en_el_espacio_de_otro(router):
    with TestClient(router) as a:
        b = TestClient(router)
        _login(a); b.get("/api/auth/demo-accounts")                        # B solo tiene su espacio, sin sesión
        stolen = a.cookies.get("aicos_session")
        b.cookies.set("aicos_session", stolen)
        assert b.get("/api/auth/me").status_code == 401                    # el token de A no existe en la base de B


def _set_cookie(resp):
    vals = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie" and v.startswith(COOKIE + "=")]
    return vals[0].split(";")[0].split("=", 1)[1] if vals else None


def test_una_cookie_falsa_o_alterada_crea_un_espacio_nuevo_y_no_abre_otro(router):
    with TestClient(router) as a:
        _login(a)
        vid = a.cookies.get(COOKIE).split(".")[0]
        n = len(router.spaces)
        r = TestClient(router).get("/api/auth/me", headers={"Cookie": f"{COOKIE}={vid}.AAAAAAAAAAAAAAAAAAAAAA"})        # id real, firma falsa
        assert r.status_code == 401 and _set_cookie(r) and _set_cookie(r).split(".")[0] != vid and len(router.spaces) == n + 1
        r2 = TestClient(router).get("/api/auth/me", headers={"Cookie": f"{COOKIE}=../../etc/passwd.x"})
        assert r2.status_code == 401 and _set_cookie(r2) and "/" not in _set_cookie(r2)


def test_la_cookie_del_visitante_es_httponly_samesite(router):
    with TestClient(router) as a:
        r = a.get("/api/auth/me")
        sc = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie" and v.startswith(COOKIE)][0]
        assert "HttpOnly" in sc and "SameSite=Lax" in sc and "Path=/" in sc


def test_la_interfaz_y_la_salud_no_consumen_cupo(router):
    with TestClient(router) as c:
        for _ in range(10):
            assert c.get("/api/health").json()["public_demo"] is True
            assert c.get("/").status_code in (200, 404)
        assert len(router.spaces) == 0


def test_tope_de_espacios_simultaneos(router):
    with TestClient(router) as c:
        clients = [TestClient(router) for _ in range(3)]
        for x in clients:
            assert x.get("/api/auth/me").status_code == 401                # cada uno obtiene su espacio
        extra = TestClient(router)
        r = extra.get("/api/auth/me")
        assert r.status_code == 503 and "llena" in r.json()["detail"] and r.headers["retry-after"]
        assert c.get("/api/health").status_code == 200                     # la salud sigue respondiendo con la demo llena
        assert len(router.spaces) == 3
        assert clients[0].get("/api/auth/me").status_code == 401           # quien ya tiene espacio sigue entrando


def test_limite_de_espacios_nuevos_por_origen(tmp_path):
    r = VisitorRouter(tmp_path / "pd", secret=b"s" * 32, max_visitors=50, idle_seconds=60, new_per_source=2, sweep_seconds=3600)
    with TestClient(r) as c:
        assert [TestClient(r).get("/api/auth/me").status_code for _ in range(3)] == [401, 401, 429]


def test_los_espacios_inactivos_caducan_y_se_borran(router):
    with TestClient(router) as a:
        _login(a); _chat(a)
        sp = next(iter(router.spaces.values()))
        path = sp.db_path
        assert path.exists()
        assert asyncio.run(router.sweep(now=sp.last_seen + 59)) == 0       # aún vigente
        assert router.spaces
        a.portal.call(router.sweep, sp.last_seen + 61)                     # caducado (mismo bucle de eventos que la app)
        assert not router.spaces and not path.exists()
        assert a.get("/api/auth/me").status_code == 401                    # la misma cookie ya no abre nada: empieza un espacio nuevo, vacío
        assert _pending(_login(a) and a) == []


def test_un_espacio_con_una_peticion_en_curso_no_se_borra(router):
    with TestClient(router) as a:
        a.get("/api/auth/me")
        sp = next(iter(router.spaces.values()))
        sp.active = 1
        assert a.portal.call(router.sweep, sp.last_seen + 10_000) == 0 and router.spaces
        sp.active = 0


def test_la_demo_publica_se_niega_a_arrancar_en_configuraciones_inseguras(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allow_company_creation", True)
    with pytest.raises(PublicDemoConfigError):
        VisitorRouter(tmp_path / "a")
    monkeypatch.setattr(settings, "allow_company_creation", False)
    monkeypatch.setattr(settings, "env", "pilot")
    with pytest.raises(PublicDemoConfigError):
        VisitorRouter(tmp_path / "b")


def test_el_reinicio_descarta_los_espacios_anteriores(tmp_path):
    r1 = VisitorRouter(tmp_path / "pd", secret=b"s" * 32, sweep_seconds=3600)
    with TestClient(r1) as a:
        old = _set_cookie(a.get("/api/auth/me"))
        assert old
    r2 = VisitorRouter(tmp_path / "pd", secret=b"s" * 32, sweep_seconds=3600)
    with TestClient(r2) as b:
        r = b.get("/api/auth/me", headers={"Cookie": f"{COOKIE}={old}"})     # cookie válida (firma correcta) de un proceso anterior
        assert _set_cookie(r) and _set_cookie(r) != old                      # su espacio ya no existe: recibe uno nuevo


def test_la_demo_publica_se_niega_a_arrancar_con_un_modelo_de_ia(tmp_path, monkeypatch):
    for prov in ("ollama", "anthropic", "cualquier-otro"):
        monkeypatch.setattr(settings, "llm_provider", prov)
        with pytest.raises(PublicDemoConfigError):
            VisitorRouter(tmp_path / prov)
    monkeypatch.setattr(settings, "llm_provider", "deterministic")
    VisitorRouter(tmp_path / "ok")
