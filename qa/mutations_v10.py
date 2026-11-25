#!/usr/bin/env python3
"""Mutaciones de V0.10 (spec 005, fase 1: IA real en el planificador, seguridad sin red): rompe UNA propiedad cada vez en una COPIA de `backend/` + `qa/` y comprueba que las pruebas lo NOTAN.
Una mutación «sobrevive» si, con ella aplicada, las pruebas dirigidas siguen pasando: esa propiedad no estaría protegida.
Uso (raíz del repo):   backend/.venv/bin/python qa/mutations_v10.py [--list] [--only W02,W05] [--check-needles] [--json salida.json]"""
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
HOST = "tests/test_v10_planner_hostile.py"
PLANS = "tests/test_plans.py"
PD = "tests/test_v09_public_demo.py"
AB = "tests/test_v10_anthropic_budget.py"
PL, BASE, PUB = "backend/app/engine/planner.py", "backend/app/providers/base.py", "backend/app/public_demo.py"
BUD, ANT = "backend/app/providers/budget.py", "backend/app/providers/anthropic_provider.py"

# id, qué se rompe, archivo (relativo a la raíz del repo), texto exacto (o lista de pares), reemplazo, pruebas (archivos) que DEBEN fallar
M = [
    ("W01", "no hay plazo duro: un modelo lento bloquea la creación del plan", PL, "        th.join(settings.llm_plan_timeout)", "        th.join()", [HOST]),
    ("W02", "un modelo lento no se distingue de uno caído (sin código «lentitud»)", PL, "        if th.is_alive():", "        if False:", [HOST]),
    ("W03", "una respuesta mal formada se acepta y no se valida su forma ni su validación", PL,
     [("        if not _well_formed(raw):", "        if False:"),
      ("                except Exception as e:                                    # una propuesta que rompe al validador es una propuesta inválida, no un error del servidor", "                except ZeroDivisionError as e:")], None, [HOST]),
    ("W04", "se omite el validador del motor: la propuesta del modelo se usa tal cual", PL, 'prop, rej = validate_llm_proposal(raw, text, ctx["open_requests"], ctx["customers"], everyone)', "prop, rej = raw, []", [HOST, PLANS]),
    ("W05", "una propuesta sin tareas soportadas ya no cae a las reglas", PL, "                if prop is not None and not (prop.review_pending or prop.admin_verify_materials or prop.technician_confirm):", "                if False and not (prop.review_pending or prop.admin_verify_materials or prop.technician_confirm):", [HOST]),
    ("W06", "el motivo del respaldo no se guarda en el plan", PL, 'pr=jdump({"overrides": overrides, "notes": d["notes"], "fallback": d.get("fallback")})', 'pr=jdump({"overrides": overrides, "notes": d["notes"], "fallback": None})', [HOST]),
    ("W07", "el modelo recibe las solicitudes completas (con descripción) y no solo lo mínimo", PL, "out[\"v\"] = prov.propose_plan(text, minimal_requests(open_requests))", "out[\"v\"] = prov.propose_plan(text, open_requests)", [HOST]),
    ("W08", "el nombre de un cliente llega al modelo sin sanear (símbolos y saltos de línea)", BASE, '    t = _LABEL_KEEP.sub(" ", str(value or ""))', '    t = str(value or "")', [HOST]),
    ("W09", "el nombre de un cliente llega al modelo sin límite de longitud", BASE, '    return " ".join(t.split())[:limit]', '    return " ".join(t.split())', [HOST]),
    ("W10", "la demo pública arranca con un modelo de IA", PUB, '        if settings.llm_provider != "deterministic":', "        if False:", [PD]),
    # ── fase 2: control de gasto y adaptador de pago ──
    ("X01", "se llama a la API aunque no haya cupo de gasto", ANT, "        if rid is None:                              # NO se llama: el tope de gasto no alcanza para el peor caso de esta llamada", "        if False:", [AB]),
    ("X02", "el tope DIARIO no se verifica", BUD, "(SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE day=:d)+:w <= :dc", "1=1", [AB]),
    ("X03", "el tope MENSUAL no se verifica", BUD, "(SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE month=:m)+:w <= :mc", "1=1", [AB]),
    ("X04", "las reservas de llamadas en curso no cuentan para el tope (dos llamadas simultáneas pasan el límite)", BUD, "FROM llm_usage WHERE day=:d)+:w <= :dc", "FROM llm_usage WHERE day=:d AND status!='reserved')+:w <= :dc", [AB]),
    ("X05", "al terminar no se cambia la reserva por el costo real", BUD, "SET input_tokens=:i,output_tokens=:o,cost_usd=:c,status=:s", "SET input_tokens=:i,output_tokens=:o,cost_usd=cost_usd,status=:s", [AB]),
    ("X06", "el peor caso ignora la salida (el tope se calcula de menos)", BUD, "+ max_tokens * self.pout / 1e6", "+ 0", [AB]),
    ("X07", "una llamada fallida no libera su reserva", ANT, "            cost = self.budget.settle(rid, tin, tout, status)", "            cost = 0.0", [AB]),
    ("X08", "la clave aparece en el repr del proveedor", ANT, "        self._key_mask = mask_key(api_key)", "        self._key_mask = api_key", [AB]),
    ("X09", "se envía temperatura (parámetro que estos modelos rechazan)", ANT, "max_tokens=self.max_tokens, system=SYSTEM,", "max_tokens=self.max_tokens, temperature=0, system=SYSTEM,", [AB]),
    ("X10", "el modelo recibe las solicitudes completas (con descripción)", ANT, "{[(r['id'], r.get('customer'), r.get('category')) for r in open_requests]}", "{open_requests}", [AB]),
    ("X11", "un rechazo del modelo se acepta como propuesta válida", ANT, '            if resp.stop_reason == "refusal":', "            if False:", [AB]),
    ("X12", "se activa el modelo de pago sin clave", ANT, "        if not api_key and client is None:", "        if False:", [AB]),
    ("X13", "un modelo sin precio conocido arranca de todos modos", BUD, '    raise ProviderConfigError(f"No se conoce el precio del modelo «{model}»: define', "    return (1.0, 1.0)  # define", [AB]),
    ("X14", "los topes de gasto inválidos se aceptan", BUD, "        if day_usd <= 0 or month_usd <= 0 or day_usd > month_usd:", "        if False:", [AB]),
    ("X15", "el tope de gasto alcanzado se rotula como un error cualquiera", PL, 'code = "presupuesto" if getattr(prov, "last_code", None) == "presupuesto" else "error"', 'code = "error"', [AB]),
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
