"""Cifras del sitio y su FUENTE reproducible. Una sola definición: la usan el generador y la comprobación, así una cifra publicada no puede divergir de lo que el repositorio dice hoy."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUT_SCRIPTS = ["qa/mutations_v07_p1.py", "qa/mutations_v07_p2.py", "qa/mutations_v07_p25.py", "qa/mutations_v08.py", "qa/mutations_v09.py", "qa/mutations_v10.py"]
E2E_DIRS = ["frontend/e2e", "frontend/e2e_pilot", "frontend/e2e_public"]
E2E_SKIP = ("audit",)          # la auditoría QA tiene su propia configuración y no corre en el CI


def _python() -> str:
    venv = ROOT / "backend" / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def count_tests(python: str | None = None) -> int:
    p = subprocess.run([python or _python(), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"], cwd=ROOT / "backend", capture_output=True, text=True, timeout=300)
    m = re.search(r"(\d+) tests? collected", p.stdout)
    if not m:
        raise RuntimeError("no se pudo contar las pruebas con pytest --collect-only: " + (p.stdout + p.stderr)[-300:])
    return int(m.group(1))


def count_mutations() -> dict[str, int]:
    out = {}
    for rel in MUT_SCRIPTS:
        spec = importlib.util.spec_from_file_location("m_" + Path(rel).stem, ROOT / rel)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out[rel] = len(mod.M)
    return out


def count_e2e() -> int:
    n = 0
    for d in E2E_DIRS:
        for f in sorted((ROOT / d).glob("*.spec.ts")):
            if not f.name.startswith(E2E_SKIP):
                n += len(re.findall(r"^test\(", f.read_text(), re.M))
    return n


def compute(python: str | None = None) -> dict[str, int]:
    mut = count_mutations()
    return {"tests": count_tests(python), "mutaciones": sum(mut.values()), "e2e": count_e2e()}


SOURCES = {
    "tests": "cd backend && python -m pytest --collect-only -q",
    "mutaciones": "python qa/mutations_v07_p{1,2,25}.py qa/mutations_v08.py qa/mutations_v09.py qa/mutations_v10.py --list | wc -l",
    "e2e": "npx playwright test --list (e2e, e2e_pilot, e2e_public; sin la auditoría QA)",
}
