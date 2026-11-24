"""V0.6 P4 (Issue #13) — abuso del canal público `/api/public/*` y su límite de tasa persistente.

Antes del arreglo estas pruebas MUESTRAN el abuso (todas las peticiones pasan, se crean runs sin tope, un código de 6 hex se adivina a ritmo ilimitado, `X-Forwarded-For`
cambia de «identidad» a voluntad). Después fijan el contrato: 429 + Retry-After, sin efectos, aislado por empresa, resistente a concurrencia y a varios procesos,
sin confiar en cabeceras de cliente, sin IPs en claro y con estado que expira.

Convenciones: `limits()` fija solo las reglas que cada prueba quiere ejercitar (el resto queda «no vinculante»: 1000/60); `at(app, ip)` es un cliente con esa IP de origen;
`clock` es la costura de tiempo del limitador (nunca `time.time` global).
"""
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import backup, ratelimit
from app.api import create_app
from app.config import settings
from app.seed import DEMO_PASSWORD

from .conftest import GAS, PLACA, chat, pending_approval

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
RULES = ("rl_chat_src", "rl_chat_co", "rl_outbox_src", "rl_outbox_co", "rl_outbox_miss", "rl_confirm_src", "rl_confirm_target", "rl_confirm_co", "rl_unknown_src", "rl_unknown_all")
WORK = ("runs", "requests", "messages", "conversations", "visits", "approvals", "tasks", "events", "audit_log", "commitments", "work_orders", "customers", "login_attempts")
NORTE = {"message": "Me suenan los frenos, necesito cambiar pastillas", "name": "Luis Herrera", "phone": "+56 9 5555 0201"}


def limits(monkeypatch, **narrow):
    """Every public rule non-binding (1000 per minute) except the ones the test narrows. `raising=False` so that, before the fix, the test runs and shows the behaviour."""
    for k in RULES:
        monkeypatch.setattr(settings, k, narrow.pop(k, "1000/60"), raising=False)
    monkeypatch.setattr(settings, "trusted_proxies", narrow.pop("trusted_proxies", ()), raising=False)
    assert not narrow, f"regla desconocida: {narrow}"


@pytest.fixture
def clock(monkeypatch):
    t = [1_900_000_000.0]
    monkeypatch.setattr(ratelimit, "clock", lambda: t[0])

    class C:
        def advance(self, s):
            t[0] += s
    return C()


def at(app, ip, **kw):
    return TestClient(app, client=(ip, 50000), **kw)


def body(i, base=GAS, prefix="rl"):
    return {**base, "client_msg_id": f"{prefix}-{i:05d}", "retry_secret": f"{prefix}-secreto-{i:05d}-abcdefghij"}   # Issue #20: sin secreto no hay idempotencia por id


def post_chat(c, i, slug="clima-sur", base=GAS, prefix="rl", **kw):
    return c.post(f"/api/public/{slug}/chat", json=body(i, base, prefix), **kw)


def snapshot(app):
    with app.state.eng.connect() as c:
        return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in WORK}


def rl_rows(app):
    try:
        with app.state.eng.connect() as c:
            return c.execute(text("SELECT count(*) FROM rate_limits")).scalar()
    except Exception:
        return None


def ok_codes(rs):
    return [r.status_code for r in rs]


# ============================================================ 1. el abuso del chat público
def test_a_burst_of_chats_from_one_source_is_cut_off_and_after_the_limit_nothing_is_created(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="4/600")
    a = at(app, "198.51.100.10")
    first = [post_chat(a, i) for i in range(4)]
    assert ok_codes(first) == [200] * 4
    before = snapshot(app)
    over = [post_chat(a, 100 + i) for i in range(6)]
    assert ok_codes(over) == [429] * 6
    for r in over:
        wait = int(r.headers["Retry-After"])
        assert 1 <= wait <= 600
        assert "solicitudes" in r.json()["detail"].lower()
    assert snapshot(app) == before                                   # ni runs, ni solicitudes, ni conversaciones, ni mensajes, ni auditoría
    assert before["runs"] == 4 and before["conversations"] == 4


def test_a_flood_from_many_sources_hits_the_company_cap_and_the_other_company_is_untouched(app, monkeypatch):
    limits(monkeypatch, rl_chat_co="6/3600")
    victims = [post_chat(at(app, f"198.51.100.{20 + i}"), i).status_code for i in range(8)]
    assert victims == [200] * 6 + [429] * 2                          # la tapa de la empresa frena a quien rota de dirección
    assert post_chat(at(app, "198.51.100.99"), 900, slug="motor-norte", base=NORTE).status_code == 200
    assert post_chat(at(app, "198.51.100.20"), 901, slug="motor-norte", base=NORTE).status_code == 200   # incluso una fuente ya bloqueada en Clima Sur


