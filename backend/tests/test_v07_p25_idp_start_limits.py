"""V0.7 P2.5 (Issue #25, PR A) — límites PERSISTENTES de `POST /api/auth/idp/start` (bloqueo B8 de P2). Escritas EN ROJO antes de implementar.
Hoy solo se limitan los intentos con un enlace de invitación INVENTADO; un inicio sin invitación (login normal) o con un enlace válido no gasta ningún cupo, así que se puede llenar `auth_flows` y abusar
del proveedor sin límite. Aquí: un cupo por ORIGEN y otro GLOBAL, guardados en la base (sobreviven a un reinicio), que se aplican ANTES de cualquier trabajo, no se pueden evadir con cabeceras y no bloquean
a quien comparte NAT con un uso normal. Bases temporales, proveedor SIMULADO inyectado, reloj del limitador controlado: sin red."""
import pytest
from fastapi.testclient import TestClient

from app import ratelimit
from app.api import create_app
from app.config import settings

from .idp_util import OK_LOCATION, FrozenClock, make_world
from .pilot_util import PILOT_URL, PW

IP = "198.51.100.10"
GENERIC_429 = "Demasiadas solicitudes de acceso desde este origen. Espera unos minutos antes de volver a intentarlo."


def narrow(monkeypatch, src="1000/60", all_="1000/60", **extra):
    """Las dos reglas de `idp/start` (`raising=False`: antes de implementar, la prueba corre y muestra el comportamiento) y, si se pide, otros ajustes."""
    monkeypatch.setattr(settings, "rl_idp_start_src", src, raising=False)
    monkeypatch.setattr(settings, "rl_idp_start_all", all_, raising=False)
    for k, v in extra.items():
        monkeypatch.setattr(settings, k, v, raising=False)


@pytest.fixture
def clock(monkeypatch):
    t = [1_900_000_000.0]
    monkeypatch.setattr(ratelimit, "clock", lambda: t[0])

    class C:
        def advance(self, s):
            t[0] += s
    return C()


@pytest.fixture
def w(tmp_path, monkeypatch, clock):
    FrozenClock().install(monkeypatch)
    return make_world(tmp_path, monkeypatch)


def codes(w, n, ip=IP, body=None, **kw):
    c = w.browser(ip)
    return [w.start(c, body if body is not None else {}, **kw).status_code for _ in range(n)]


# ───────────────────────────── por origen, sin importar si hay invitación ─────────────────────────────

def test_plain_starts_without_any_invitation_are_limited_per_source_and_the_refused_ones_create_nothing(w, monkeypatch):
    narrow(monkeypatch, src="5/600")
    c = w.browser(IP)
    rs = [w.start(c, {}) for _ in range(8)]
    assert [r.status_code for r in rs] == [200] * 5 + [429] * 3
    assert w.count("auth_flows") == 5, "un inicio rechazado no debe dejar un flujo vivo"
    r = rs[-1]
    assert int(r.headers["retry-after"]) >= 1 and r.json() == {"detail": GENERIC_429} and not any(x.startswith(w.flow_cookie + "=") for x in r.headers.get_list("set-cookie"))


def test_another_source_has_its_own_budget(w, monkeypatch):
    narrow(monkeypatch, src="3/600")
    assert codes(w, 4) == [200, 200, 200, 429]
    assert codes(w, 3, ip="198.51.100.11") == [200, 200, 200]


def test_every_kind_of_start_spends_the_same_budget_valid_token_invalid_token_plain_and_reauth(w, monkeypatch):
    narrow(monkeypatch, src="5/600")
    A = w.company("alfa")
    inv = w.invite(A, "ana@alfa.test")
    c = w.browser(IP)
    assert w.start(c, {"invite_token": inv["invite_token"]}).status_code == 200
    assert w.start(c, {"invite_token": "inventado-" * 6}).status_code == 400
    assert w.start(c, {}).status_code == 200
    assert w.start(c, {"reauth": True}).status_code == 401
    assert w.start(c, {"invite_token": inv["invite_token"]}).status_code == 200
    r = w.start(c, {"invite_token": inv["invite_token"]})
    assert r.status_code == 429, "un enlace VÁLIDO no puede saltarse el límite"


def test_a_malformed_body_still_spends_the_budget_the_limit_comes_before_any_work(w, monkeypatch):
    narrow(monkeypatch, src="3/600")
    c = w.browser(IP)
    assert [w.start(c, {"role": "owner"}).status_code for _ in range(3)] == [422, 422, 422]
    assert w.start(c, {}).status_code == 429


