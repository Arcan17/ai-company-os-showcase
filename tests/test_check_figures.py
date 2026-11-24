"""`tools/check_figures.py`: las cifras de los textos deben coincidir con el sitio; una cifra distinta o un texto que deja de citarla hacen fallar la comprobación."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_figures as cf  # noqa: E402

FACTS = cf.site_facts(ROOT)


def test_los_textos_actuales_coinciden_con_el_sitio():
    assert cf.check(ROOT) == []


def test_una_cifra_distinta_se_detecta():
    t = (ROOT / "README.md").read_text().replace(f"**{FACTS['mutaciones']}** (29 ", f"**{FACTS['mutaciones'] + 1}** (29 ")
    assert any("«mutaciones» dice" in e for e in cf.check(ROOT, texts={"README.md": t}))


def test_el_total_de_navegador_debe_sumar_la_cifra_del_sitio():
    t = (ROOT / "docs/CASO_DE_ESTUDIO.md").read_text().replace("| 18 + 4 + 2 + 2 |", "| 18 + 4 + 2 + 1 |")
    assert any("«e2e» dice" in e for e in cf.check(ROOT, texts={"docs/CASO_DE_ESTUDIO.md": t}))


def test_un_texto_que_deja_de_citar_la_cifra_se_detecta():
    t = (ROOT / "docs/VIDEO_GUION.md").read_text().replace(f"`{FACTS['tests']} passed`", "`todo pasa`")
    assert any("ya no cita" in e for e in cf.check(ROOT, texts={"docs/VIDEO_GUION.md": t}))
