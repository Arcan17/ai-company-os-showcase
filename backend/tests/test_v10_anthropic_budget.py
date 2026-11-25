"""V0.10 (spec 005, fase 2) — control de gasto y adaptador del modelo de pago. SIN red y SIN clave real: el cliente de la API es simulado.
Lo que se demuestra: el tope se verifica ANTES de llamar (atómico, día y mes), el costo se calcula con los tokens reales, la clave no se filtra, y cualquier falla cae a reglas rotulada."""
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app import clock
from app.config import settings
from app.db import make_engine, md
from app.providers import budget as budget_mod
from app.providers.anthropic_provider import AnthropicProvider, mask_key
from app.providers.base import PlanProposal, ProviderConfigError
from app.providers.budget import Budget, resolve_prices
from app.providers.factory import get_provider

from .test_plans import E1_NAMED, marta, n, post_plan  # noqa: F401
from .test_v10_planner_hostile import fallback_of

KEY = "sk" + "-ant-api03-ABCDEFGH-secreto-NO-DEBE-SALIR-1234"      # armada por partes: el saneamiento de la vitrina busca formas de clave


def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'b.db'}")
    md.create_all(eng)
    return eng


def budget(tmp_path, day=1.0, month=10.0, pin=4.0, pout=20.0):
    return Budget(engine(tmp_path), "anthropic", "claude-opus-5-5", pin, pout, day, month)


class Reloj:
    def __init__(self, y=2026, m=11, d=25):
        self.t = datetime(y, m, d, 12, 0, tzinfo=timezone.utc)

    def utcnow(self):
        return self.t


@pytest.fixture
def reloj(monkeypatch):
    r = Reloj()
    monkeypatch.setattr(clock, "utcnow", r.utcnow)
    return r


def respuesta(d=None, tin=500, tout=120, stop="end_turn"):
    d = d if d is not None else {"review_pending": False, "admin_verify_materials": True, "technician_confirm": True, "notify_owner": True, "request_id": "NINGUNO", "unsupported_actions": []}
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(d))], usage=SimpleNamespace(input_tokens=tin, output_tokens=tout), stop_reason=stop)


class Cliente:
    def __init__(self, comportamiento):
        self.llamadas = []
        self.messages = SimpleNamespace(create=self._create)
        self.comportamiento = comportamiento

    def _create(self, **kw):
        self.llamadas.append(kw)
        r = self.comportamiento(kw)
        if isinstance(r, Exception):
            raise r
        return r


def proveedor(tmp_path, comportamiento, **kw):
    b = kw.pop("budget", None) or budget(tmp_path)
    c = Cliente(comportamiento)
    return AnthropicProvider(KEY, "claude-opus-5-5", b, client=c), c, b


# ── T005: gasto ──
def test_el_peor_caso_y_el_costo_real_se_calculan_con_los_precios(tmp_path):
    b = budget(tmp_path, pin=4.0, pout=20.0)
    assert b.cost(1_000_000, 0) == pytest.approx(4.0) and b.cost(0, 1_000_000) == pytest.approx(20.0)
    assert b.worst_case(2000, 1024) == pytest.approx(1000 * 4 / 1e6 + 1024 * 20 / 1e6)          # entrada estimada por exceso (2 car./token) + toda la salida


def test_el_tope_diario_se_verifica_antes_de_llamar_y_no_se_pasa(tmp_path, reloj):
    b = budget(tmp_path, day=0.05, month=10.0)
    ids = []
    for _ in range(10):
        rid = b.reserve(0.02)
        if rid is None:
            break
        ids.append(rid)
    assert len(ids) == 2 and b.spent()["day"] == pytest.approx(0.04) and b.reserve(0.02) is None      # 0.04 + 0.02 > 0.05: no cabe


def test_el_tope_mensual_manda_aunque_el_diario_alcance(tmp_path, reloj):
    b = budget(tmp_path, day=1.0, month=1.5)
    for dia in (25, 26):
        reloj.t = datetime(2026, 11, dia, 12, tzinfo=timezone.utc)
        assert b.reserve(0.7) is not None
    reloj.t = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    assert b.reserve(0.7) is None                                      # 1.4 + 0.7 > 1.5 (mes) aunque ese día no se haya gastado nada
    reloj.t = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
    assert b.reserve(0.7) is not None                                  # mes nuevo: el contador del mes empieza de cero


