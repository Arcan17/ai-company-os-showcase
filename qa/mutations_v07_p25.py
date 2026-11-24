#!/usr/bin/env python3
"""Mutaciones de V0.7 P2.5 (Issue #25, PR A): rompe UNA propiedad cada vez en una COPIA de `backend/` + `qa/` (nunca en el árbol de trabajo) y comprueba que las pruebas lo NOTAN.

Una mutación «sobrevive» si, con ella aplicada, las pruebas dirigidas siguen pasando: esa propiedad no estaría protegida. Antes de mutar se comprueba que la copia SIN mutar pasa las mismas pruebas.
Uso (raíz del repo):   backend/.venv/bin/python qa/mutations_v07_p25.py [--list] [--only Q05,Q12] [--check-needles] [--json salida.json]
Usa bases TEMPORALES (las pruebas crean las suyas); no abre ni toca `backend/data/`. Cada mutación se aplica con un reemplazo de texto EXACTO que debe aparecer una sola vez (si el código cambia, el script lo dice).
Cuando una propiedad se defiende en DOS sitios la mutación rompe los dos a la vez: si no, sobreviviría por redundancia, no por falta de prueba.
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
OWN = "tests/test_v07_p25_owner_link.py"
LIM = "tests/test_v07_p25_idp_start_limits.py"
AUDIT = "tests/test_v07_audit_tool.py"
RACE = "tests/test_v07_p25_approval_race.py"
ORCH, RL = "backend/app/engine/orchestrator.py", "backend/app/ratelimit.py"
FLOWPY, INVPY, API, CLI, CFG = "backend/app/identity_flow.py", "backend/app/invitations.py", "backend/app/api.py", "backend/app/company_admin.py", "backend/app/config.py"

# id, qué se rompe, archivo (relativo a la raíz del repo), texto exacto (o lista de pares), reemplazo, pruebas (archivos) que DEBEN fallar
M = [
    # ── vínculo del dueño existente ──
    ("Q01", "el correo del proveedor no tiene que coincidir con el del dueño", FLOWPY, "    if ident.email != ident.email.strip() or ident.email.lower() != inv[\"email_key\"]:", "    if False:", [OWN]),
    ("Q02", "un token vencido o revocado todavía vincula (antes o durante el flujo)", FLOWPY,
     [("    if not inv or inv[\"status\"] != \"pending\" or inv[\"expires_at\"] <= n:", "    if not inv:"), ("accepted_user_id=:u WHERE id=:i AND company_id=:c AND status='pending' AND expires_at>:n\"", "accepted_user_id=:u WHERE id=:i AND company_id=:c\"")], "", [OWN]),
    ("Q03", "el objetivo del vínculo no se busca en la empresa de la invitación", FLOWPY,
     "target = one(c, \"SELECT * FROM users WHERE id=:i AND company_id=:c AND role='owner' AND COALESCE(active,1)=1\", i=inv[\"target_user_id\"], c=inv[\"company_id\"])",
     "target = one(c, \"SELECT * FROM users WHERE id=:i AND role='owner' AND COALESCE(active,1)=1\", i=inv[\"target_user_id\"])", [OWN]),
    ("Q04", "el vínculo de dueño acepta como objetivo a quien NO es dueño", FLOWPY, "AND company_id=:c AND role='owner' AND COALESCE(active,1)=1\", i=inv[\"target_user_id\"]", "AND company_id=:c AND COALESCE(active,1)=1\", i=inv[\"target_user_id\"]", [OWN]),
    ("Q05", "no se vuelve a comprobar que el dueño conserve el correo de la invitación", FLOWPY, "    if not target or target[\"email\"].strip().lower() != inv[\"email_key\"]:", "    if not target:", [OWN]),
    ("Q06", "un dueño que ya tiene identidad recibe una segunda", FLOWPY,
     "    if one(c, \"SELECT 1 AS x FROM identities WHERE user_id=:u AND company_id=:c\", u=target[\"id\"], c=inv[\"company_id\"]):\n        raise FlowRefused(**ctx)                                                 # un segundo vínculo",
     "    if False:\n        raise FlowRefused(**ctx)                                                 # un segundo vínculo", [OWN]),
    ("Q07", "una invitación de empleado puede crear un DUEÑO (rol owner en una fila editada)", FLOWPY, "    if kind != \"employee\" or inv[\"role\"] not in EMPLOYEE_ROLES:", "    if kind != \"employee\":", [OWN]),
    ("Q08", "la lista de invitaciones de la API muestra el vínculo del dueño", INVPY, " AND COALESCE(kind,'employee')='employee' ORDER BY created_at DESC, id LIMIT 100", " ORDER BY created_at DESC, id LIMIT 100", [OWN]),
    ("Q09", "la API puede revocar el vínculo del dueño", INVPY, "SELECT id,status FROM invitations WHERE id=:i AND company_id=:c AND COALESCE(kind,'employee')='employee'\"", "SELECT id,status FROM invitations WHERE id=:i AND company_id=:c\"", [OWN]),
    ("Q10", "el secreto del vínculo se guarda en claro", INVPY, "h=token_hash(token), u=actor, t=n, x=exp, o=owner[\"id\"]", "h=token, u=actor, t=n, x=exp, o=owner[\"id\"]", [OWN]),
    ("Q11", "la vigencia del vínculo no tiene límites", INVPY, "or not OWNER_LINK_MIN_MINUTES <= ttl_minutes <= OWNER_LINK_MAX_MINUTES:", "or False:", [OWN]),
    ("Q12", "se pueden emitir dos enlaces pendientes para el mismo dueño", INVPY, "        if one(c, \"SELECT 1 AS x FROM invitations WHERE company_id=:c AND kind='owner_link' AND status='pending'\", c=co[\"id\"]):", "        if False:", [OWN]),
    ("Q13", "se emite un enlace para un dueño que ya está vinculado", INVPY, "        if one(c, \"SELECT 1 AS x FROM identities WHERE user_id=:u AND company_id=:c\", u=owner[\"id\"], c=co[\"id\"]):", "        if False:", [OWN]),
    ("Q14", "desvincular no cierra las sesiones del dueño", INVPY, "                s = q(c, \"DELETE FROM sessions WHERE user_id=:u AND company_id=:c\", u=owner[\"id\"], c=co[\"id\"]).rowcount", "                s = 0", [OWN]),
    ("Q15", "desvincular no deja auditoría", INVPY, "        if n or links:", "        if False:", [OWN]),
    ("Q16", "revocar el enlace no lo revoca", INVPY, "        n = q(c, \"UPDATE invitations SET status='revoked', revoked_at=:n WHERE company_id=:c AND kind='owner_link' AND status IN ('pending','expired')\"", "        n = q(c, \"UPDATE invitations SET status=status, revoked_at=:n WHERE company_id=:c AND kind='owner_link' AND status IN ('pending','expired')\"", [OWN]),
    ("Q17", "vincular al dueño no deja auditoría", FLOWPY, "    audit(c, inv[\"company_id\"], \"user\", target[\"id\"], \"auth.owner_link\", inv[\"id\"], \"ok\", \"\")", "    pass", [OWN]),
    ("Q18", "un rechazo del vínculo se audita como una invitación de empleado", FLOWPY, "    return \"auth.owner_link\" if inv.get(\"kind\") == \"owner_link\" else \"auth.invite_accept\"", "    return \"auth.invite_accept\"", [OWN]),
    ("Q19", "la CLI desvincula sin pedir confirmación", CLI, "        if a.cmd == \"unlink-owner\" and not a.yes:", "        if False:", [OWN]),
    ("Q20", "la CLI de vínculo carga el proveedor simulado", CLI, "    from . import invitations ", "    from . import idp_fake  # noqa\n    from . import invitations ", [OWN]),
    ("Q21", "una consulta NUEVA sin company_id sobre el vínculo del dueño", INVPY, "        co, _ = _company_and_owner(c, slug)\n        n = q(c, \"UPDATE invitations SET status='revoked'", "        co, _ = _company_and_owner(c, slug)\n        _x = one(c, \"SELECT * FROM invitations WHERE kind='owner_link'\")\n        n = q(c, \"UPDATE invitations SET status='revoked'", [AUDIT]),
    # ── revisión del PR #26: desvincular también revoca el enlace PENDIENTE ──
    ("Q22", "desvincular no revoca el enlace pendiente (solo la identidad)", INVPY, "links = q(c, \"UPDATE invitations SET status='revoked', revoked_at=:n WHERE company_id=:c AND kind='owner_link' AND status='pending'\"", "links = q(c, \"UPDATE invitations SET status='revoked', revoked_at=:n WHERE company_id=:c AND kind='owner_link' AND status='nunca'\"", [OWN]),
    ("Q23", "desvincular revoca los enlaces pendientes de TODAS las empresas", INVPY, "WHERE company_id=:c AND kind='owner_link' AND status='pending'\", n=now(), c=co[\"id\"]).rowcount\n        n = s = 0", "WHERE kind='owner_link' AND status='pending'\", n=now()).rowcount\n        n = s = 0", [OWN]),
    # ── límites de idp/start (a nivel ASGI, antes de analizar el cuerpo) ──
    ("Q24", "idp/start no tiene ningún límite", API, "        app.add_middleware(ratelimit.IdpStartLimit, limiter=limiter)", "        pass", [LIM]),
    ("Q25", "idp/start no tiene límite GLOBAL", RL, ", Bucket(\"idp.start.all\", None, \"\")]", "]", [LIM]),
    ("Q26", "idp/start no tiene límite por ORIGEN", RL, "chain = [Bucket(\"idp.start.src\", None, client_source(scope)), ", "chain = [", [LIM]),
    ("Q27", "el origen del límite sale de una cabecera libre del cliente", RL, "Bucket(\"idp.start.src\", None, client_source(scope))", "Bucket(\"idp.start.src\", None, dict(scope[\"headers\"]).get(b\"x-forwarded-for\", b\"\").decode() or client_source(scope))", [LIM]),
    ("Q28", "el límite por origen por defecto baja a 3 (bloquearía una oficina entera)", CFG, "os.environ.get(\"AICOS_RL_IDP_START_SRC\", \"30/600\")", "os.environ.get(\"AICOS_RL_IDP_START_SRC\", \"3/600\")", [LIM]),
    ("Q29", "el límite también cuenta los GET", RL, "scope[\"type\"] != \"http\" or scope[\"method\"] != \"POST\" or scope[\"path\"] != IDP_START_PATH", "scope[\"type\"] != \"http\" or scope[\"path\"] != IDP_START_PATH", [LIM]),
    ("Q30", "el límite cuenta cualquier ruta que EMPIECE por /api/auth/idp/start", RL, "or scope[\"path\"] != IDP_START_PATH:", "or not scope[\"path\"].startswith(IDP_START_PATH):", [LIM]),
    ("Q31", "un limitador que falla deja pasar la petición (falla ABIERTO)", RL, "            return await _reply(send, 503, \"Acceso no disponible momentáneamente. Reintenta en unos segundos.\", 5)", "            return await self.app(scope, receive, send)", [LIM]),
    ("Q32", "el límite se instala FUERA de CSRF (los 403 gastan cupo)", API,
     [("    if idp is not None:   # V0.7 P2.5: el cupo de idp/start se cobra a nivel ASGI, antes de analizar el cuerpo (hallazgo de la revisión del PR #26)\n        app.add_middleware(ratelimit.IdpStartLimit, limiter=limiter)\n", ""),
      ("    app.add_middleware(CsrfMiddleware, eng=eng, cookie_name=cookie)\n", "    app.add_middleware(CsrfMiddleware, eng=eng, cookie_name=cookie)\n    if idp is not None:\n        app.add_middleware(ratelimit.IdpStartLimit, limiter=limiter)\n")], "", [LIM]),
    # ── revisión del PR #26: la carrera «aprobación decidida antes de esperar» ──
    ("R01", "la ejecución no vuelve a leer la aprobación al esperar (lost wake-up)", ORCH, "            if ap and ap[\"status\"] != \"pendiente\":", "            if False:", [RACE]),
    ("R02", "solo una aprobación APROBADA reanuda (un rechazo decidido en la ventana se pierde)", ORCH, "            if ap and ap[\"status\"] != \"pendiente\":", "            if ap and ap[\"status\"] == \"approved\":", [RACE]),
    ("R03", "reanudar sin ganar el CAS (se puede reanudar dos veces)", ORCH, "                resume = q(c, \"UPDATE runs SET status='running',phase='decided',updated_at=:t WHERE id=:r AND company_id=:c AND status='waiting_approval'\", t=now(), r=run_id, c=company_id).rowcount == 1", "                resume = True", [RACE]),
    ("R04", "esperar ya no exige que la ejecución siga `running` (reactiva una fallida)", ORCH, "WHERE id=:r AND company_id=:c AND status='running'\", t=now(), r=run_id, c=company_id).rowcount != 1:", "WHERE id=:r AND company_id=:c\", t=now(), r=run_id, c=company_id).rowcount != 1:", [RACE]),
    ("R05", "el presupuesto vuelve a esperar sin comprobar la decisión (código anterior)", ORCH, "        self.save(quote_total=quote[\"total\"], approval_subject=subject)\n        self.park_for_approval(ap[\"approval_id\"])", "        self.save(quote_total=quote[\"total\"], approval_subject=subject)\n        self.o._set(self.rid, status=\"waiting_approval\")", [RACE]),
    ("R06", "el material adicional vuelve a esperar sin comprobar la decisión (código anterior)", ORCH, "Ejecución en pausa hasta que un humano decida el gasto del material\")\n        self.park_for_approval(ap[\"approval_id\"])", "Ejecución en pausa hasta que un humano decida el gasto del material\")\n        self.o._set(self.rid, status=\"waiting_approval\")", [RACE]),
    ("R07", "la lectura de la aprobación al esperar no filtra por empresa", ORCH, "AND run_id=:r AND company_id=:c\", a=approval_id, r=run_id, c=company_id)", "AND run_id=:r\", a=approval_id, r=run_id)", [AUDIT]),
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
