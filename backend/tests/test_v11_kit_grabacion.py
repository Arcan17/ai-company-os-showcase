"""V0.11 — kit de grabación (`qa/grabar_demo.sh`): sin descargas, verificación previa clara y salida ignorada por Git. Sin red, sin Ollama y sin navegador."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "qa" / "grabar_demo.sh"


def test_la_sintaxis_es_valida_y_es_ejecutable():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


def test_con_ia_sin_ollama_falla_con_un_mensaje_claro_antes_de_construir_o_grabar(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}
    env.update(AICOS_OLLAMA_URL="http://127.0.0.1:1", GRABACIONES_DIR=str(tmp_path / "g"), LC_ALL="C.UTF-8")
    p = subprocess.run(["bash", str(SCRIPT), "--ia"], capture_output=True, encoding="utf-8", errors="replace", timeout=30, env=env, cwd=ROOT)
    assert p.returncode == 1
    assert "No hay un Ollama respondiendo" in p.stderr or "Falta ffmpeg" in p.stderr      # (si el equipo no tiene ffmpeg, avisa eso primero)
    assert not (tmp_path / "g" / "raw").exists()                                           # no llegó a grabar nada


def test_no_usa_el_ffmpeg_descargable_de_playwright_y_la_salida_esta_ignorada():
    cfg = (ROOT / "frontend" / "playwright.record.config.ts").read_text()
    assert 'video: "off"' in cfg and "video: {" not in cfg                               # el video sale de los cuadros capturados + el ffmpeg del sistema
    spec = (ROOT / "frontend" / "record" / "record_demo.spec.ts").read_text()
    assert "Page.startScreencast" in spec and "RECORD_VIDEO" in spec
    assert "grabaciones/" in (ROOT / ".gitignore").read_text().splitlines()


def test_la_misma_prueba_corre_rapida_y_sin_video_en_el_ci():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "playwright.record.config.ts" in ci
