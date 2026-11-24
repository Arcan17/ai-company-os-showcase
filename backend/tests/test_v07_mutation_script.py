"""La herramienta de mutaciones de V0.7 P1 (`qa/mutations_v07_p1.py`) aplica reemplazos de texto EXACTOS sobre el código: si el código cambia y un texto deja de existir (o aparece dos veces), la herramienta
dejaría de probar lo que dice probar. Esta prueba lo detecta (no ejecuta las mutaciones: eso lo hace el script, ver docs/v07/P1_MODO_PILOTO.md)."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "qa" / "mutations_v07_p1.py"


def test_every_mutation_still_applies_exactly_once_to_the_current_code():
    p = subprocess.run([sys.executable, str(SCRIPT), "--check-needles"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "existen una sola vez" in p.stdout, (p.stdout, p.stderr)
