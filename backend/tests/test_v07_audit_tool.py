"""V0.7 P0 (Issue #17) — la auditoría de identidad y aislamiento (`qa/audit_identity_tenancy.py`) como RED DE SEGURIDAD: la superficie anónima de la API y las consultas sobre tablas de empresa
sin `company_id` son exactamente las documentadas. Una ruta anónima o una consulta sin filtrar NUEVA hace fallar el CI hasta que alguien la revise y la justifique."""
import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "qa" / "audit_identity_tenancy.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("audit_identity_tenancy", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_anonymous_surface_is_exactly_the_documented_one(tool):
    a = tool.audit()
    assert not a["anonymous_unknown"], [(r["method"], r["path"]) for r in a["anonymous_unknown"]]            # una ruta pública NUEVA es una decisión de seguridad: se revisa y se documenta
    assert {(r["method"], r["path"]) for r in a["anonymous"]} == set(tool.ANONYMOUS_ROUTES)                  # y lo documentado sigue existiendo (si se borra una ruta, se borra de la lista)
    assert len(a["routes"]) >= 50 and sum(r["session"] for r in a["routes"]) >= 40


def test_every_query_on_a_tenant_table_filters_by_company_or_is_justified(tool):
    s = tool.audit()["sql"]
    assert s["statements_on_tenant_tables"] >= 150                                                             # el análisis realmente lee el código (no pasa por vacío)
    assert not s["unjustified"], s["unjustified"]


def test_the_allowlist_has_no_stale_entries(tool):
    """Una justificación que ya no corresponde a ninguna consulta es una puerta abierta para la próxima que se parezca: cada entrada se usa EXACTAMENTE las veces declaradas."""
    assert set(tool.ALLOWED_COUNT) <= set(tool.ALLOWED_GLOBAL)
    expected = {f"{k[0]} :: {k[1]}": tool.ALLOWED_COUNT.get(k, 1) for k in tool.ALLOWED_GLOBAL}
    usage = tool.sql_inventory()["allowlist_usage"]
    assert usage == expected, {"obsoletas o de menos": sorted(k for k in expected if usage.get(k) != expected[k]), "sin entrada": sorted(set(usage) - set(expected))}


def test_the_guard_flags_a_new_unfiltered_query_and_accepts_a_filtered_one(tool, tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "nuevo.py").write_text(
        'def a(c, i):\n    return one(c, "SELECT * FROM customers WHERE id=:i", i=i)\n'                         # sin company_id: otra empresa podría leerlo con solo conocer el id
        'def b(c, i, e):\n    return one(c, "SELECT * FROM customers WHERE id=:i AND company_id=:c", i=i, c=e)\n'
        'def d(c, t):\n    return q(c, f"DELETE FROM {t} WHERE id=:i")\n')                                     # el f-string sin tabla conocida no se confunde con una consulta de empresa
    monkeypatch.setattr(tool, "BACKEND", tmp_path)
    monkeypatch.setattr(tool, "_tenant_tables", lambda: {"customers"})
    s = tool.sql_inventory()
    assert s["statements_on_tenant_tables"] == 2 and [u["line"] for u in s["unjustified"]] == [2]


# ───────────── PR #18, hallazgo P2 de revisión automatizada: la guardia leía solo `backend/app/*.py`; el motor (`engine/`) y el resto de subcarpetas quedaban fuera ─────────────

def _real_tenant_tables(tool):
    return tool._tenant_tables()


def test_the_audit_walks_every_module_under_app_recursively(tool):
    """No debe quedar ningún .py de `backend/app` sin leer, a ninguna profundidad (la lista se calcula aquí por separado, con os.walk)."""
    import os
    app_dir = tool.BACKEND / "app"
    on_disk = {Path(d, f).relative_to(app_dir).as_posix() for d, _, files in os.walk(app_dir) if "__pycache__" not in d for f in files if f.endswith(".py")}
    scanned = set(tool.sql_inventory()["files_scanned"])
    assert {"engine/planner.py", "engine/orchestrator.py", "api.py"} <= scanned, sorted(scanned)
    assert scanned == on_disk, {"sin leer": sorted(on_disk - scanned), "inexistentes": sorted(scanned - on_disk)}


def test_a_nested_module_with_an_unfiltered_tenant_query_is_flagged(tool, tmp_path, monkeypatch):
    """El caso exacto de revisión automatizada: `app/engine/demo_nested.py` con una consulta de `customers` sin `company_id` debe salir en `unjustified`; con `company_id`, no. También a 2 niveles."""
    for rel in ("engine/demo_nested.py", "tools/deep/er/demo_deep.py"):
        p = tmp_path / "app" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('def a(c, i):\n    return one(c, "SELECT * FROM customers WHERE id=:i", i=i)\n'
                     'def b(c, i, e):\n    return one(c, "SELECT * FROM customers WHERE id=:i AND company_id=:c", i=i, c=e)\n')
    monkeypatch.setattr(tool, "BACKEND", tmp_path)
    monkeypatch.setattr(tool, "_tenant_tables", lambda: {"customers"})
    s = tool.sql_inventory()
    assert s["statements_on_tenant_tables"] == 4
    assert sorted((u["file"], u["line"]) for u in s["unjustified"]) == [("engine/demo_nested.py", 2), ("tools/deep/er/demo_deep.py", 2)]


