#!/usr/bin/env python3
"""Mutaciones de V0.11 (spec 006: primera impresión de la demo — datos de ejemplo, recorrido y resultados verificables): rompe UNA propiedad cada vez en una COPIA de `backend/` + `qa/` y comprueba que las pruebas lo NOTAN.
Una mutación «sobrevive» si, con ella aplicada, las pruebas dirigidas siguen pasando: esa propiedad no estaría protegida.
Uso (raíz del repo):   backend/.venv/bin/python qa/mutations_v11.py [--list] [--only W02,W05] [--check-needles] [--json salida.json]"""
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
EX, INFO, RES = "tests/test_v11_demo_example.py", "tests/test_v11_demo_info.py", "tests/test_v11_results.py"
DE, API, PUB = "backend/app/demo_example.py", "backend/app/api.py", "backend/app/public_demo.py"

# id, qué se rompe, archivo (relativo a la raíz del repo), texto exacto (o lista de pares), reemplazo, pruebas (archivos que deben fallar)
M = [
    # ── US1: datos de ejemplo ──
    ("E01", "el ejemplo se crea también en modo piloto", DE, "    if not settings.demo_example or pilot.is_pilot():", "    if not settings.demo_example:", [EX]),
    ("E02", "el ejemplo se crea aunque no esté activado (apagado por defecto deja de serlo)", DE, "    if not settings.demo_example or pilot.is_pilot():", "    if pilot.is_pilot():", [EX]),
    ("E03", "la marca se escribe aunque no haya una decisión pendiente (a medias)", DE, '        if status != "waiting_approval" or not pend:', "        if False:", [EX]),
    ("E04", "una base que ya se usó recibe datos de ejemplo", DE, '        if one(c, "SELECT 1 AS x FROM runs WHERE company_id=:c", c=cid):', "        if False:", [EX]),
    ("E05", "el ejemplo lo arma el modelo de IA (no el motor determinista)", DE, "    orch.provider = DeterministicProvider()                                        # el ejemplo es SIEMPRE el mismo, aunque la instancia use un modelo; se restaura abajo", "    pass", [EX]),
    ("E09", "tras crear el ejemplo el proveedor no se restaura (la demo con IA queda con el motor determinista)", DE, "        settings.step_delay, orch.provider = pace, modelo", "        settings.step_delay = pace", [EX]),
    ("E06", "el ejemplo no es idempotente (dos arranques lo duplican)", DE, [('        if one(c, "SELECT 1 AS x FROM audit_log WHERE company_id=:c AND action=:a", c=cid, a=MARK):', "        if False:"),
                                                                                ('        if one(c, "SELECT 1 AS x FROM runs WHERE company_id=:c", c=cid):', "        if False:")], None, [EX]),
    ("E07", "la demo pública dice que no hay ejemplo aunque lo haya", PUB, '"example": bool(settings.demo_example)})', '"example": False})', [INFO]),
    ("E08", "tener datos de ejemplo hace aparecer el banner «DEMO COMERCIAL» (`demo` = true)", API, 'return {**base, "example": True} if settings.demo_example else base', 'return {**base, "example": True, "demo": True} if settings.demo_example else base', [INFO]),
    # ── US3: tarjeta de resultados ──
    ("R01", "la tarjeta de resultados no exige el permiso (administración y técnico/a ven las cifras)", API, '        need(user, "company.summary")\n        from datetime import datetime, timedelta, timezone', "        from datetime import datetime, timedelta, timezone", [RES]),
    ("R02", "las solicitudes cerradas se cuentan de TODAS las empresas", API, "\"SELECT created_at,updated_at FROM requests WHERE company_id=:c AND status='cerrada' AND updated_at>=:s\", c=cid, s=since)", "\"SELECT created_at,updated_at FROM requests WHERE status='cerrada' AND updated_at>=:s\", s=since)", [RES]),
    ("R03", "las aprobaciones decididas se cuentan de TODAS las empresas", API, "FROM approvals WHERE company_id=:c AND status<>'pendiente' AND decided_at>=:s\", c=cid, s=since)", "FROM approvals WHERE status<>'pendiente' AND decided_at>=:s\", s=since)", [RES]),
    ("R04", "sin solicitudes cerradas la tarjeta muestra 0 en vez de «sin datos» (null)", API, '"median_hours_to_close": round(median(hours), 2) if hours else None}', '"median_hours_to_close": round(median(hours), 2) if hours else 0}', [RES]),
    ("R05", "la ventana de 30 días se ignora (un cierre de hace 300 días distorsiona la cifra)", API, "timedelta(days=RESULTS_DAYS)).isoformat(timespec=", "timedelta(days=36500)).isoformat(timespec=", [RES]),
    ("R06", "una aprobación pendiente cuenta como decisión tomada", API, "status<>'pendiente' AND decided_at>=:s\", c=cid", "(status<>'pendiente' OR status='pendiente') AND (decided_at>=:s OR decided_at IS NULL)\", c=cid", [RES]),
    ("R07", "el tiempo por solicitud es un promedio (un caso atípico lo deforma) y no la mediana", API, "round(median(hours), 2) if hours", "round(sum(hours) / len(hours), 2) if hours", [RES]),
    ("R08", "una solicitud abierta cuenta como atendida", API, "status='cerrada' AND updated_at>=:s\", c=cid, s=since)", "updated_at>=:s\", c=cid, s=since)", [RES]),
]
M = [(i, w, f, o, n if n is not None else "", t) for i, w, f, o, n, t in M]


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
