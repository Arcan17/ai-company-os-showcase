"""La herramienta de mutaciones de V0.8 (`qa/mutations_v08.py`) aplica reemplazos de texto EXACTOS sobre el código: si el código cambia y un texto deja de existir (o aparece dos veces), una mutación
dejaría de probar lo que dice probar. Esta prueba lo detecta (no ejecuta las mutaciones: eso lo hace el script)."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "qa" / "mutations_v08.py"


def test_every_v08_mutation_still_applies_exactly_once_to_the_current_code():
    p = subprocess.run([sys.executable, str(SCRIPT), "--check-needles"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "existen una sola vez" in p.stdout, (p.stdout, p.stderr)