def test_a_justification_is_bound_to_the_full_module_path_not_the_file_name(tool, tmp_path, monkeypatch):
    """Dos `util.py` de carpetas distintas: justificar la consulta de `a/util.py` NO justifica la misma consulta en `b/util.py`."""
    sql = "SELECT * FROM customers WHERE id=:i"
    for rel in ("a/util.py", "b/util.py"):
        p = tmp_path / "app" / rel
        p.parent.mkdir(parents=True)
        p.write_text(f'def q(c, i):\n    return one(c, "{sql}", i=i)\n')
    monkeypatch.setattr(tool, "BACKEND", tmp_path)
    monkeypatch.setattr(tool, "_tenant_tables", lambda: {"customers"})
    monkeypatch.setattr(tool, "ALLOWED_GLOBAL", {("a/util.py", sql): "razón de prueba"})
    s = tool.sql_inventory()
    assert [(u["file"], u["allowed"]) for u in s["without_company_id"]] == [("a/util.py", True), ("b/util.py", False)]
    assert [u["file"] for u in s["unjustified"]] == ["b/util.py"]


@pytest.mark.parametrize("rel, needle, mutated", [
    ("engine/planner.py", 'one(c, "SELECT * FROM plans WHERE id=:i AND company_id=:c", i=plan_id, c=company_id)', 'one(c, "SELECT * FROM plans WHERE id=:i", i=plan_id)'),                     # Planner._plan (:192)
    ("engine/orchestrator.py", '"SELECT * FROM approvals WHERE id=:i AND company_id=:c", i=approval_id, c=company_id', '"SELECT * FROM approvals WHERE id=:i", i=approval_id'),                # Orchestrator.decide
    ("tools/plans.py", '"SELECT * FROM plans WHERE id=:i AND company_id=:c", i=plan_id, c=ctx.company_id', '"SELECT * FROM plans WHERE id=:i", i=plan_id'),                                   # herramienta plans._plan
])
def test_removing_company_id_from_a_real_nested_query_is_detected(tool, tmp_path, monkeypatch, rel, needle, mutated):
    """La mutación de revisión automatizada, sobre una COPIA del código real (el original no se toca): quitar `AND company_id=:c` de una consulta del motor o de las herramientas debe aparecer como consulta sin justificar."""
    import shutil
    real = tool._tenant_tables()
    copy = tmp_path / "backend"
    shutil.copytree(tool.BACKEND / "app", copy / "app", ignore=shutil.ignore_patterns("__pycache__"))
    target = copy / "app" / rel
    src = target.read_text()
    assert src.count(needle) == 1, f"la consulta de {rel} cambió: actualiza esta prueba (no la ignores)"
    target.write_text(src.replace(needle, mutated))
    monkeypatch.setattr(tool, "BACKEND", copy)
    monkeypatch.setattr(tool, "_tenant_tables", lambda: real)
    bad = tool.sql_inventory()["unjustified"]
    assert [(u["file"], u["sql"].split(" WHERE ")[0], "company_id" in u["sql"]) for u in bad] == [(rel, mutated.split('"')[1].split(" WHERE ")[0], False)], bad


def test_an_allowlist_entry_matches_the_exact_sql_not_a_prefix(tool, tmp_path, monkeypatch):
    """Una variante de una consulta justificada (`... OR 1=1`) es OTRA consulta: no hereda la justificación. Va SOLA en su módulo: si compartiera módulo con la original, el límite de copias
    la marcaría por otra razón y la prueba no distinguiría coincidencia exacta de coincidencia por prefijo."""
    base = "SELECT * FROM customers WHERE id=:i"
    (tmp_path / "app" / "engine").mkdir(parents=True)
    (tmp_path / "app" / "engine" / "exacta.py").write_text(f'def a(c, i):\n    return one(c, "{base}", i=i)\n')
    (tmp_path / "app" / "engine" / "variante.py").write_text(f'def b(c, i):\n    return one(c, "{base} OR 1=1", i=i)\n')
    monkeypatch.setattr(tool, "BACKEND", tmp_path)
    monkeypatch.setattr(tool, "_tenant_tables", lambda: {"customers"})
    monkeypatch.setattr(tool, "ALLOWED_GLOBAL", {("engine/exacta.py", base): "razón de prueba", ("engine/variante.py", base): "razón de prueba"})
    monkeypatch.setattr(tool, "ALLOWED_COUNT", {})
    assert [(u["file"], u["allowed"]) for u in tool.sql_inventory()["without_company_id"]] == [("engine/exacta.py", True), ("engine/variante.py", False)]


def test_an_extra_identical_copy_of_a_justified_query_is_not_justified(tool, tmp_path, monkeypatch):
    """Dos consultas idénticas en el mismo módulo: la entrada declara cuántas justifica (ALLOWED_COUNT); una copia nueva de más se marca, aunque el SQL coincida."""
    sql = "SELECT * FROM customers WHERE id=:i"
    p = tmp_path / "app" / "m.py"
    p.parent.mkdir(parents=True)
    p.write_text("".join(f'def f{n}(c, i):\n    return one(c, "{sql}", i=i)\n' for n in range(3)))
    monkeypatch.setattr(tool, "BACKEND", tmp_path)
    monkeypatch.setattr(tool, "_tenant_tables", lambda: {"customers"})
    monkeypatch.setattr(tool, "ALLOWED_GLOBAL", {("m.py", sql): "razón de prueba"})
    monkeypatch.setattr(tool, "ALLOWED_COUNT", {("m.py", sql): 2})
    assert [(u["line"], u["allowed"]) for u in tool.sql_inventory()["without_company_id"]] == [(2, True), (4, True), (6, False)]
    monkeypatch.setattr(tool, "ALLOWED_COUNT", {("m.py", sql): 3})
    assert tool.sql_inventory()["unjustified"] == []