def test_the_budget_refills_evenly_with_the_limiters_own_clock(w, monkeypatch, clock):
    narrow(monkeypatch, src="2/600")
    assert codes(w, 3) == [200, 200, 429]
    clock.advance(299)
    assert codes(w, 1) == [429]
    clock.advance(2)                                                                                           # 300 s = una ficha de 2/600
    assert codes(w, 2) == [200, 429]


# ───────────────────────────── el origen real, nunca el que dice el cliente ─────────────────────────────

SPOOF = ["X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Host", "True-Client-IP", "CF-Connecting-IP"]


def test_forwarding_headers_from_an_untrusted_peer_cannot_evade_the_limit_or_frame_someone_else(w, monkeypatch):
    narrow(monkeypatch, src="3/600")
    c = w.browser(IP)
    out = []
    for i in range(8):
        h = {n: (f"192.0.2.{i + 1}" if n != "Forwarded" else f"for=192.0.2.{i + 1}") for n in SPOOF}
        h["X-Forwarded-For"] = f"192.0.2.{i + 1}, 192.0.2.{100 + i}"
        out.append(w.start(c, {}, headers=h).status_code)
    assert out == [200] * 3 + [429] * 5
    victim = w.browser("198.51.100.77")
    assert w.start(victim, {}).status_code == 200, "un encabezado falso no puede gastar el cupo de otra persona"
    framed = w.browser("198.51.100.78")
    assert [w.start(framed, {}, headers={"X-Forwarded-For": IP}).status_code for _ in range(3)] == [200, 200, 200]


def test_behind_a_configured_proxy_each_real_client_has_its_own_budget_and_prepending_does_nothing(w, monkeypatch):
    narrow(monkeypatch, src="2/600", trusted_proxies=("10.0.0.0/8",))
    proxy = w.browser("10.0.0.5")
    h = lambda real, spoof="1.1.1.1": {"X-Forwarded-For": f"{spoof}, {real}, 10.0.0.9"}   # noqa: E731
    got = [w.start(proxy, {}, headers=h("198.51.100.90", spoof=f"9.9.9.{i}")).status_code for i in range(3)]
    assert got == [200, 200, 429], "cambiar lo que el cliente antepone no debe servir"
    assert w.start(proxy, {}, headers=h("198.51.100.91")).status_code == 200                                    # otro cliente real detrás del mismo proxy


def test_people_sharing_one_address_are_not_blocked_by_normal_use(w):
    """Valores por defecto: 25 inicios seguidos desde UNA dirección (una oficina entera entrando a la vez) pasan."""
    assert settings.rl_idp_start_src == "30/600" and settings.rl_idp_start_all == "300/600"
    assert codes(w, 25) == [200] * 25


# ───────────────────────────── global ─────────────────────────────

def test_the_global_budget_caps_the_flows_a_botnet_can_create(w, monkeypatch):
    narrow(monkeypatch, src="1000/60", all_="4/600")
    out = [w.start(w.browser(f"198.51.100.{20 + i}"), {}).status_code for i in range(6)]
    assert out == [200] * 4 + [429] * 2 and w.count("auth_flows") == 4


def test_a_refused_start_spends_nothing_from_the_other_bucket(w, monkeypatch):
    """«Todo o nada»: si el origen ya agotó el suyo, el global no se gasta (un atacante no puede vaciar el global desde un solo origen)."""
    narrow(monkeypatch, src="2/600", all_="10/600")
    codes(w, 12)                                                                                               # 2 pasan y 10 se rechazan: el global solo gastó 2
    assert [w.start(w.browser(f"198.51.100.{40 + i}"), {}).status_code for i in range(8)] == [200] * 8        # quedaban exactamente 8


# ───────────────────────────── persistencia y privacidad ─────────────────────────────

def test_the_budgets_survive_a_restart_of_the_application(tmp_path, monkeypatch, clock):
    FrozenClock().install(monkeypatch)
    narrow(monkeypatch, src="3/600")
    w0 = make_world(tmp_path, monkeypatch)
    assert codes(w0, 3) == [200, 200, 200]
    w0.app.state.orch.keeper.stop()
    del w0
    w1 = make_world(tmp_path, monkeypatch)                                                                      # otro proceso, la misma base
    assert codes(w1, 2) == [429, 429], "el límite vivía solo en memoria"


def test_no_address_is_stored_in_clear_anywhere_and_the_buckets_have_no_company(w, monkeypatch):
    narrow(monkeypatch, src="5/600")
    codes(w, 3, ip="203.0.113.77")
    assert "203.0.113.77" not in w.all_text()
    rows = w.rows("SELECT kind,company_id FROM rate_limits WHERE kind LIKE 'idp.start%'")
    assert sorted(r["kind"] for r in rows) == ["idp.start.all", "idp.start.src"] and all(r["company_id"] is None for r in rows)


