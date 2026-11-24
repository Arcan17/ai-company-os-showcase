#!/usr/bin/env python3
"""Mutaciones de V0.8 (spec 003, control de datos de clientes): rompe UNA propiedad cada vez en una COPIA de `backend/` + `qa/` (nunca en el árbol de trabajo) y comprueba que las pruebas lo NOTAN.

Una mutación «sobrevive» si, con ella aplicada, las pruebas dirigidas siguen pasando: esa propiedad no estaría protegida. Antes de mutar se comprueba que la copia SIN mutar pasa las mismas pruebas.
Uso (raíz del repo):   backend/.venv/bin/python qa/mutations_v08.py [--list] [--only S01,S05] [--check-needles] [--json salida.json]
Usa bases TEMPORALES; no abre ni toca `backend/data/`. Cada mutación es un reemplazo de texto EXACTO que debe aparecer una sola vez (si el código cambia, el script lo avisa).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "backend" / ".venv" / "bin" / "python"
PRIV = "tests/test_v08_customer_privacy.py"
CTRL = "tests/test_v08_customer_data_control.py"
MATRIX = "tests/test_v07_cross_tenant_matrix.py"
API, PRIVACY = "backend/app/api.py", "backend/app/privacy.py"

# id, qué se rompe, archivo (relativo a la raíz del repo), texto exacto (o lista de pares), reemplazo, pruebas (archivos) que DEBEN fallar
M = [
    # ── historia 1: el técnico/a no recibe datos de contacto ──
    ("S01", "los eventos de una ejecución llegan sin enmascarar al técnico/a", API, 'return scrub(eng, user, {"events": fetch(eng, user["company_id"], rid, after)})', 'return {"events": fetch(eng, user["company_id"], rid, after)}', [PRIV]),
    ("S02", "el detalle de una ejecución llega sin enmascarar", API, 'return scrub(eng, user, {"id": r["id"], "status": r["status"],', 'return ({"id": r["id"], "status": r["status"],', [PRIV]),
    ("S03", "el flujo en vivo (SSE) no enmascara", API, "e = await run_in_threadpool(scrub, eng, user, e)", "e = e", [PRIV]),
    ("S04", "la lista de tareas no enmascara", API, 'return scrub(eng, user, {"tasks": out["tasks"], "waiting": waiting})', 'return {"tasks": out["tasks"], "waiting": waiting}', [PRIV]),
    ("S05", "las órdenes de trabajo no enmascaran", API, 'return scrub(eng, user, {"work_orders": all_(c, sql, **p)})', 'return {"work_orders": all_(c, sql, **p)}', [PRIV]),
    ("S06", "todos los roles pasan por «puede ver contacto»", PRIVACY, 'return "customers.read" in ROLE_PERMISSIONS.get(role, set())', "return True", [PRIV]),
    ("S07", "solo se enmascaran las claves (phone/email), no los valores dentro de textos", PRIVACY, "    return sorted(vals, key=len, reverse=True)", "    return []", [PRIV]),
    # ── historia 2: registro de consultas ──
    ("S08", "abrir una ficha no deja registro", PRIVACY, 'audit(conn, company_id, "user", actor_id, "customer.view", customer_id, "ok", f"campos: {fields}")', "pass", [CTRL]),
    ("S09", "administración también puede leer el registro de accesos", API, 'need(user, "customers.access_log")', 'need(user, "customers.read")', [CTRL]),
    ("S10", "el registro de accesos no comprueba la empresa del cliente", PRIVACY, 'if not one(c, "SELECT 1 AS x FROM customers WHERE id=:i AND company_id=:c", i=customer_id, c=company_id):', 'if not one(c, "SELECT 1 AS x FROM customers WHERE id=:i", i=customer_id):', [CTRL, MATRIX]),
    ("S11", "recargar la misma ficha llena el registro (sin deduplicar)", PRIVACY, "DEDUP_SECONDS = 600   # una persona consultando la misma ficha (o con la pantalla abierta, que se actualiza sola) deja UNA entrada cada 10 minutos", "DEDUP_SECONDS = 0", [CTRL]),
    # ── historia 3: exportar y anonimizar ──
    ("S12", "exportar no comprueba la empresa del cliente", PRIVACY, 'cu = one(c, "SELECT id,name,phone,email,address,created_at FROM customers WHERE id=:i AND company_id=:c", i=customer_id, c=company_id)', 'cu = one(c, "SELECT id,name,phone,email,address,created_at FROM customers WHERE id=:i", i=customer_id)', [CTRL, MATRIX]),
    ("S13", "exportar no deja auditoría", API, 'audit(c, user["company_id"], "user", user["id"], "customer.export", kid, "ok", "")', "pass", [CTRL]),
    ("S14", "administración también puede exportar", API, 'need(user, "customers.export")', 'need(user, "customers.read")', [CTRL]),
    ("S15", "anonimizar no exige escribir el nombre", PRIVACY, 'if (confirm_name or "").strip() != cu["name"].strip():', "if False:", [CTRL]),
    ("S16", "se anonimiza un cliente con trabajo activo", PRIVACY, "        if active:", "        if False:", [CTRL]),
    ("S17", "administración también puede anonimizar", API, 'need(user, "customers.anonymize")   # solo la dueña, con autenticación reciente', 'need(user, "customers.read")', [CTRL]),
    ("S18", "anonimizar no barre las demás tablas", PRIVACY, 'if "company_id" not in t.c or t.name in ("customers", "users", "identities", "invitations", "sessions", "agents"):', "if True:", [CTRL]),
    ("S19", "anonimizar deja la ficha del cliente intacta", PRIVACY, 'q(c, "UPDATE customers SET name=:a, phone=NULL, email=NULL, address=NULL WHERE id=:i AND company_id=:c", a="Cliente anonimizado", i=customer_id, c=company_id)', "pass", [CTRL]),
    ("S20", "anonimizar no deja auditoría", PRIVACY, 'audit(c, company_id, "user", actor["id"], "customer.anonymize", customer_id, "ok", f"filas tocadas: {touched}")', "pass", [CTRL]),
    ("S21", "anonimizar borra también montos y estados (no se conserva la contabilidad)", PRIVACY, 'if "company_id" not in t.c or t.name in ("customers", "users", "identities", "invitations", "sessions", "agents"):', 'if "company_id" not in t.c:\n                continue\n            if t.name == "quotes":\n                c.execute(text("UPDATE quotes SET total=0 WHERE company_id=:c"), {"c": company_id})\n            if t.name in ("customers", "users", "identities", "invitations", "sessions", "agents"):', [CTRL]),
]


def apply(copy: Path, mut) -> None:
    _id, _what, rel, old, new, _t = mut
    f = copy / rel
    text = f.read_text()
    pairs = old if isinstance(old, list) else [(old, new)]            # una mutación puede romper una propiedad en DOS sitios a la vez (defensa en profundidad)
    for o, n in pairs:
        if text.count(o) != 1:
            raise SystemExit(f"{_id}: el texto a mutar aparece {text.count(o)} veces (debe ser 1) en {rel}: {o[:70]!r}")
        text = text.replace(o, n)
    f.write_text(text)


def clone(dst: Path) -> None:
    ig = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "data", "node_modules", ".pytest_cache")
    for rel in ("backend/app", "backend/tests", "qa"):
        shutil.copytree(ROOT / rel, dst / rel, ignore=ig)
    for rel in ("backend/requirements.txt",):
        if (ROOT / rel).exists():
            shutil.copy(ROOT / rel, dst / rel)
    (dst / "backend" / "data").mkdir(exist_ok=True)


def run_tests(copy: Path, targets: list[str]) -> tuple[int, list[str], float]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}
    env.update(AICOS_LLM_PROVIDER="deterministic", PYTHONDONTWRITEBYTECODE="1")
    t0 = time.time()
    p = subprocess.run([str(PY), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--tb=no", "-rf", *targets], cwd=copy / "backend", env=env, capture_output=True, text=True, timeout=900)
    fails = [re.sub(r" - .*", "", ln)[7:] for ln in p.stdout.splitlines() if ln.startswith("FAILED")]
    return p.returncode, fails, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--check-needles", action="store_true", help="solo comprueba que cada texto a mutar existe una vez (sin ejecutar pruebas)")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    todo = [m for m in M if not only or m[0] in only]
    if a.list:
        for m in todo:
            print(f"{m[0]}  {m[1]}  [{', '.join(Path(t).name for t in m[5])}]")
        return 0
    results, survivors = [], []
    with tempfile.TemporaryDirectory(prefix="aicos-mut-") as tmp:
        base = Path(tmp) / "base"
        clone(base)
        if a.check_needles:
            for m in todo:
                c = Path(tmp) / m[0]
                shutil.copytree(base, c)
                apply(c, m)
            print(f"OK: los {len(todo)} textos a mutar existen una sola vez")
            return 0
        targets = sorted({t for m in todo for t in m[5]})
        code, fails, secs = run_tests(base, targets)
        print(f"BASE sin mutar: {'PASA' if code == 0 else 'FALLA'} ({secs:.0f} s) {fails[:3]}")
        if code != 0:
            return 2
        for m in todo:
            c = Path(tmp) / m[0]
            shutil.copytree(base, c)
            apply(c, m)
            code, fails, secs = run_tests(c, m[5])
            ok = code != 0
            results.append({"id": m[0], "mutation": m[1], "detected": ok, "failed_tests": fails, "seconds": round(secs, 1)})
            if not ok:
                survivors.append(m[0])
            print(f"{m[0]}  {'DETECTADA ' if ok else 'SOBREVIVE  '} {m[1]}  →  {fails[0] if fails else '(ninguna prueba falló)'}  [{secs:.0f} s]")
            shutil.rmtree(c, ignore_errors=True)
    print(f"\n{len(results) - len(survivors)} / {len(results)} detectadas; sobreviven: {survivors or 'ninguna'}")
    if a.json:
        Path(a.json).write_text(json.dumps({"mutations": results, "survivors": survivors}, ensure_ascii=False, indent=1))
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
