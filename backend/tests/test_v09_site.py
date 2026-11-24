"""V0.9 (spec 004, fase C) — el sitio de portafolio: la comprobación automática (`site/check_site.py`) debe PASAR con el sitio actual y FALLAR ante cada categoría de problema.
Cifras = fuente (FR-009); sin precios, correos, rutas locales, claves ni nombres del flujo interno (FR-008/010)."""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "site"))
import build_site  # noqa: E402
import check_site  # noqa: E402
import facts as F  # noqa: E402

PAGE = (ROOT / "site" / "index.html").read_text()
CAT = json.loads((ROOT / "site" / "catalog.json").read_text())


@pytest.fixture(scope="module")
def facts():
    return F.compute()


def test_el_sitio_actual_coincide_con_sus_fuentes(facts):
    assert check_site.check(facts=facts) == []
    assert PAGE == build_site.build(), "site/index.html no está al día: ejecuta `python site/build_site.py`"


def test_una_cifra_distinta_de_la_fuente_se_detecta(facts):
    bad = PAGE.replace(f'data-fact="tests">{facts["tests"]}<', f'data-fact="tests">{facts["tests"] + 1}<')
    assert any("cifra «tests»" in e for e in check_site.check(page=bad, facts=facts))


def test_una_cifra_ausente_se_detecta(facts):
    bad = PAGE.replace('data-fact="mutaciones"', 'data-fact-x="mutaciones"')
    assert any("«mutaciones» ausente" in e for e in check_site.check(page=bad, facts=facts))


# Los casos plantados se arman por partes: el archivo no contiene literales que el saneamiento de la vitrina busca (y se exporta sin reescrituras).
@pytest.mark.parametrize("plant,needle", [
    ("Desde $450.000 al mes", "precio"), ("Cuesta 2 UF", "precio"), ("USD 300", "precio"),
    ("escríbeme a alguien" + "@gmail" + ".com", "correo"), ("ver /Us" + "ers/alguien/Documentos/x", "ruta local"),
    ("hecho con Claude " + "Code", "nombre del flujo interno"), ("sk-" + "a" * 30, "clave"),
])
def test_cada_categoria_prohibida_se_detecta(facts, plant, needle):
    bad = PAGE.replace("<footer>", f"<footer>{plant} ")
    assert any(needle in e for e in check_site.check(page=bad, facts=facts)), plant


def test_un_modulo_sin_exclusiones_o_con_evidencia_inexistente_se_detecta(facts):
    c = copy.deepcopy(CAT)
    c["modulos"][0]["no_incluye"] = []
    assert any("NO incluye" in e for e in check_site.check(catalog=c, facts=facts))
    c = copy.deepcopy(CAT)
    c["modulos"][1]["evidencia"].append("backend/tests/no_existe.py")
    assert any("no existe" in e for e in check_site.check(catalog=c, facts=facts))


def test_la_pagina_es_accesible_y_sin_dependencias_externas():
    assert '<html lang="es">' in PAGE and "<title>" in PAGE and 'name="viewport"' in PAGE and 'class="skip"' in PAGE
    assert "<script" not in PAGE and "http://" not in PAGE.replace("http://127.0.0.1:8731", "")
    assert PAGE.count("<h1") == 1