@pytest.mark.parametrize("name", ["rl_idp_start_src", "rl_idp_start_all"])
@pytest.mark.parametrize("bad", ["abc", "0/60", "5/0", "5", "5/100000"])
def test_a_malformed_rule_stops_the_server_at_startup(tmp_path, monkeypatch, name, bad):
    monkeypatch.setattr(settings, name, bad, raising=False)
    with pytest.raises(ValueError) as e:
        create_app(f"sqlite:///{tmp_path / 'x.db'}")
    assert "AICOS_" + name.upper() in str(e.value)
    assert not (tmp_path / "x.db").exists()


# ───────────────────────────── lo que NO debe cambiar ─────────────────────────────

def test_a_flow_already_started_still_completes_after_the_budget_is_spent_and_other_routes_are_unaffected(w, monkeypatch):
    narrow(monkeypatch, src="2/600")
    A = w.company("alfa")
    inv = w.invite(A, "ana@alfa.test")
    http = w.browser(IP)
    url = w.start(http, {"invite_token": inv["invite_token"]}).json()["authorize_url"]
    back = w.prov.login(url, w.prov.account("ana@alfa.test"))
    noisy = w.browser(IP)                                                                                      # (OTRO navegador del mismo origen: iniciar otro flujo en el MISMO navegador reemplazaría su cookie)
    assert w.start(noisy, {}).status_code == 200 and w.start(noisy, {}).status_code == 429
    assert http.get("/api/auth/callback", params=back, follow_redirects=False).headers["location"] == OK_LOCATION      # el callback no gasta ni depende del cupo
    assert w.browser(IP).post("/api/auth/login", json={"email": "dueno@alfa.test", "password": PW}).status_code == 200  # ni el inicio con contraseña
    assert A.post("/api/auth/reauth", json={"password": PW}).status_code == 200
    assert w.invite(A, "beto@alfa.test")["invite_token"]                                                                # ni la creación de invitaciones


def test_the_limit_also_holds_in_a_pilot_with_a_real_provider(tmp_path, monkeypatch, clock):
    from .test_v07_p2_pilot_guard import RealLike
    FrozenClock().install(monkeypatch)
    narrow(monkeypatch, src="3/600")
    w = make_world(tmp_path, monkeypatch, env="pilot", provider=RealLike())
    c = TestClient(w.app, base_url=PILOT_URL, client=(IP, 50000))
    out = [w.start(c, {}) for _ in range(5)]
    assert [r.status_code for r in out] == [200] * 3 + [429] * 2
    assert out[-1].headers["cache-control"] == "no-store" and "strict-transport-security" in out[-1].headers


def test_without_a_provider_the_route_does_not_exist_so_there_is_nothing_to_limit(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, provider=None)
    assert w.browser(IP).post("/api/auth/idp/start", json={}).status_code in (404, 405)


# ───────────────────────────── revisión del PR #26: el límite va ANTES de analizar el cuerpo (hallazgo P2 de revisión automatizada) ─────────────────────────────
# FastAPI rechaza un JSON sintácticamente inválido ANTES de resolver las dependencias: un límite puesto como `Depends` no se cobra ni se aplica a esas peticiones. El límite vive ahora a nivel ASGI.

INVALID_BODIES = [(b"{", "application/json"), (b"", "application/json"), (b"[1,2", "application/json"), (b"\xff\xfe\x00", "application/json"), (b'{"invite_token": ', "application/json"),
                  (b"not json at all", "text/plain"), (b"a=1&b=2", "application/x-www-form-urlencoded")]


@pytest.mark.parametrize("raw,ctype", INVALID_BODIES)
def test_a_syntactically_invalid_body_spends_the_budget_and_gets_429_once_exhausted_before_any_parsing(w, monkeypatch, raw, ctype):
    narrow(monkeypatch, src="3/600")
    c = w.browser(IP)
    post = lambda: c.post("/api/auth/idp/start", content=raw, headers={"Content-Type": ctype})   # noqa: E731
    first = [post().status_code for _ in range(3)]
    assert all(code in (400, 415, 422) for code in first), first                                    # el cuerpo basura se rechaza como siempre…
    r = post()
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json() == {"detail": GENERIC_429}, "…pero ya gastó el cupo: el siguiente, aunque sea basura, recibe 429 SIN analizarse"
    assert w.start(c, {}).status_code == 429, "y el origen no puede hacer un inicio válido hasta que el cupo se reponga"
    assert w.count("auth_flows") == 0 and not any(x.startswith(w.flow_cookie + "=") for x in r.headers.get_list("set-cookie"))