def test_the_per_source_budget_belongs_to_one_company_only(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="3/600")
    a = at(app, "198.51.100.30")
    assert ok_codes([post_chat(a, i) for i in range(4)]) == [200, 200, 200, 429]
    assert ok_codes([post_chat(a, 50 + i, slug="motor-norte", base=NORTE) for i in range(4)]) == [200, 200, 200, 429]
    assert post_chat(at(app, "198.51.100.31"), 99).status_code == 200      # otra fuente de la misma empresa no se ve afectada


def test_a_duplicate_client_msg_id_is_deduplicated_but_never_free(app, monkeypatch, clock):
    limits(monkeypatch, rl_chat_src="3/600")
    a = at(app, "198.51.100.40")
    rs = [a.post("/api/public/clima-sur/chat", json=body(1)) for _ in range(3)]
    assert ok_codes(rs) == [200, 200, 200] and [r.json()["deduplicated"] for r in rs] == [False, True, True]
    assert len({r.json()["run_id"] for r in rs}) == 1
    before = snapshot(app)
    again = a.post("/api/public/clima-sur/chat", json=body(1))
    assert again.status_code == 429                                  # el reintento duplicado también consume presupuesto: no premia el spam
    assert snapshot(app) == before and before["runs"] == 1
    clock.advance(600)
    later = a.post("/api/public/clima-sur/chat", json=body(1))
    assert later.status_code == 200 and later.json()["deduplicated"] is True and snapshot(app) == before   # pasada la ventana: misma ejecución, sin duplicar


def test_budgets_recover_with_time(app, monkeypatch, clock):
    limits(monkeypatch, rl_chat_src="2/60")
    a = at(app, "198.51.100.50")
    assert ok_codes([post_chat(a, i) for i in range(3)]) == [200, 200, 429]
    clock.advance(29)
    assert post_chat(a, 10).status_code == 429                       # 2 por minuto = una cada 30 s: todavía no
    clock.advance(2)
    assert post_chat(a, 11).status_code == 200
    assert post_chat(a, 12).status_code == 429


# ============================================================ 2. códigos de confirmación de visitas
def _pending_visit(app, owner):
    r = chat(at(app, "192.0.2.1"), mid="rl-setup-0001")
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    rid = owner.get(f"/api/runs/{r['run_id']}").json()["request_id"]
    box = at(app, "192.0.2.1").get(f"/api/public/clima-sur/outbox?token={r['conversation_token']}").json()
    code = [m for m in box["messages"] if m["status"] == "preparado_no_enviado"][0]["body"].split("confirmación: ")[1].strip(".")
    return rid, code, r["conversation_token"]


def _status(owner, rid):
    return owner.get(f"/api/requests/{rid}").json()["visit"]["status"]