def test_las_reservas_concurrentes_no_pasan_el_tope_juntas(tmp_path, reloj):
    b = budget(tmp_path, day=1.0, month=10.0)
    res, lock = [], threading.Lock()

    def go():
        r = b.reserve(0.3)
        with lock:
            res.append(r)
    ths = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ths]; [t.join() for t in ths]
    assert sum(1 for r in res if r) == 3 and b.spent()["day"] <= 1.0 + 1e-9


def test_al_terminar_la_reserva_se_cambia_por_el_costo_real_y_un_fallo_libera_el_cupo(tmp_path, reloj):
    b = budget(tmp_path, day=1.0)
    rid = b.reserve(0.9)
    assert b.reserve(0.2) is None                                       # lo reservado cuenta
    assert b.settle(rid, 0, 0, "error") == 0.0 and b.reserve(0.2) is not None     # la llamada falló sin gastar tokens: el cupo vuelve
    rid2 = b.reserve(0.5)
    assert b.settle(rid2, 100_000, 1_000, "ok") == pytest.approx(100_000 * 4 / 1e6 + 1_000 * 20 / 1e6)


def test_topes_invalidos_se_rechazan(tmp_path):
    for dia, mes in ((0, 10), (1, 0), (5, 1), (-1, 10)):
        with pytest.raises(ProviderConfigError):
            Budget(engine(tmp_path), "anthropic", "m", 4, 20, dia, mes)


