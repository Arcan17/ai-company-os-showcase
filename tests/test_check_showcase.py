"""La comprobación de saneamiento debe DETECTAR cada categoría (mutación: se planta un caso en una copia limpia) y NO dar falsos positivos con lenguaje del dominio."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_showcase", ROOT / "tools" / "check_showcase.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CASES = {
    "ruta_absoluta": ("docs/a.md", "ver /Users/alguien/Documents/proyecto/x.py"),
    "correo_real": ("docs/b.md", "escríbeme a persona.ejemplo@gmail.com"),
    "herramienta": ("docs/c.md", "**NEXT_EXACT_STEP:** el propietario/ChatGPT revisa"),
    "clave": ("backend/app/d.py", 'KEY = "sk-' + "a1B2c3D4e5F6g7H8i9J0k1L2" + '"'),
}


def clean(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "backend/app").mkdir(parents=True)
    (tmp_path / "docs/ok.md").write_text("Un traspaso (handoff) entre personas, cuentas dueno@climasur.test, clave de demostración demo1234.\n")
    (tmp_path / "backend/app/ok.py").write_text("retry_secret = 'secreto-ficticio-de-prueba-123'\n")
    return tmp_path


def test_a_clean_tree_passes_and_domain_language_is_not_a_false_positive(tmp_path):
    assert mod.scan(clean(tmp_path)) == []


@pytest.mark.parametrize("category", sorted(CASES))
def test_each_category_is_detected_when_planted(tmp_path, category):
    root = clean(tmp_path)
    rel, text = CASES[category]
    (root / rel).write_text(text + "\n")
    found = mod.scan(root)
    assert [c for _, c, _ in found] == [category], found
    assert mod.main([str(root)]) == 1


@pytest.mark.parametrize("name", [".env", "id.pem", "claves.key", "copia.sqlite", "datos.db", "respaldo.bak"])
def test_forbidden_file_types_are_detected_and_env_example_is_allowed(tmp_path, name):
    root = clean(tmp_path)
    (root / name).write_text("x")
    assert [c for _, c, _ in mod.scan(root)] == ["archivo"]
    (root / name).unlink()
    (root / ".env.example").write_text("AICOS_ENV=\n")
    assert mod.scan(root) == []


def test_the_tests_dir_secret_exemption_is_narrow(tmp_path):
    root = clean(tmp_path)
    (root / "backend/tests").mkdir(parents=True)
    (root / "backend/tests/t.py").write_text("x = chat(secret='retry-secret-ficticio-123')\n")
    assert mod.scan(root) == []
    (root / "backend/tests/t.py").write_text('KEY = "ghp_' + "A" * 36 + '"\n')
    assert [c for _, c, _ in mod.scan(root)] == ["clave"]


def test_placeholder_user_paths_are_not_flagged_but_a_real_looking_one_is(tmp_path):
    root = clean(tmp_path)
    (root / "docs/p.md").write_text("logs en /Users/ejemplo/Library/Logs y /Users/example/x\n")
    assert mod.scan(root) == []
    (root / "docs/p.md").write_text("logs en /Users/bastian/Library/Logs\n")
    assert [c for _, c, _ in mod.scan(root)] == ["ruta_absoluta"]
