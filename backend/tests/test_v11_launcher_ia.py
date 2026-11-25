"""V0.11 — `run_demo_ia.sh`: la demo local con IA gratis (Ollama). Verificación previa clara, solo local y sin tocar la demo pública. Sin red ni Ollama real."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "run_demo_ia.sh"


def run(**env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}
    e.update(env)
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, encoding="utf-8", errors="replace", timeout=30, env=e, cwd=ROOT)


def test_la_sintaxis_es_valida_y_es_ejecutable():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


def test_sin_ollama_falla_con_un_mensaje_claro_y_no_arranca_nada():
    p = run(AICOS_OLLAMA_URL="http://127.0.0.1:1")
    assert p.returncode == 1 and "No hay un Ollama respondiendo" in p.stderr and "AICOS_OLLAMA_URL" in p.stderr
    assert "AI Company OS →" not in p.stdout                                  # run_demo.sh no llegó a ejecutarse


def test_es_solo_local_y_no_activa_la_demo_publica():
    s = SCRIPT.read_text()
    assert "AICOS_PUBLIC_DEMO" not in s and "AICOS_LLM_PROVIDER=ollama" in s and "exec ./run_demo.sh" in s
    assert 'AICOS_LLM_PLAN_TIMEOUT="${AICOS_LLM_PLAN_TIMEOUT:-90}"' in s       # plazo largo para equipos sin GPU


def test_el_modelo_inexistente_se_informa_antes_de_arrancar(tmp_path):
    import http.server, json, threading
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"models": [{"name": "otro:1b"}]}).encode())
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = run(AICOS_OLLAMA_URL=f"http://127.0.0.1:{srv.server_port}", AICOS_OLLAMA_MODEL="no-existe:7b")
        assert p.returncode == 1 and "no está instalado" in p.stderr and "ollama pull no-existe:7b" in p.stderr
    finally:
        srv.shutdown()
