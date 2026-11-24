#!/usr/bin/env python3
"""Mutaciones de V0.7 P1 (AC8, Issue #19): rompe UNA propiedad cada vez en una COPIA de `backend/` + `qa/` (nunca en el árbol de trabajo) y comprueba que las pruebas lo NOTAN.

Una mutación «sobrevive» si, con ella aplicada, las pruebas dirigidas siguen pasando: esa propiedad no estaría protegida. Antes de mutar se comprueba que la copia SIN mutar pasa las mismas pruebas.
Uso (raíz del repo):   backend/.venv/bin/python qa/mutations_v07_p1.py [--list] [--only M05,M12] [--check-needles] [--json salida.json]
Usa bases TEMPORALES (las pruebas crean las suyas); no abre ni toca `backend/data/`. Cada mutación se aplica con un reemplazo de texto EXACTO que debe aparecer una sola vez (si el código cambia, el script lo dice).
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
PILOT = "tests/test_v07_pilot_mode.py"
MATRIX = "tests/test_v07_cross_tenant_matrix.py"
GUARD = "tests/test_v07_audit_tool.py"

# id, qué se rompe, archivo (relativo a la raíz del repo), texto exacto, reemplazo, pruebas (archivos o ids de pytest) que DEBEN fallar
M = [
    # ── AC2: sin demostración ──
    ("M01", "demo-accounts vuelve a registrarse en piloto", "backend/app/api.py", "    if not pilot:   # devuelve la contraseña de demostración: SOLO fuera del piloto", "    if True:   # devuelve la contraseña de demostración", [PILOT]),
    ("M02", "setup/* vuelve a registrarse en piloto", "backend/app/api.py", "    if not pilot:   # alta local de empresas: SOLO fuera del piloto (allí las da de alta el operador con la CLI)", "    if True:   # alta local", [PILOT]),
    ("M03", "demo/info y reset-scenarios vuelven en piloto", "backend/app/api.py", "    if not pilot:   # V0.7 P1: en el piloto no existe ninguna ruta de demostración (404 real, no solo oculta)", "    if True:   # demo", [PILOT]),
    ("M04", "seed() siembra la demostración en piloto", "backend/app/seed.py", "    if pilot.is_pilot():      # el piloto NUNCA siembra", "    if False:      # el piloto NUNCA siembra", [PILOT]),
    # ── AC3: falla cerrado ──
    ("M05", "se acepta AICOS_COOKIE_SECURE distinto de 1", "backend/app/pilot.py", '    if settings.cookie_secure != "1":', "    if False:", [PILOT]),
    ("M06", "se acepta AICOS_TRUSTED_ORIGINS vacío", "backend/app/pilot.py", "    if not settings.trusted_origins:", "    if False:", [PILOT]),
    ("M07", "se acepta AICOS_DEMO_MODE=1", "backend/app/pilot.py", "    if settings.demo_mode:", "    if False:", [PILOT]),
    ("M08", "se acepta AICOS_ALLOW_COMPANY_CREATION=1", "backend/app/pilot.py", "    if settings.allow_company_creation:", "    if False:", [PILOT]),
    ("M09", "se aceptan proxies de confianza públicos", "backend/app/pilot.py", "    if _proxy_problem():", "    if False:", [PILOT]),
    ("M10", "se acepta una base con cuentas de demostración", "backend/app/pilot.py", "    why = _demo_accounts_problem(db_url)", "    why = None", [PILOT]),
    ("M11", "se acepta un AICOS_ENV desconocido", "backend/app/pilot.py", "    if m not in ENVS:", "    if False:", [PILOT]),
    ("M12", "create_app no llama a validate_startup", "backend/app/api.py", "    pilot_mod.validate_startup(db_url or settings.db_url, identity_provider)   #", "    #", [PILOT]),
    # ── AC4: cookie y cabeceras ──
    ("M13", "cookie de piloto sin prefijo __Host-", "backend/app/pilot.py", 'COOKIE_PILOT = "__Host-aicos_session"', 'COOKIE_PILOT = "aicos_session"', [PILOT]),
    ("M14", "la cookie sin prefijo también autentica en piloto", "backend/app/api.py", "    def current_user(request: Request) -> dict:\n        token = request.cookies.get(cookie)\n", '    def current_user(request: Request) -> dict:\n        token = request.cookies.get(cookie) or request.cookies.get("aicos_session")\n', [PILOT]),
    ("M15", "Secure ya no se fuerza en piloto (solo depende de la configuración)", "backend/app/api.py", "    def set_session_cookie(request: Request, response: Response, token: str):\n        secure = pilot or settings.cookie_secure", "    def set_session_cookie(request: Request, response: Response, token: str):\n        secure = settings.cookie_secure", [PILOT]),
    ("M16", "el borrado de cookie del logout no cumple __Host-", "backend/app/api.py", 'response.delete_cookie(cookie, path="/", secure=True, httponly=True, samesite="lax")', 'response.delete_cookie(cookie, path="/")', [PILOT]),
    ("M17", "falta HSTS", "backend/app/pilot.py", "BASE_HEADERS = [(b\"strict-transport-security\", HSTS), ", "BASE_HEADERS = [", [PILOT]),
    ("M18", "las respuestas de /api/* ya no llevan no-store", "backend/app/pilot.py", '([(b"cache-control", b"no-store")] if api else [])', "[]", [PILOT]),
    ("M19", "SecurityHeaders deja de ser el más externo (el rechazo de host sale sin cabeceras)", "backend/app/api.py",
     "        app.add_middleware(HostGuard, allowed=settings.trusted_origins)\n        app.add_middleware(SecurityHeaders)", "        app.add_middleware(SecurityHeaders)\n        app.add_middleware(HostGuard, allowed=settings.trusted_origins)", [PILOT]),
    ("M20", "no se valida el host", "backend/app/api.py", "        app.add_middleware(HostGuard, allowed=settings.trusted_origins)\n", "", [PILOT]),
    ("M21", "falta la CSP que impide incrustar la aplicación", "backend/app/pilot.py", 'CSP_ENFORCED = b"frame-ancestors \'none\'"', 'CSP_ENFORCED = b"default-src \'self\'"', [PILOT]),
    # ── AC5/AC6: aislamiento entre empresas ──
    ("M22", "GET /api/runs/{id} sin filtro de empresa", "backend/app/api.py", 'one(c, "SELECT * FROM runs WHERE id=:i AND company_id=:c", i=rid, c=user["company_id"])', 'one(c, "SELECT * FROM runs WHERE id=:i", i=rid)', [MATRIX, GUARD]),
    ("M23", "el coordinador carga planes sin filtro de empresa", "backend/app/engine/planner.py", 'return one(c, "SELECT * FROM plans WHERE id=:i AND company_id=:c", i=plan_id, c=company_id)', 'return one(c, "SELECT * FROM plans WHERE id=:i", i=plan_id)', [MATRIX, GUARD]),
    ("M24", "el flujo SSE de una ejecución se abre sin comprobar la empresa", "backend/app/api.py", '        need(user, "events.read")\n        _run(user, rid)\n        return _sse(request, user["company_id"], rid, after)', '        need(user, "events.read")\n        return _sse(request, user["company_id"], rid, after)', [MATRIX]),
    ("M25", "el token público se acepta con el slug de OTRA empresa", "backend/app/api.py", "WHERE cv.token=:t AND co.slug=:s\", t=token, s=slug)", "WHERE cv.token=:t\", t=token)", [MATRIX, GUARD]),
    ("M26", "la confirmación de visita no filtra por empresa", "backend/app/engine/orchestrator.py", 'one(c, "SELECT * FROM visits WHERE company_id=:c AND request_id=:r", c=co["id"], r=request_id) if co else None', 'one(c, "SELECT * FROM visits WHERE request_id=:r", r=request_id) if co else None', [MATRIX, GUARD]),
    ("M27", "las herramientas buscan solicitudes sin filtro de empresa", "backend/app/tools/definitions.py", 'r = one(ctx.conn, "SELECT * FROM requests WHERE id=:i AND company_id=:c", i=rid, c=ctx.company_id)', 'r = one(ctx.conn, "SELECT * FROM requests WHERE id=:i", i=rid)', [MATRIX, GUARD]),
    ("M28", "una ruta NUEVA con identificador sin caso en la matriz", "backend/app/api.py", "    if settings.static_dir.exists():",
     '    @app.get("/api/zzz/{zid}")\n    def zzz(zid: str, user=Depends(current_user)):\n        return {"id": zid}\n\n    if settings.static_dir.exists():', [MATRIX, GUARD]),
    ("M29", "una consulta NUEVA sin company_id en un módulo anidado", "backend/app/engine/planner.py", 'return one(c, "SELECT * FROM plans WHERE id=:i AND company_id=:c", i=plan_id, c=company_id)',
     'return one(c, "SELECT * FROM plans WHERE id=:i AND company_id=:c", i=plan_id, c=company_id) or one(c, "SELECT * FROM approvals WHERE id=:i", i=plan_id)', [GUARD]),
]


def apply(copy: Path, mut) -> None:
    _id, _what, rel, old, new, _t = mut
    f = copy / rel
    text = f.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"{_id}: el texto a mutar aparece {text.count(old)} veces (debe ser 1) en {rel}: {old[:70]!r}")
    f.write_text(text.replace(old, new))


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