def test_garbage_cannot_be_used_to_probe_after_the_budget_is_gone_valid_json_and_invalid_json_get_the_same_429(w, monkeypatch):
    narrow(monkeypatch, src="2/600")
    c = w.browser(IP)
    codes(w, 2)
    bad = c.post("/api/auth/idp/start", content=b"{", headers={"Content-Type": "application/json"})
    good = w.start(c, {})
    wrong_type = c.post("/api/auth/idp/start", json={"role": "owner"})
    assert (bad.status_code, good.status_code, wrong_type.status_code) == (429, 429, 429) and bad.json() == good.json() == wrong_type.json()


def test_a_request_refused_before_the_limiter_spends_nothing_csrf_and_foreign_origin(w, monkeypatch):
    """CsrfMiddleware es MÁS externo: sus 403 nunca llegan al limitador y no gastan cupo."""
    narrow(monkeypatch, src="3/600")
    A = w.company("alfa")                                                                            # (la sesión del dueño comparte el origen «testclient» con w.browser())
    for _ in range(5):
        assert A.http.post("/api/auth/idp/start", json={"reauth": True}, headers={"X-CSRF-Token": "mal"}).status_code == 403
        assert w.browser().post("/api/auth/idp/start", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert w.browser().post("/api/auth/idp/start", json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert [w.start(w.browser(), {}).status_code for _ in range(4)] == [200, 200, 200, 429], "los 403 anteriores gastaron cupo"


def test_only_a_post_to_the_exact_path_spends_the_budget(w, monkeypatch):
    narrow(monkeypatch, src="3/600")
    c = w.browser()
    for i in range(6):
        assert c.get("/api/auth/idp/start").status_code == 405
        assert c.post("/api/auth/idp/startx", json={}).status_code == 404
        assert c.post("/api/auth/idp", json={}).status_code in (404, 405)
        assert c.get("/api/auth/callback", params={"code": "x", "state": "y"}, follow_redirects=False).status_code == 303
        assert c.post("/api/auth/login", json={"email": f"nadie{i}@alfa.test", "password": "x" * 12}).status_code == 401      # (un correo distinto cada vez: el límite de fallos de LOGIN es por correo y no es el que se prueba aquí)
    assert [w.start(c, {}).status_code for _ in range(4)] == [200, 200, 200, 429]


def test_a_standby_instance_shares_the_budget_with_the_active_one_and_its_503s_spend_nothing(tmp_path, monkeypatch, clock):
    """Dos aplicaciones sobre LA MISMA base: la primera tiene el arrendamiento (activa), la segunda queda en espera. Su 503 (ActiveGate) no gasta cupo; sus inicios de acceso (las rutas /api/auth/* se atienden en espera) sí gastan EL MISMO cupo."""
    from .idp_util import World
    FrozenClock().install(monkeypatch)
    narrow(monkeypatch, src="5/600")
    active = make_world(tmp_path, monkeypatch)
    A = active.company("alfa")
    from datetime import datetime, timezone
    with active.eng.begin() as c:                                                                       # (en un mismo proceso el mismo pid nunca es «otra instancia»: se planta un arrendamiento FRESCO de otra instancia viva — el pid 1 siempre existe)
        c.exec_driver_sql("UPDATE instance_lease SET boot_id='otra-instancia', pid=1, heartbeat_at=? WHERE id=1", (datetime.now(timezone.utc).isoformat(timespec="milliseconds"),))
    standby_app = create_app(f"sqlite:///{active.db_path}", identity_provider=active.prov)
    assert standby_app.state.orch.active is False
    standby = World(standby_app, active.prov, "local", active.base_url)
    owner_on_standby = standby.session("dueno@alfa.test")
    assert codes(active, 2) == [200, 200]
    for _ in range(4):                                                                                  # escrituras que la instancia en espera rechaza con 503: no gastan
        assert owner_on_standby.post("/api/config/employees", json={"name": "Zed Uno", "email": "zed@alfa.test", "role": "technician"}).status_code == 503
    assert [standby.start(standby.browser(IP), {}).status_code for _ in range(4)] == [200, 200, 200, 429], "el cupo es UNO solo para las dos instancias (2 + 3 = 5)"
    assert active.start(active.browser(IP), {}).status_code == 429
    standby_app.state.orch.keeper.stop()
    active.app.state.orch.keeper.stop()


def test_a_limiter_that_cannot_answer_fails_closed_and_does_not_let_the_request_through(w, monkeypatch):
    def broken(self, buckets):
        raise RuntimeError("base no disponible")
    monkeypatch.setattr(ratelimit.Limiter, "take", broken)
    r = w.browser(IP).post("/api/auth/idp/start", content=b"{", headers={"Content-Type": "application/json"})
    assert r.status_code == 503 and w.count("auth_flows") == 0
