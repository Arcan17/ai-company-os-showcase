#!/usr/bin/env python3
"""Mutaciones de V0.9 (spec 004, fase B: demo pública con un espacio aislado por visitante): rompe UNA propiedad cada vez en una COPIA de `backend/` + `qa/` (nunca en el árbol de trabajo)
y comprueba que las pruebas lo NOTAN. Una mutación «sobrevive» si, con ella aplicada, las pruebas dirigidas siguen pasando: esa propiedad no estaría protegida.
Uso (raíz del repo):   backend/.venv/bin/python qa/mutations_v09.py [--list] [--only V01,V05] [--check-needles] [--json salida.json]
Cuando una propiedad se defiende en DOS sitios la mutación rompe los dos a la vez (si no, sobreviviría por redundancia, no por falta de prueba)."""
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
PD = "tests/test_v09_public_demo.py"
F = "backend/app/public_demo.py"

# id, qué se rompe, archivo (relativo a la raíz del repo), texto exacto (o lista de pares), reemplazo, pruebas (archivos) que DEBEN fallar
M = [
    ("V01", "la cookie del visitante se acepta sin comprobar firma ni formato", F, "        return vid if ID_RE.match(vid) and hmac.compare_digest(sig, _sig(self.secret, vid)) else None", "        return vid", [PD]),
    ("V02", "no hay tope de espacios simultáneos", F, "            if len(self.spaces) >= self.max:", "            if False:", [PD]),
    ("V03", "no hay límite de espacios nuevos por origen", F, "        if len(q) >= self.new_per_source:", "        if False:", [PD]),
    ("V04", "los espacios inactivos nunca caducan", F, "        old = [v for v, s in self.spaces.items() if s.active == 0 and now - s.last_seen > self.idle]", "        old = []", [PD]),
    ("V05", "al caducar, la base del visitante queda en el disco", F, "                Path(str(s.db_path) + suffix).unlink(missing_ok=True)", "                pass", [PD]),
    ("V06", "el barrido borra un espacio con una petición en curso", F, "if s.active == 0 and now - s.last_seen > self.idle]", "if now - s.last_seen > self.idle]", [PD]),
    ("V07", "todos los visitantes comparten la misma base de datos", F, '            path = self.dir / "visitors" / f"{vid}.db"', '            path = self.dir / "visitors" / "shared.db"', [PD]),
    ("V08", "la interfaz y los recursos estáticos consumen cupo de visitantes", F, '        if not path.startswith("/api"):', "        if False:", [PD]),
    ("V09", "la comprobación de salud consume cupo de visitantes", F, 'path == "/api/health":', 'path == "/api/healthz":', [PD]),
    ("V10", "la cookie del visitante deja de ser HttpOnly/SameSite", F, "Path=/; HttpOnly; SameSite=Lax; Max-Age=", "Path=/; Max-Age=", [PD]),
    ("V11", "la demo pública arranca con la creación de empresas activa", F, "        if settings.allow_company_creation:", "        if False:", [PD]),
    ("V12", "la demo pública arranca en modo piloto", F, '        if settings.env == "pilot":', "        if False:", [PD]),
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
