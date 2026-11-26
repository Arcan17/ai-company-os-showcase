"""V0.11 — salvaguardas ESTÁTICAS de la unidad Quadlet de la demo PRIVADA con IA local (`deploy/hp/aicos-demo-ia.container`). No instala ni ejecuta nada: si alguien la debilita
(red del anfitrión, puerto abierto, demo pública con un modelo, secretos, mezcla con la unidad pública) esta prueba falla antes de que llegue al HP."""
import re
from pathlib import Path

DIR = Path(__file__).resolve().parents[2] / "deploy" / "hp"
UNIT = (DIR / "aicos-demo-ia.container").read_text()
PUBLIC = (DIR / "aicos-demo.container").read_text()


def lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def values(key, text=UNIT):
    return [ln.split("=", 1)[1] for ln in lines(text) if ln.startswith(key + "=")]


def test_imagen_con_etiqueta_explicita_y_local():
    (img,) = values("Image")
    assert img.startswith("localhost/") and ":" in img and not img.endswith(":latest")


def test_solo_loopback_y_otro_puerto_que_la_demo_publica():
    (port,) = values("PublishPort")
    (pub,) = values("PublishPort", PUBLIC)
    assert port.startswith("127.0.0.1:") and port.split(":")[1] != pub.split(":")[1]


def test_es_otra_instancia_no_comparte_nombre_ni_volumen_con_la_publica():
    assert values("ContainerName") != values("ContainerName", PUBLIC) and values("Volume") != values("Volume", PUBLIC)
    for v in values("Volume"):
        assert not v.startswith(("/", "~", ".")), v                      # solo volúmenes con nombre, jamás rutas del anfitrión


def test_contenedor_de_solo_lectura_sin_capacidades_ni_privilegios_y_con_limites():
    assert values("ReadOnly") == ["true"] and values("DropCapability") == ["ALL"] and values("NoNewPrivileges") == ["true"] and values("Tmpfs") == ["/tmp"]
    args = " ".join(values("PodmanArgs"))
    assert re.search(r"--memory=\d+m", args) and re.search(r"--memory-swap=\d+m", args) and "--pids-limit=" in args and "--cpus=" in args
    assert "--privileged" not in UNIT


def test_la_red_es_lo_minimo_para_llegar_al_ollama_y_nunca_la_del_anfitrion():
    assert values("Network") == ["slirp4netns:allow_host_loopback=true"] and "--network=host" not in UNIT and "host" not in " ".join(v for v in values("Network") if v.startswith("host"))


def test_usa_ollama_con_una_url_fija_y_no_es_la_demo_publica():
    env = dict(v.split("=", 1) for v in values("Environment"))
    assert env["AICOS_LLM_PROVIDER"] == "ollama" and env["AICOS_OLLAMA_URL"] == "http://10.0.2.2:11434"
    assert "AICOS_PUBLIC_DEMO" not in env                                # la demo pública se niega a usar un modelo: esta es una instancia privada y distinta
    assert env["AICOS_ALLOW_COMPANY_CREATION"] == "0" and env["AICOS_DEMO_EXAMPLE"] == "1" and int(env["AICOS_LLM_PLAN_TIMEOUT"]) >= 60


def test_no_lleva_secretos():
    assert not re.search(r"(?i)(secret|token|password|api_key)\s*=", UNIT)
