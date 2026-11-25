"""V0.9 (spec 004, fase D) — salvaguardas ESTÁTICAS de la unidad Quadlet del HP (`deploy/hp/aicos-demo.container`): si alguien la debilita (imagen `latest`, puerto abierto a toda la red, contenedor escribible, sin límites,
red del anfitrión, montaje de carpetas personales o secretos) esta prueba falla antes de que llegue al HP. No instala ni ejecuta nada."""
import re
from pathlib import Path

UNIT = (Path(__file__).resolve().parents[2] / "deploy" / "hp" / "aicos-demo.container").read_text()
LINES = [ln.strip() for ln in UNIT.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def values(key):
    return [ln.split("=", 1)[1] for ln in LINES if ln.startswith(key + "=")]


def test_imagen_con_etiqueta_explicita_y_local():
    (img,) = values("Image")
    assert img.startswith("localhost/") and ":" in img and not img.endswith(":latest")


def test_el_puerto_solo_se_publica_en_loopback():
    ports = values("PublishPort")
    assert ports and all(p.startswith("127.0.0.1:") for p in ports), ports


def test_contenedor_de_solo_lectura_sin_capacidades_ni_privilegios():
    assert values("ReadOnly") == ["true"] and values("DropCapability") == ["ALL"] and values("NoNewPrivileges") == ["true"]
    assert values("Tmpfs") == ["/tmp"]


def test_tiene_limites_de_memoria_sin_swap_procesos_y_cpu():
    args = " ".join(values("PodmanArgs"))
    assert re.search(r"--memory=\d+m", args) and re.search(r"--memory-swap=\d+m", args) and "--pids-limit=" in args and "--cpus=" in args


def test_no_usa_red_del_anfitrion_ni_monta_carpetas_personales_ni_lleva_secretos():
    assert not values("Network") and "--network=host" not in UNIT and "--privileged" not in UNIT
    for v in values("Volume"):
        assert not v.startswith(("/", "~", ".")), v          # solo volúmenes con nombre, jamás rutas del anfitrión
    assert not re.search(r"(?i)(secret|token|password|api_key)\s*=", UNIT)


def test_es_la_demo_publica_con_topes_conservadores_y_sin_creacion_de_empresas():
    env = dict(v.split("=", 1) for v in values("Environment"))
    assert env["AICOS_PUBLIC_DEMO"] == "1" and env["AICOS_ALLOW_COMPANY_CREATION"] == "0"
    assert 1 <= int(env["AICOS_PUBLIC_MAX_VISITORS"]) <= 10
