"""Cierre de transparencia (spec 005): el plan DICE qué partes de la instrucción no cubre. Antes, una orden fuera del vocabulario del escaneo («Devuélvele el dinero…», «Cambia el stock…»)
no se ejecutaba (catálogo cerrado) pero el plan no avisaba. Ahora cada parte sin paso ni etiqueta aparece en `ignored`. Es informativo: nunca cambia pasos ni estado."""
import pytest

from app.engine.planner import unmapped_clauses
from app.providers.base import PlanProposal

from .test_plans import E1, E1_NAMED, marta, n, post_plan  # noqa: F401  (marta es un fixture)

NADA = PlanProposal(False, False, False, False, None, [], "deterministic")
LEE = PlanProposal(True, False, False, False, None, [], "deterministic")
ADMIN = PlanProposal(False, True, False, True, None, [], "deterministic")


@pytest.mark.parametrize("texto", ["Devuélvele el dinero a Pedro por la mantención", "Cóbrale a Pedro lo de la última visita", "Adquiere los repuestos que faltan para la visita de Marta",
                                   "Ordena 5 filtros nuevos al proveedor habitual"])
def test_una_orden_sin_equivalente_en_el_catalogo_se_avisa(texto):
    assert unmapped_clauses(texto, NADA, [], [], []) == [texto]


def test_lo_que_tiene_paso_no_se_avisa():
    assert unmapped_clauses("Pide a administración que verifique los materiales de la visita de Marta y avísame si falta algo", ADMIN, [], [], []) == []
    assert unmapped_clauses("Revisa las solicitudes pendientes, por favor", LEE, [], [], []) == []


def test_lo_prohibido_ya_etiquetado_no_se_repite_como_ignorado():
    assert unmapped_clauses("Compra los repuestos y revisa lo pendiente", LEE, [], [], [{"code": "comprar"}]) == []


def test_un_texto_repetido_se_avisa_una_vez_y_hay_tope():
    assert unmapped_clauses("Haz algo raro. Haz algo raro. Haz algo raro", LEE, [], [], []) == ["Haz algo raro"]
    muchas = ". ".join(f"Frase inventada número {x}" for x in range(20))
    assert len(unmapped_clauses(muchas, LEE, [], [], [])) == 5


def test_el_plan_valido_no_trae_avisos(app, owner, marta):
    pl = post_plan(owner, E1_NAMED)
    assert pl["status"] == "awaiting_user_confirmation" and pl["ignored"] == []


def test_un_plan_con_pasos_avisa_de_la_parte_que_no_hara(app, owner, marta):
    pl = post_plan(owner, E1_NAMED + ". Cambia el precio del filtro a mil pesos")
    assert pl["steps"] and pl["ignored"] == ["Cambia el precio del filtro a mil pesos"]
    assert n(app, "tasks", "kind='plan_step'") == 0                      # informativo: nada se ejecuta ni se crea


def test_un_plan_bloqueado_tambien_avisa(app, owner, marta):
    pl = post_plan(owner, "Devuélvele el dinero a Marta por la mantención")
    assert pl["status"] == "blocked" and pl["ignored"] == ["Devuélvele el dinero a Marta por la mantención"]


def test_el_aviso_sobrevive_a_recargar_el_plan(app, owner, marta):
    pl = post_plan(owner, E1 + ". Cóbrale a Marta lo de la última visita")
    otra = owner.get(f"/api/plans/{pl['id']}").json()
    otra = otra.get("plan", otra)
    assert otra["ignored"] == pl["ignored"] != []
