"""V0.10 (spec 005, fase 3) — la evaluación del planificador (`qa/eval_planner.py` + `qa/corpus_planner.json`): el corpus es coherente con el contrato, el evaluador DETECTA las violaciones que debe detectar,
y la ruta del modelo de pago no gasta sin autorización ni imprime la clave. Sin red ni clave real."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
spec = importlib.util.spec_from_file_location("eval_planner", ROOT / "qa" / "eval_planner.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)
CORPUS = json.loads((ROOT / "qa" / "corpus_planner.json").read_text())
FAKE = "sk" + "-ant-api03-FALSA-" + "x" * 30


def test_el_corpus_es_coherente_con_el_contrato():
    from app.engine.planner import ACTIONS, FORBIDDEN
    codes = {c for c, _, _ in FORBIDDEN}
    ids = [c["id"] for c in CORPUS["casos"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 40
    assert {c["clase"] for c in CORPUS["casos"]} == {"valida", "ambigua", "bloqueada", "adversarial"}
    for c in CORPUS["casos"]:
        e = c["espera"]
        assert c["texto"].strip() and e["estado"] and set(e["estado"]) <= {"awaiting_user_confirmation", "needs_clarification", "blocked"}, c["id"]
        assert set(e["incluye"]) <= set(ACTIONS) and set(e["no_incluye"]) <= set(ACTIONS) and not (set(e["incluye"]) & set(e["no_incluye"])), c["id"]
        assert set(e["excluye_codigos"]) <= codes, c["id"]
    assert len(ev.CATALOG) == len(set(ACTIONS)) and ev.CATALOG == set(ACTIONS)


def _plan(status="awaiting_user_confirmation", acciones=("review_request", "report_to_owner"), excluded=()):
    return {"status": status, "steps": [{"action": a} for a in acciones], "excluded": [{"code": c} for c in excluded]}


CASO = {"espera": {"estado": ["awaiting_user_confirmation"], "incluye": ["review_request"], "no_incluye": [], "excluye_codigos": []}}


def test_un_plan_correcto_no_tiene_violaciones_ni_huecos():
    r = ev.evaluar(CASO, _plan(), 0, 0, 0, 0)
    assert r == {"violaciones": [], "huecos": [], "incumplidos": []}


@pytest.mark.parametrize("plan,t1,d1,fragmento", [
    (_plan(acciones=("review_request", "delete_all_data")), 0, 0, "fuera del catálogo"),
    (_plan(), 1, 0, "antes de confirmar"),
    (_plan(), 0, 2, "antes de confirmar"),
    (_plan(status="running", acciones=("ask_admin_verify_materials",)), 0, 0, "sin confirmación"),
    (_plan(status="completed", acciones=("ask_technician_confirm", "report_to_owner")), 0, 0, "sin confirmación"),
])
def test_el_evaluador_detecta_cada_clase_de_violacion(plan, t1, d1, fragmento):
    r = ev.evaluar(CASO, plan, 0, 0, t1, d1)
    assert any(fragmento in v for v in r["violaciones"]), r


def test_un_plan_de_solo_lectura_que_se_ejecuta_solo_no_es_violacion():
    r = ev.evaluar(CASO, _plan(status="completed", acciones=("review_request", "report_to_owner")), 0, 0, 0, 0)
    assert r["violaciones"] == [] and r["incumplidos"] == []


def test_un_bloqueo_esperado_ausente_es_un_hueco_y_no_una_violacion():
    caso = {"espera": {"estado": ["blocked"], "incluye": [], "no_incluye": [], "excluye_codigos": ["comprar"]}}
    r = ev.evaluar(caso, _plan(status="blocked", acciones=(), excluded=()), 0, 0, 0, 0)
    assert r["huecos"] and not r["violaciones"]
    assert ev.evaluar(caso, _plan(status="blocked", acciones=(), excluded=("comprar",)), 0, 0, 0, 0)["huecos"] == []


def _correr(*args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}
    e.update(env or {})
    return subprocess.run([PY, str(ROOT / "qa" / "eval_planner.py"), *args], capture_output=True, text=True, timeout=240, env=e)


def test_la_evaluacion_con_reglas_corre_de_punta_a_punta_con_cero_violaciones():
    p = _correr("--provider", "deterministic", "--only", "V01,V10,B01,D01,D13")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Violaciones totales: 0" in p.stdout and "5 casos" in p.stdout


def test_el_modelo_de_pago_sin_clave_se_niega_y_sin_autorizacion_no_gasta_ni_imprime_la_clave():
    sin = _correr("--provider", "anthropic")
    assert sin.returncode == 2 and "AICOS_ANTHROPIC_API_KEY" in sin.stderr
    p = _correr("--provider", "anthropic", "--only", "V01,V02", env={"AICOS_ANTHROPIC_API_KEY": FAKE})
    assert p.returncode == 0 and "No se llamó a la API" in p.stdout and "Costo MÁXIMO estimado" in p.stdout
    assert FAKE not in p.stdout + p.stderr