def test_precios_conocidos_configurados_y_modelo_desconocido(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_price_in", ""); monkeypatch.setattr(settings, "anthropic_price_out", "")
    assert resolve_prices("claude-opus-5-5") == (4.0, 20.0)
    with pytest.raises(ProviderConfigError):
        resolve_prices("claude-modelo-nuevo-9")                         # sin precio no se puede acotar el gasto
    monkeypatch.setattr(settings, "anthropic_price_in", "3"); monkeypatch.setattr(settings, "anthropic_price_out", "15")
    assert resolve_prices("claude-modelo-nuevo-9") == (3.0, 15.0)
    monkeypatch.setattr(settings, "anthropic_price_in", "x")
    with pytest.raises(ProviderConfigError):
        resolve_prices("claude-opus-5-5")


# ── T006: el adaptador ──
def test_una_llamada_sana_devuelve_la_propuesta_y_cobra_los_tokens_reales(tmp_path, reloj):
    p, c, b = proveedor(tmp_path, lambda kw: respuesta(tin=500, tout=120))
    prop = p.propose_plan("Pide a Javiera que confirme los materiales", [{"id": "req_aaaaaaaaaaaa", "customer": "Marta", "category": "mantencion"}])
    assert isinstance(prop, PlanProposal) and prop.source == "llm:claude-opus-5-5" and prop.admin_verify_materials and prop.request_id is None
    assert b.spent()["day"] == pytest.approx(500 * 4 / 1e6 + 120 * 20 / 1e6)
    kw = c.llamadas[0]
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 1024 and kw["output_config"]["effort"] == "low"
    assert "temperature" not in kw and "tools" not in kw and "thinking" not in kw          # parámetros que este modelo rechaza o que no queremos
    assert kw["output_config"]["format"]["type"] == "json_schema"


def test_el_modelo_solo_recibe_id_cliente_y_categoria(tmp_path, reloj):
    p, c, _ = proveedor(tmp_path, lambda kw: respuesta())
    p.propose_plan("Revisa lo pendiente", [{"id": "req_aaaaaaaaaaaa", "customer": "Marta", "category": "mantencion", "description": "SECRETO-DEL-CLIENTE"}])
    assert "SECRETO-DEL-CLIENTE" not in json.dumps(c.llamadas[0]["messages"]) + c.llamadas[0]["system"]


def test_sin_cupo_no_se_llama_a_la_api(tmp_path, reloj):
    b = budget(tmp_path, day=0.001, month=1.0)                          # menos que el peor caso de UNA llamada
    p, c, _ = proveedor(tmp_path, lambda kw: respuesta(), budget=b)
    assert p.propose_plan("Revisa lo pendiente", []) is None
    assert c.llamadas == [] and p.last_code == "presupuesto"


@pytest.mark.parametrize("fallo", [
    anthropic.APITimeoutError(request=httpx2.Request("POST", "http://x")),
    anthropic.APIConnectionError(request=httpx2.Request("POST", "http://x")),
    ValueError("boom"),
])
def test_un_error_de_red_o_de_la_api_no_lanza_y_libera_el_cupo(tmp_path, reloj, fallo):
    p, c, b = proveedor(tmp_path, lambda kw: fallo)
    assert p.propose_plan("Revisa lo pendiente", []) is None and p.last_error
    assert b.spent()["day"] == 0.0                                     # nada se gastó


def test_rechazo_o_respuesta_cortada_o_invalida_devuelven_none_y_cobran_lo_gastado(tmp_path, reloj):
    for comportamiento in (lambda kw: respuesta(stop="refusal"), lambda kw: respuesta(stop="max_tokens"), lambda kw: SimpleNamespace(
            content=[SimpleNamespace(type="text", text="esto no es JSON")], usage=SimpleNamespace(input_tokens=400, output_tokens=50), stop_reason="end_turn")):
        p, c, b = proveedor(tmp_path, comportamiento, budget=budget(tmp_path))
        assert p.propose_plan("Revisa lo pendiente", []) is None and p.last_error
        assert b.spent()["day"] > 0                                    # los tokens que sí se usaron se cobran


def test_un_id_de_solicitud_inventado_por_el_modelo_se_descarta(tmp_path, reloj):
    d = {"review_pending": True, "admin_verify_materials": False, "technician_confirm": False, "notify_owner": False, "request_id": "req_ffffffffffff", "unsupported_actions": ["comprar", "inventada"]}
    p, c, _ = proveedor(tmp_path, lambda kw: respuesta(d))
    prop = p.propose_plan("Revisa lo pendiente", [{"id": "req_aaaaaaaaaaaa", "customer": "M", "category": "x"}])
    assert prop.request_id is None and prop.unsupported == ["comprar"]


# ── T007: la clave ──
def test_la_clave_no_aparece_en_repr_ni_en_errores_ni_en_registros(tmp_path, reloj, caplog):
    p, c, b = proveedor(tmp_path, lambda kw: anthropic.APIConnectionError(request=httpx2.Request("POST", "http://x")))
    p.propose_plan("Revisa lo pendiente", [])
    assert mask_key(KEY) == "****1234"
    blob = repr(p) + str(p.last_error) + caplog.text
    assert KEY not in blob and "secreto" not in blob


def test_sin_clave_no_se_activa_y_el_modelo_de_pago_exige_base_de_datos(monkeypatch, tmp_path):
    with pytest.raises(ProviderConfigError):
        AnthropicProvider("", "claude-opus-5-5", budget(tmp_path))
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    with pytest.raises(ProviderConfigError):
        get_provider(None)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(ProviderConfigError):
        get_provider(engine(tmp_path))                                   # sin clave: se niega a arrancar


def test_la_fabrica_arma_el_modelo_de_pago_con_los_topes_configurados(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "llm_provider", "anthropic"); monkeypatch.setattr(settings, "anthropic_api_key", KEY)
    monkeypatch.setattr(settings, "anthropic_model", "claude-haiku-5-5"); monkeypatch.setattr(settings, "anthropic_price_in", ""); monkeypatch.setattr(settings, "anthropic_price_out", "")
    monkeypatch.setattr(settings, "llm_budget_day_usd", 0.5); monkeypatch.setattr(settings, "llm_budget_month_usd", 3.0)
    p = get_provider(engine(tmp_path))
    assert p.name == "anthropic:claude-haiku-5-5" and (p.budget.pin, p.budget.pout) == (0.10, 0.50) and (p.budget.day_cap, p.budget.month_cap) == (0.5, 3.0) and KEY not in repr(p)


# ── integración con el planificador ──
def test_el_planificador_usa_el_modelo_de_pago_y_rotula_el_plan(app, owner, marta, tmp_path):
    p, c, b = proveedor(tmp_path, lambda kw: respuesta({"review_pending": False, "admin_verify_materials": True, "technician_confirm": True, "notify_owner": True, "request_id": "NINGUNO", "unsupported_actions": []}))
    app.state.orch.provider = p
    pl = post_plan(owner, "Pide a Javiera que confirme los materiales del mantenimiento de Marta y avísame si falta algo antes de agendar")
    assert pl["source"] == "llm:claude-opus-5-5" and pl["fallback"] is None and b.spent()["day"] > 0
    assert n(app, "tasks", "kind='plan_step'") == 0                      # sigue exigiendo confirmación humana


def test_con_el_tope_alcanzado_el_plan_sale_por_reglas_y_dice_por_que(app, owner, marta, tmp_path):
    p, c, _ = proveedor(tmp_path, lambda kw: respuesta(), budget=budget(tmp_path, day=0.001, month=1.0))
    app.state.orch.provider = p
    pl = post_plan(owner, E1_NAMED)
    assert pl["source"] == "deterministic" and pl["fallback"] == "presupuesto" and "tope de gasto" in pl["fallback_label"] and fallback_of(owner, pl["id"]) == "presupuesto" and c.llamadas == []
