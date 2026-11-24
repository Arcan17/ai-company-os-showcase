"""V0.9 (spec 004, fase A) — salvaguardas ESTÁTICAS de los archivos del contenedor: si alguien las debilita sin querer (usuario root, imagen sin versión, puerto abierto a toda la red, sistema de archivos escribible), esta prueba falla
antes de que lo vea el CI del contenedor. La prueba de punta a punta es `qa/smoke_container.sh`."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CF = (ROOT / "Containerfile").read_text()
COMPOSE = (ROOT / "compose.yaml").read_text()
START = (ROOT / "start.sh").read_text()


def test_la_imagen_corre_sin_privilegios_y_con_bases_fijadas():
    assert re.search(r"^USER 10001$", CF, re.M)
    froms = re.findall(r"^FROM (\S+)", CF, re.M)
    assert froms and all(":" in f and not f.endswith(":latest") for f in froms), froms


def test_compose_solo_escucha_en_loopback_por_defecto_y_endurece_el_contenedor():
    assert '"${AICOS_BIND:-127.0.0.1}:${PORT:-8731}:8731"' in COMPOSE
    for needle in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "mem_limit:"):
        assert needle in COMPOSE, needle


def test_la_demo_del_contenedor_no_permite_crear_empresas_ni_usa_modelos_externos():
    assert "AICOS_ALLOW_COMPANY_CREATION=0" in CF.replace(" \\\n    ", " ").replace("\\\n", "")
    assert "AICOS_LLM_PROVIDER=deterministic" in CF.replace(" \\\n    ", " ")


def test_start_sh_es_un_comando_y_borrar_los_datos_es_explicito():
    assert "set -euo pipefail" in START and "reset)" in START and "down -v" in START
    assert re.search(r"^\s*up\) ;;", START, re.M)       # por defecto solo arranca; jamás borra
