"""V0.9 (spec 004, fase D) — el apagado programado de la demo pública del servidor: el temporizador tiene una fecha explícita y la acción solo detiene la demo y retira SU propia unidad (nunca otros servicios ni rutas ajenas)."""
import re
from pathlib import Path

D = Path(__file__).resolve().parents[2] / "deploy" / "hp"
TIMER, SERVICE = (D / "aicos-demo-apagado.timer").read_text(), (D / "aicos-demo-apagado.service").read_text()
EXEC = [ln.split("=", 1)[1] for ln in SERVICE.splitlines() if ln.startswith("ExecStart=")]


def test_el_temporizador_tiene_fecha_explicita_con_zona_y_recupera_si_el_servidor_estaba_apagado():
    assert re.search(r"^OnCalendar=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$", TIMER, re.M)
    assert "Persistent=true" in TIMER and "Unit=aicos-demo-apagado.service" in TIMER and "WantedBy=timers.target" in TIMER


def test_la_accion_solo_toca_la_demo_y_su_propia_unidad():
    assert "Type=oneshot" in SERVICE
    joined = "\n".join(EXEC)
    assert "stop aicos-demo.service" in joined and "rm -f %h/.config/containers/systemd/aicos-demo.container" in joined
    assert "disable aicos-demo-apagado.timer" in joined
    for forbidden in ("rm -rf", "podman volume", "podman rm", "podman image", "kill", "sudo", "reboot", "n8n", "hermes", "oficina"):
        assert forbidden not in joined, forbidden
    assert not re.search(r"rm\s+-f\s+(?!%h/\.config/containers/systemd/aicos-demo\.container$)", "\n".join(e for e in EXEC if e.startswith("/usr/bin/rm")))