def test_confirmation_codes_cannot_be_guessed_by_rotating_sources_and_the_right_code_is_no_oracle(app, owner, monkeypatch, clock):
    limits(monkeypatch, rl_confirm_target="3/900")
    rid, code, _ = _pending_visit(app, owner)
    wrong = [at(app, f"203.0.113.{10 + i}").post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": f"BAD{i:03d}"}).status_code for i in range(10)]
    assert wrong == [400] * 3 + [429] * 7                            # diez fuentes distintas: la solicitud solo admite 3 intentos, vengan de donde vengan
    fresh = at(app, "203.0.113.200").post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": code})
    assert fresh.status_code == 429 and _status(owner, rid) == "propuesta"   # ni siquiera el código correcto responde distinto: no hay oráculo
    clock.advance(900)
    ok = at(app, "203.0.113.201").post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": code})
    assert ok.status_code == 200 and ok.json()["status"] == "confirmada" and _status(owner, rid) == "confirmada"


def test_one_source_cannot_spray_many_requests_either(app, monkeypatch):
    limits(monkeypatch, rl_confirm_src="5/600")
    a = at(app, "203.0.113.50")
    codes = [a.post("/api/public/clima-sur/visits/confirm", json={"request_id": f"req_{i:012x}", "code": "ABC123"}).status_code for i in range(8)]
    assert codes == [400] * 5 + [429] * 3
    assert at(app, "203.0.113.51").post("/api/public/clima-sur/visits/confirm", json={"request_id": "req_000000000000", "code": "ABC123"}).status_code == 400


def test_confirmation_is_stricter_than_outbox_reads_for_the_same_source(app, owner, monkeypatch):
    limits(monkeypatch, rl_confirm_src="5/600", rl_outbox_src="60/60")
    rid, code, token = _pending_visit(app, owner)
    a = at(app, "203.0.113.60")
    assert ok_codes([a.post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": "NOPE00"}) for _ in range(6)]) == [400] * 5 + [429]
    assert ok_codes([a.get(f"/api/public/clima-sur/outbox?token={token}") for _ in range(20)]) == [200] * 20     # la lectura sigue disponible: otro presupuesto


def test_confirmation_company_cap_is_independent_of_the_target_and_the_source(app, monkeypatch):
    limits(monkeypatch, rl_confirm_co="6/600")
    codes = [at(app, f"203.0.113.{80 + i}").post("/api/public/clima-sur/visits/confirm", json={"request_id": f"req_{i:012x}", "code": "ABC123"}).status_code for i in range(9)]
    assert codes == [400] * 6 + [429] * 3
    assert at(app, "203.0.113.150").post("/api/public/motor-norte/visits/confirm", json={"request_id": "req_000000000000", "code": "ABC123"}).status_code == 400


# ============================================================ 3. consultas de conversaciones (outbox)
def test_a_legitimate_poller_every_1_2_seconds_is_never_limited(app, monkeypatch, clock):
    limits(monkeypatch, rl_outbox_src="60/60", rl_outbox_co="120/60", rl_outbox_miss="3/300")
    c = at(app, "198.51.100.60")
    token = chat(c, mid="rl-poll-0001")["conversation_token"]
    codes = []
    for _ in range(150):                                             # 3 minutos de sondeo de una pestaña (1,2 s) con un presupuesto de 60/min
        codes.append(c.get(f"/api/public/clima-sur/outbox?token={token}").status_code)
        clock.advance(1.2)
    assert set(codes) == {200}


def test_reading_outboxes_faster_than_the_budget_is_cut_off(app, monkeypatch):
    limits(monkeypatch, rl_outbox_src="5/60")
    c = at(app, "198.51.100.61")
    token = chat(c, mid="rl-poll-0002")["conversation_token"]
    assert ok_codes([c.get(f"/api/public/clima-sur/outbox?token={token}") for _ in range(7)]) == [200] * 5 + [429] * 2
    assert at(app, "198.51.100.62").get(f"/api/public/clima-sur/outbox?token={token}").status_code == 200


def test_invalid_tokens_burn_a_strict_miss_budget_and_a_valid_token_leaks_nothing(app, monkeypatch, clock):
    limits(monkeypatch, rl_outbox_miss="3/300")
    owner_c = at(app, "198.51.100.70")
    token = chat(owner_c, mid="rl-miss-0001")["conversation_token"]
    atk = at(app, "198.51.100.71")
    assert ok_codes([atk.get(f"/api/public/clima-sur/outbox?token=tok_{i:024x}") for i in range(3)]) == [404] * 3
    guess = atk.get("/api/public/clima-sur/outbox?token=tok_ffffffffffffffffffffffff")
    valid = atk.get(f"/api/public/clima-sur/outbox?token={token}")
    assert guess.status_code == 429 and valid.status_code == 429      # agotado el presupuesto, acertar y fallar se ven igual (sin oráculo)
    assert guess.headers["Retry-After"] and valid.headers["Retry-After"]
    assert at(app, "198.51.100.72").get(f"/api/public/clima-sur/outbox?token={token}").status_code == 200     # el cliente legítimo, desde su propia fuente, lee con normalidad
    clock.advance(300)
    assert atk.get(f"/api/public/clima-sur/outbox?token={token}").status_code == 200


def test_valid_reads_do_not_consume_the_miss_budget(app, monkeypatch):
    limits(monkeypatch, rl_outbox_miss="3/300")
    c = at(app, "198.51.100.73")
    token = chat(c, mid="rl-miss-0002")["conversation_token"]
    read = lambda t: c.get(f"/api/public/clima-sur/outbox?token={t}").status_code   # noqa: E731
    assert [read(token) for _ in range(20)] == [200] * 20                          # veinte lecturas válidas seguidas: ninguna gasta el presupuesto de fallos
    assert [read(f"tok_{i:024x}") for i in range(3)] == [404] * 3                  # …así que los tres fallos que sí se permiten siguen enteros
    assert read("tok_ffffffffffffffffffffffff") == 429                             # y el cuarto ya no
    assert read(token) == 429                                                      # agotado el presupuesto de fallos, una lectura válida de esa fuente tampoco pasa (sin oráculo)


def test_tokens_of_another_company_are_never_readable_and_cost_the_same(app, monkeypatch):
    limits(monkeypatch, rl_outbox_miss="2/300")
    token = chat(at(app, "198.51.100.74"), mid="rl-x-0001")["conversation_token"]
    atk = at(app, "198.51.100.75")
    assert ok_codes([atk.get(f"/api/public/motor-norte/outbox?token={token}") for _ in range(3)]) == [404, 404, 429]


# ============================================================ 4. la identidad de la fuente no la dicta el cliente
SPOOF = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "Client-IP", "True-Client-IP", "CF-Connecting-IP", "X-Client-IP", "X-Forwarded-Host")


def test_forwarding_headers_from_an_untrusted_peer_cannot_evade_the_limit(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="3/600")
    a = at(app, "198.51.100.80")
    codes = []
    for i in range(12):
        h = {name: f"192.0.2.{i + 1}" if name != "Forwarded" else f"for=192.0.2.{i + 1}" for name in SPOOF}
        h["X-Forwarded-For"] = f"192.0.2.{i + 1}, 192.0.2.{100 + i}"
        codes.append(post_chat(a, i, headers=h).status_code)
    assert codes == [200] * 3 + [429] * 9


def test_a_spoofed_header_cannot_frame_someone_else(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="2/600")
    atk = at(app, "198.51.100.81")
    assert ok_codes([post_chat(atk, i, headers={"X-Forwarded-For": "198.51.100.82"}) for i in range(3)]) == [200, 200, 429]
    assert post_chat(at(app, "198.51.100.82"), 50).status_code == 200      # la «víctima» real no pierde su presupuesto


def test_a_configured_trusted_proxy_yields_the_real_client_and_ignores_what_the_client_prepends(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="2/600", trusted_proxies=("10.0.0.0/8",))
    proxy = at(app, "10.0.0.5")
    h = lambda real, spoof="1.1.1.1": {"X-Forwarded-For": f"{spoof}, {real}, 10.0.0.9"}   # noqa: E731  (el proxy añade a la derecha; lo del cliente va a la izquierda)
    assert ok_codes([post_chat(proxy, i, headers=h("198.51.100.90", spoof=f"9.9.9.{i}")) for i in range(3)]) == [200, 200, 429]    # cambiar lo de la izquierda no sirve
    assert post_chat(proxy, 20, headers=h("198.51.100.91")).status_code == 200                                                     # otro cliente real detrás del mismo proxy
    assert post_chat(proxy, 21).status_code == 200 and post_chat(proxy, 22).status_code == 200 and post_chat(proxy, 23).status_code == 429   # sin cabecera: cae en la fuente del proxy


def test_only_a_trusted_peer_is_believed(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="2/600", trusted_proxies=("10.0.0.0/8",))
    stranger = at(app, "198.51.100.95")
    codes = [post_chat(stranger, i, headers={"X-Forwarded-For": f"10.0.0.{i + 1}, 192.0.2.{i + 1}"}).status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_ipv6_sources_share_their_64_prefix_and_v4_mapped_is_the_same_as_v4(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="2/600")
    a, b = at(app, "2001:db8:1:2::1"), at(app, "2001:db8:1:2:ffff:ffff:ffff:9")
    assert ok_codes([post_chat(a, 0), post_chat(b, 1), post_chat(a, 2), post_chat(b, 3)]) == [200, 200, 429, 429]     # rotar dentro de un /64 no da fuentes nuevas
    assert post_chat(at(app, "2001:db8:1:3::1"), 4).status_code == 200
    v4, mapped = at(app, "203.0.113.7"), at(app, "::ffff:203.0.113.7")
    assert ok_codes([post_chat(v4, 10), post_chat(mapped, 11), post_chat(v4, 12)]) == [200, 200, 429]


# ============================================================ 5. concurrencia y varios procesos
def _race(n, fn):
    barrier, out = threading.Barrier(n), [None] * n

    def work(i):
        barrier.wait()
        out[i] = fn(i)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return out


def test_parallel_guesses_cannot_exceed_the_budget_by_racing(app, monkeypatch):
    limits(monkeypatch, rl_outbox_miss="7/300")
    res = _race(60, lambda i: at(app, "198.51.100.110").get(f"/api/public/clima-sur/outbox?token=tok_{i:024x}").status_code)
    assert sorted(res).count(404) == 7 and sorted(res).count(429) == 53 and set(res) <= {404, 429}


def test_parallel_chats_cannot_exceed_the_budget_nor_create_extra_runs(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="5/600")
    res = _race(24, lambda i: post_chat(at(app, "198.51.100.111"), i, prefix="race").status_code)
    assert sorted(res).count(200) == 5 and sorted(res).count(429) == 19
    assert snapshot(app)["runs"] == 5 and snapshot(app)["conversations"] == 5


def test_parallel_confirmations_of_one_request_share_one_target_budget(app, owner, monkeypatch):
    limits(monkeypatch, rl_confirm_target="4/900")
    rid, code, _ = _pending_visit(app, owner)
    res = _race(30, lambda i: at(app, f"203.0.113.{100 + i}").post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": f"BAD{i:03d}"}).status_code)
    assert sorted(res).count(400) == 4 and sorted(res).count(429) == 26


def _spawn(url, env_extra):
    """A real server exactly as this repo documents launching it: uvicorn WITHOUT its own proxy-header handling (trust is AICOS_TRUSTED_PROXIES only)."""
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    script = ("import sys,uvicorn;from app.api import create_app;"
              "uvicorn.run(create_app(sys.argv[1]),host='127.0.0.1',port=int(sys.argv[2]),log_level='warning',proxy_headers=False)")
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "AICOS_STEP_DELAY": "0", "AICOS_SYNC": "0", "AICOS_LLM_PROVIDER": "deterministic", "AICOS_LEASE_HEARTBEAT": "0.4",
           "AICOS_STATIC_DIR": str(BACKEND / "data" / "nonexistent-static"), **env_extra}
    p = subprocess.Popen([sys.executable, "-W", "ignore", "-c", script, url, str(port)], cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                return p, base
        except httpx.HTTPError:
            time.sleep(0.25)
    p.kill()
    raise AssertionError("el servidor de prueba no arrancó")


def _open_env():
    return {name.replace("rl_", "AICOS_RL_").upper(): "1000/60" for name in RULES}


def test_two_real_server_processes_share_one_persistent_budget(tmp_path):
    url = f"sqlite:///{tmp_path / 'two.db'}"
    env = {**_open_env(), "AICOS_RL_OUTBOX_MISS": "5/300"}
    a, base_a = _spawn(url, env)
    b, base_b = _spawn(url, env)                                      # b queda en espera (no tiene el lease): sigue sirviendo lecturas, y comparte el presupuesto
    try:
        assert httpx.get(base_b + "/api/health").json()["active"] is False
        miss = lambda base, i: httpx.get(base + f"/api/public/clima-sur/outbox?token=tok_{i:024x}").status_code   # noqa: E731
        assert [miss(base_a, 0), miss(base_b, 1), miss(base_a, 2), miss(base_b, 3), miss(base_a, 4)] == [404] * 5
        assert [miss(base_a, 5), miss(base_b, 6)] == [429, 429]
        a.send_signal(signal.SIGKILL); a.wait()                       # el estado vive en la base: matar al proceso no «perdona» a nadie
        assert miss(base_b, 7) == 429
    finally:
        for x in (a, b):
            x.kill(); x.wait()


def test_a_standby_refuses_chat_before_the_limiter_and_spends_no_budget(tmp_path):
    url = f"sqlite:///{tmp_path / 'sb.db'}"
    env = {**_open_env(), "AICOS_RL_CHAT_SRC": "2/600"}
    a, base_a = _spawn(url, env)
    b, base_b = _spawn(url, env)
    try:
        assert httpx.get(base_b + "/api/health").json()["active"] is False
        send = lambda base, i: httpx.post(base + "/api/public/clima-sur/chat", json=body(i, prefix="sb")).status_code   # noqa: E731
        assert [send(base_b, i) for i in range(5)] == [503] * 5       # el gate de standby va primero: nunca 429, nunca consume
        assert [send(base_a, 10), send(base_a, 11), send(base_a, 12)] == [200, 200, 429]
    finally:
        for x in (a, b):
            x.kill(); x.wait()


def test_forwarded_for_rotation_against_a_real_server_started_as_documented(tmp_path):
    url = f"sqlite:///{tmp_path / 'xff.db'}"
    p, base = _spawn(url, {**_open_env(), "AICOS_RL_OUTBOX_MISS": "3/300"})
    try:
        codes = [httpx.get(base + f"/api/public/clima-sur/outbox?token=tok_{i:024x}", headers={"X-Forwarded-For": f"198.51.100.{i + 1}"}).status_code for i in range(8)]
        assert codes == [404] * 3 + [429] * 5
    finally:
        p.kill(); p.wait()


def test_every_launcher_of_this_repo_disables_uvicorn_proxy_headers():
    """uvicorn trusts 127.0.0.1 by default and rewrites request.client from X-Forwarded-For: a local reverse proxy that does not overwrite the header would let any visitor
    pick their own «address». Trust is configured ONLY through AICOS_TRUSTED_PROXIES, so every place that starts the server must say so."""
    launch = re.compile(r"uvicorn\.(run|Config|Server)\(|-m uvicorn app\.main:app|\"-m\", \"uvicorn\"")
    skip = {"node_modules", ".venv", ".git", "dist", "dist-v05", "__pycache__", "data", "test-results", "docs"}
    sites, missing = [], []
    for f in ROOT.rglob("*"):
        if f.is_file() and f.suffix in {".sh", ".ts", ".py", ".yml", ".yaml"} and not skip & set(f.relative_to(ROOT).parts) and f.name != Path(__file__).name:
            src = f.read_text(errors="ignore")
            if launch.search(src):
                sites.append(f.name)
                if "--no-proxy-headers" not in src and "proxy_headers=False" not in src:
                    missing.append(f.name)
    assert {"run_demo_comercial.sh", "run_v05_lab.sh", "playwright.config.ts"} <= set(sites), sites     # la búsqueda encuentra los lanzadores conocidos
    assert not missing, f"lanzan uvicorn sin desactivar proxy_headers: {missing}"


# ============================================================ 6. efectos, privacidad y retención del estado
def test_rejected_requests_leave_no_trace_anywhere(app, owner, monkeypatch):
    limits(monkeypatch, rl_chat_src="2/600", rl_outbox_miss="2/300", rl_confirm_src="2/600")
    rid, _, token = _pending_visit(app, owner)
    a = at(app, "198.51.100.120")
    [post_chat(a, i) for i in range(2)]
    [a.get(f"/api/public/clima-sur/outbox?token=tok_{i:024x}") for i in range(2)]
    [a.post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": "NOPE00"}) for _ in range(2)]
    before, rows = snapshot(app), rl_rows(app)
    rejected = [post_chat(a, 90 + i) for i in range(5)] + [a.get(f"/api/public/clima-sur/outbox?token=tok_{i:024x}") for i in range(5)] \
        + [a.post("/api/public/clima-sur/visits/confirm", json={"request_id": rid, "code": "NOPE00"}) for _ in range(5)]
    assert set(ok_codes(rejected)) == {429}
    assert snapshot(app) == before                                    # sin runs, solicitudes, visitas, auditoría ni eventos nuevos
    assert rl_rows(app) == rows                                       # y lo rechazado tampoco crece la propia tabla de límites


def test_the_limiter_stores_no_addresses_and_only_hashed_bounded_keys(app, tmp_path, monkeypatch):
    limits(monkeypatch, rl_chat_src="3/600")
    a = at(app, "203.0.113.77")
    [post_chat(a, i, headers={"X-Forwarded-For": "203.0.113.78"}) for i in range(5)]
    with app.state.eng.connect() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(rate_limits)"))]
        dump = c.execute(text("SELECT * FROM rate_limits")).fetchall()
    assert dump and not {"ip", "source", "address", "addr"} & set(cols)
    flat = " ".join(map(str, dump))
    assert "203.0.113" not in flat and "198.51" not in flat
    app.state.eng.dispose()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("t.db*"))
    assert b"203.0.113.77" not in raw and b"203.0.113.78" not in raw   # ni en claro en el archivo ni en el WAL


def test_expired_state_disappears_and_the_table_stays_small_under_a_rotating_flood(app, monkeypatch, clock):
    limits(monkeypatch, rl_chat_co="40/3600", rl_chat_src="5/600", rl_unknown_all="30/60", rl_unknown_src="30/60")
    for i in range(60):                                                # 60 «fuentes» distintas pegándole a la empresa: la tapa de la empresa frena a las últimas 20
        post_chat(at(app, f"198.51.100.{i + 1}"), i)
    assert rl_rows(app) <= 40 + 2                                      # fuentes aceptadas (≤ tapa) + cubo de empresa: nunca crece con lo rechazado
    clock.advance(3 * 3600)
    assert post_chat(at(app, "198.51.100.200"), 999).status_code == 200
    clock.advance(40)
    post_chat(at(app, "198.51.100.201"), 1000)
    assert rl_rows(app) <= 4                                           # lo vencido se purgó: no se acumula historial de visitantes


def test_unknown_slugs_and_unknown_public_paths_share_bounded_buckets(app, monkeypatch):
    limits(monkeypatch, rl_unknown_src="6/60", rl_unknown_all="12/60")
    a = at(app, "198.51.100.130")
    codes = [a.get(f"/api/public/no-existe-{i}/outbox?token=tok_x").status_code for i in range(10)]
    assert codes == [404] * 6 + [429] * 4
    assert post_chat(a, 1).status_code == 200                          # su presupuesto real contra empresas existentes es otro
    many = [at(app, f"198.51.100.{140 + i}").get(f"/api/public/otra-{i}/outbox?token=tok_x").status_code for i in range(20)]
    assert many.count(404) == 6 and many.count(429) == 14              # 12 en total para «empresas inexistentes»: 6 ya gastados arriba
    assert (rl_rows(app) or 0) <= 24


def test_denials_are_logged_for_alerting_once_per_window_and_without_addresses(app, monkeypatch, caplog):
    import logging
    limits(monkeypatch, rl_chat_src="2/600")
    with caplog.at_level(logging.WARNING, logger="aicos.ratelimit"):
        [post_chat(at(app, "203.0.113.99"), i) for i in range(40)]
    lines = [r.getMessage() for r in caplog.records if r.name == "aicos.ratelimit"]
    assert 1 <= len(lines) <= 2                                        # 38 rechazos, una sola línea de alerta por (tipo, empresa) y ventana
    assert "chat.src" in lines[0] and "clima-sur" in lines[0]
    assert not any("203.0.113" in x for x in lines)


def test_a_malformed_or_oversized_public_request_is_counted_and_never_reaches_the_engine(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="3/600")
    a = at(app, "198.51.100.150")
    codes = [a.post("/api/public/clima-sur/chat", json={"message": "x"}).status_code for _ in range(5)]
    assert codes == [422] * 3 + [429] * 2
    assert snapshot(app)["runs"] == 0


# ============================================================ 7. lo que ya existía sigue igual
def test_the_public_channel_keeps_its_csrf_exemption_and_never_answers_403(app, monkeypatch):
    limits(monkeypatch, rl_chat_src="3/600")
    a = at(app, "198.51.100.160")
    evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site", "X-CSRF-Token": "forged"}
    r = post_chat(a, 1, headers=evil)
    assert r.status_code == 200                                        # el canal público no usa cookie: no hay autoridad ambiente que falsificar (diseño de #10)
    assert ok_codes([post_chat(a, i, headers=evil) for i in range(2, 6)]) == [200, 200, 429, 429]      # el límite responde 429, nunca 403, y la exención de CSRF sigue en pie


def test_staff_endpoints_are_not_touched_by_the_public_budgets(app, owner, monkeypatch):
    limits(monkeypatch, rl_chat_src="1/600", rl_outbox_src="1/60", rl_confirm_src="1/600", rl_unknown_src="1/60", rl_unknown_all="1/60", rl_chat_co="1/600")
    assert ok_codes([owner.get("/api/dashboard") for _ in range(10)]) == [200] * 10
    assert owner.post("/api/assistant/chat", json={"message": "hola"}).status_code == 200
    assert at(app, "198.51.100.170").get("/api/health").json()["ok"] is True


def test_authenticated_writes_still_require_csrf_with_the_limiter_in_place(app, owner):
    r = owner.post("/api/plans", json={"text": "Revisa las solicitudes pendientes", "client_plan_id": "rl-csrf-1"}, headers={"x-csrf-token": "forged"})
    assert r.status_code == 403


# ============================================================ 8. configuración, copias y migración
def test_defaults_are_prudent_and_confirmation_is_the_strictest():
    def rate(rule):                                                    # peticiones por minuto sostenidas
        n, w = rule.split("/")
        return int(n) / (int(w) / 60)
    d = {k: getattr(settings, k) for k in RULES}
    assert rate(d["rl_confirm_src"]) < rate(d["rl_chat_src"]) <= 60 and rate(d["rl_confirm_target"]) <= rate(d["rl_confirm_src"])
    assert rate(d["rl_outbox_src"]) >= 50 and rate(d["rl_outbox_miss"]) <= 6                                # el sondeo de una pestaña (50/min) cabe; las lecturas fallidas, no
    assert rate(d["rl_chat_co"]) <= 600 and int(d["rl_confirm_target"].split("/")[0]) <= 10                 # tope duro de intentos sobre una misma solicitud


@pytest.mark.parametrize("bad", ["0/60", "abc", "5/0", "-3/60", "5", "5/60/1", "1e9/60"])
def test_an_invalid_rule_stops_the_server_at_startup_naming_the_variable(dbfile, monkeypatch, bad):
    limits(monkeypatch)
    monkeypatch.setattr(settings, "rl_confirm_src", bad, raising=False)
    with pytest.raises(ValueError, match="AICOS_RL_CONFIRM_SRC"):
        create_app(dbfile)


def test_the_limiter_has_no_test_mode_branch():
    src = Path(ratelimit.__file__).read_text()
    assert not re.search(r"sync_runs|pytest|PYTEST|TESTING|demo_mode|is_test", src)


def test_an_existing_database_gets_the_tables_added_and_keeps_its_data(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    first = create_app(url)
    first.state.orch.keeper.stop()
    with first.state.eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS rate_limits"))
        c.execute(text("DROP TABLE IF EXISTS rate_limit_meta"))
        before = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("companies", "users", "agents")}
    first.state.eng.dispose()
    second = create_app(url)                                           # la forma anterior a este PR: arranca, añade las tablas y no toca lo existente
    try:
        with second.state.eng.connect() as c:
            after = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("companies", "users", "agents")}
            tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert after == before and {"rate_limits", "rate_limit_meta"} <= tables
        assert post_chat(at(second, "198.51.100.180"), 1).status_code == 200
    finally:
        second.state.orch.keeper.stop()
        second.state.eng.dispose()


def test_a_restored_backup_never_carries_visitor_counters_or_the_hashing_key(app, tmp_path, monkeypatch):
    limits(monkeypatch, rl_chat_src="3/600")
    [post_chat(at(app, "198.51.100.190"), i) for i in range(5)]
    assert rl_rows(app) >= 1
    src = tmp_path / "t.db"
    m = backup.create(src, tmp_path / "bk")
    out = tmp_path / "restored.db"
    backup.restore(tmp_path / "bk" / m["file"], out)
    import sqlite3
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT count(*) FROM rate_limits").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM rate_limit_meta").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM companies").fetchone()[0] >= 2     # lo demás sí viaja
    finally:
        con.close()


def test_the_sign_in_throttle_follows_the_same_trusted_address_rules(app, monkeypatch):
    """Switching uvicorn's own proxy handling off must not blind the login throttle behind a proxy: it reads the address through the same trust rules."""
    limits(monkeypatch, trusted_proxies=("10.0.0.0/8",))
    bad = {"email": "dueno@climasur.test", "password": "incorrecta"}
    at(app, "10.0.0.5").post("/api/auth/login", json=bad, headers={"X-Forwarded-For": "9.9.9.9, 198.51.100.7, 10.0.0.9"})        # a trusted proxy: the client it saw
    at(app, "198.51.100.8").post("/api/auth/login", json=bad, headers={"X-Forwarded-For": "1.2.3.4"})                              # a stranger cannot choose its address
    with app.state.eng.connect() as c:
        seen = {r[0] for r in c.execute(text("SELECT ip FROM login_attempts"))}
    assert seen == {"198.51.100.7", "198.51.100.8"}


def test_a_limiter_that_breaks_fails_closed_and_leaves_no_work_behind(app, monkeypatch):
    """If the limiter cannot decide (database locked, bug), the public request is refused — never waved through."""
    limits(monkeypatch)
    monkeypatch.setattr(ratelimit.Limiter, "take", lambda self, buckets: (_ for _ in ()).throw(RuntimeError("base de datos bloqueada")))
    r = post_chat(at(app, "198.51.100.200"), 1)
    assert r.status_code == 503 and int(r.headers["Retry-After"]) >= 1 and snapshot(app)["runs"] == 0
    assert at(app, "198.51.100.200").get("/api/health").status_code == 200          # solo el canal público se ve afectado


def test_housekeeping_failures_never_fail_an_accepted_request(app, monkeypatch, clock):
    """The sweep of expired rows and the refund of a successful lookup are best effort: the request already spent its tokens and must be answered normally."""
    limits(monkeypatch, rl_outbox_miss="3/300")
    c = at(app, "198.51.100.210")
    token = chat(c, mid="rl-house-0001")["conversation_token"]                      # the first accepted request also runs the first sweep
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("disco lleno")
    monkeypatch.setattr(ratelimit.Limiter, "_purge", boom)
    monkeypatch.setattr(ratelimit.Limiter, "_refund", boom)
    clock.advance(60)                                                               # a sweep is due again
    assert post_chat(c, 1).status_code == 200 and len(calls) == 1                   # …it fails, and the chat is still accepted
    assert c.get(f"/api/public/clima-sur/outbox?token={token}").status_code == 200  # the refund fails, and the valid read is still a 200
    assert len(calls) == 2
