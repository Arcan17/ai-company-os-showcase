#!/usr/bin/env python3
"""Mutaciones de V0.7 P2 (Issue #23): rompe UNA propiedad cada vez en una COPIA de `backend/` + `qa/` (nunca en el árbol de trabajo) y comprueba que las pruebas lo NOTAN.

Una mutación «sobrevive» si, con ella aplicada, las pruebas dirigidas siguen pasando: esa propiedad no estaría protegida. Antes de mutar se comprueba que la copia SIN mutar pasa las mismas pruebas.
Uso (raíz del repo):   backend/.venv/bin/python qa/mutations_v07_p2.py [--list] [--only P05,P12] [--check-needles] [--json salida.json]
Usa bases TEMPORALES (las pruebas crean las suyas); no abre ni toca `backend/data/`. Cada mutación se aplica con un reemplazo de texto EXACTO que debe aparecer una sola vez (si el código cambia, el script lo dice).
Cuando una propiedad se defiende en DOS sitios (p. ej. «un solo uso»: comprobación previa + UPDATE condicional) la mutación rompe los dos a la vez: si no, sobreviviría por redundancia, no por falta de prueba.
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
INV = "tests/test_v07_p2_invitations.py"
FLOW = "tests/test_v07_p2_login_flow.py"
STEP = "tests/test_v07_p2_step_up.py"
GUARD = "tests/test_v07_p2_pilot_guard.py"
FAKE = "tests/test_v07_p2_fake_provider.py"
AUDIT = "tests/test_v07_audit_tool.py"
MATRIX = "tests/test_v07_cross_tenant_matrix.py"
FLOWPY, INVPY, API, SEC, PILOT = "backend/app/identity_flow.py", "backend/app/invitations.py", "backend/app/api.py", "backend/app/security.py", "backend/app/pilot.py"

# id, qué se rompe, archivo (relativo a la raíz del repo), texto exacto (o lista de pares), reemplazo, pruebas (archivos) que DEBEN fallar
M = [
    # ── invitaciones ──
    ("P01", "el secreto de la invitación se guarda en claro", INVPY, "h=token_hash(token), u=actor[\"id\"]", "h=token, u=actor[\"id\"]", [INV]),
    ("P02", "la invitación dura 30 días en lugar de 7", INVPY, "INVITE_DAYS = 7", "INVITE_DAYS = 30", [INV]),
    ("P03", "un enlace vencido todavía inicia el flujo", FLOWPY, "WHERE token_hash=:h AND status='pending' AND expires_at>:n\"", "WHERE token_hash=:h AND status='pending'\"", [FLOW]),
    ("P04", "una invitación vencida o revocada mientras la persona está en el proveedor aún se acepta", FLOWPY,
     [("    if not inv or inv[\"status\"] != \"pending\" or inv[\"expires_at\"] <= n:", "    if not inv:"), ("accepted_at=:n WHERE id=:i AND company_id=:c AND status='pending' AND expires_at>:n\"", "accepted_at=:n WHERE id=:i AND company_id=:c\"")], "", [FLOW]),
    ("P05", "el correo del proveedor no tiene que coincidir con el invitado", FLOWPY, "    if ident.email != ident.email.strip() or ident.email.lower() != inv[\"email_key\"]:", "    if False:", [FLOW]),
    ("P06", "el correo se compara distinguiendo mayúsculas", FLOWPY, "ident.email.lower() != inv[\"email_key\"]", "ident.email != inv[\"email_key\"]", [FLOW]),
    ("P07", "un correo SIN verificar inicia sesión", FLOWPY, "    if ident.email_verified is not True:", "    if False:", [FLOW]),
    ("P08", "«true», 1 o «yes» cuentan como correo verificado", FLOWPY, "    if ident.email_verified is not True:", "    if not ident.email_verified:", [FLOW]),
    ("P09", "una identidad ya vinculada se re-vincula a otra persona", FLOWPY, "    if one(c, \"SELECT 1 AS x FROM identities WHERE provider=:p AND subject=:s\", p=ident.provider, s=ident.subject):", "    if False:", [FLOW]),
    ("P10", "la cuenta creada por invitación tiene una contraseña utilizable", FLOWPY, "    return UNUSABLE_PASSWORD_PREFIX + secrets.token_hex(24)", "    return __import__(\"app.security\", fromlist=[\"x\"]).hash_password(\"clave-ficticia-123\")", [FLOW]),
    ("P11", "una persona sin invitación ni vínculo se enlaza por su CORREO (aprovisionamiento por coincidencia)", FLOWPY,
     "    if not row:\n        raise FlowRefused()                                                      # sin invitación y sin vínculo: nada se crea (sin aprovisionamiento automático)\n",
     "    if not row:\n        _u = one(c, \"SELECT * FROM users WHERE lower(email)=:e\", e=ident.email.lower())\n        if not _u:\n            raise FlowRefused()\n        row = {\"user_id\": _u[\"id\"], \"company_id\": _u[\"company_id\"]}\n", [FLOW]),
    ("P12", "una persona desactivada todavía inicia sesión con su identidad", FLOWPY, "AND COALESCE(active,1)=1\", i=row[\"user_id\"], c=row[\"company_id\"])", "\", i=row[\"user_id\"], c=row[\"company_id\"])", [FLOW]),
    ("P13", "desactivar a una persona no pide la baja al proveedor", API, "        if idp is not None and not body.active and not res.get(\"deduplicated\"):", "        if False:", [FLOW]),
    # ── flujo: state, PKCE, nonce, cookie ──
    ("P14", "el flujo no se quema antes de hablar con el proveedor (se puede reintentar con el mismo state)", FLOWPY,
     "UPDATE auth_flows SET used_at=:n WHERE state_hash=:h AND used_at IS NULL AND expires_at>:n\"", "UPDATE auth_flows SET used_at=used_at WHERE state_hash=:h AND expires_at>:n\"", [FLOW]),
    ("P15", "el flujo caducado (10 min) todavía se acepta", FLOWPY, "WHERE state_hash=:h AND used_at IS NULL AND expires_at>:n\"", "WHERE state_hash=:h AND used_at IS NULL\"", [FLOW]),
    ("P16", "la cookie de correlación no se comprueba (login CSRF)", FLOWPY, "    if not f or not hmac.compare_digest(f[\"cookie_hash\"], _h(flow_secret)):", "    if not f:", [FLOW]),
    ("P17", "el nonce no se presenta al proveedor", FLOWPY, "idp.exchange(code, f[\"verifier\"], nonce=f[\"nonce\"])", "idp.exchange(code, f[\"verifier\"], nonce=None)", [FLOW]),
    ("P18", "una sesión nueva ya no reemplaza a la que presentó el navegador", FLOWPY, "            delete_session(c, presented_session)                                # una sesión nueva", "            pass                                # una sesión nueva", [FLOW, STEP]),
    ("P19", "la sesión por proveedor ignora el `auth_time` informado (siempre «ahora»)", FLOWPY, "token = create_session(c, user, settings.session_hours, auth_time=auth_iso)", "token = create_session(c, user, settings.session_hours)", [STEP]),
    ("P20", "un proveedor que dice haber autenticado en el futuro inicia sesión", FLOWPY, "    if (t - clock.utcnow()).total_seconds() > FUTURE_SKEW_SECONDS:", "    if False:", [STEP]),
    ("P21", "la reautenticación acepta la sesión vieja del proveedor", FLOWPY,
     "    if not auth_iso or datetime.fromisoformat(auth_iso) < started - timedelta(seconds=REAUTH_SLACK_SECONDS) or not auth_fresh({\"auth_time\": auth_iso}):", "    if False:", [STEP]),
    ("P22", "la reautenticación acepta a OTRA identidad", FLOWPY, "    if not row or not hmac.compare_digest(row[\"subject\"], ident.subject):", "    if not row:", [STEP]),
    ("P23", "el inicio del flujo acepta campos de identidad del navegador", API, "            model_config = ConfigDict(extra=\"forbid\")        # el navegador no aporta", "            model_config = ConfigDict(extra=\"ignore\")        # el navegador no aporta", [FLOW]),
    # ── autenticación reciente ──
    ("P24", "las operaciones sensibles dejan de exigir autenticación reciente", API, "        if enforce_reauth and perm in STEP_UP_PERMS and user.get(\"_write\") and not user.get(\"_auth_fresh\"):", "        if False:", [STEP]),
    ("P25", "decidir aprobaciones deja de ser sensible", API, "STEP_UP_PERMS = {\"config.manage\", \"approvals.decide\", \"customers.anonymize\"}", "STEP_UP_PERMS = {\"config.manage\", \"customers.anonymize\"}", [STEP]),
    ("P26", "un `auth_time` ausente cuenta como reciente", SEC, "    raw = s.get(\"auth_time\")\n    if not raw:\n        return False", "    raw = s.get(\"auth_time\")\n    if not raw:\n        return True", [STEP, INV]),
    ("P27", "la ventana es de una hora en lugar de 15 minutos", SEC, "(max_age_seconds if max_age_seconds is not None else settings.reauth_max_age_seconds)", "3600", [STEP]),
    ("P28", "un `auth_time` del futuro lejano cuenta como reciente", SEC, "    return -FUTURE_SKEW_SECONDS <= age <=", "    return age <=", [STEP]),
    ("P29", "la actividad de la sesión (touch) renueva `auth_time`", SEC, "UPDATE sessions SET last_seen_at=:n WHERE token_hash=:h\"", "UPDATE sessions SET last_seen_at=:n, auth_time=:n WHERE token_hash=:h\"", [STEP]),
    ("P30", "la reautenticación con contraseña acepta cualquier contraseña", API, "                if verify_password(body.password, user[\"password_hash\"]):", "                if True:", [STEP]),
    ("P31", "la reautenticación con contraseña no tiene límite de intentos", API,
     "        ip, ekey, outcome, wait, at = client_ip(request), email_key(user[\"email\"]), \"fail\", 0, None\n        with eng.begin() as c:   # igual que el inicio de sesión: un intento fallido deja su rastro ANTES de cualquier error\n            wait = throttle_wait(c, ekey, ip)",
     "        ip, ekey, outcome, wait, at = client_ip(request), email_key(user[\"email\"]), \"fail\", 0, None\n        with eng.begin() as c:   # igual que el inicio de sesión: un intento fallido deja su rastro ANTES de cualquier error\n            wait = 0", [STEP]),
    # ── el simulado no entra en un piloto real ──
    ("P32", "el piloto acepta el proveedor SIMULADO", PILOT, "    if provider is not None and getattr(provider, \"is_fake\", True) is not False:", "    if False:", [GUARD]),
    ("P33", "AICOS_IDP_PROVIDER selecciona algo", PILOT, "    if (settings.idp_provider or \"\").strip():", "    if False:", [GUARD]),
    ("P34", "con un proveedor real la contraseña local sigue abierta", API, "    password_off = pilot and idp is not None", "    password_off = False", [GUARD]),
    ("P35", "las rutas de identidad se registran SIN proveedor", API, "    if idp is not None:   # SIN proveedor inyectado estas rutas no existen", "    if True:   # SIN proveedor inyectado estas rutas no existen", [GUARD]),
    ("P36", "el piloto acepta una ventana de reautenticación de más de 15 minutos", PILOT, "    if not 1 <= settings.reauth_max_age_seconds <= REAUTH_MAX_PILOT:", "    if False:", [GUARD]),
    ("P37", "la aplicación normal importa el módulo simulado", API, "from . import identity_flow as flow_mod\n", "from . import identity_flow as flow_mod\nfrom . import idp_fake  # noqa\n", [GUARD]),
    # ── límites ──
    ("P38", "crear invitaciones no tiene límite por empresa", API, "            ok, wait = limiter.take([ratelimit.Bucket(\"idp.invite.co\", user[\"company_id\"], \"-\")])", "            ok, wait = True, 0", [INV]),
    ("P39", "los enlaces de invitación inventados no tienen límite por dirección", API, "                purge_expired(c)\n                wait = throttle_wait(c, ekey, ip)", "                purge_expired(c)\n                wait = 0", [FLOW]),
    ("P40", "un enlace de invitación inventado no deja rastro del intento", API, "                        record_attempt(c, ekey, ip, False)        # se confirma ANTES de responder", "                        pass        # se confirma ANTES de responder", [FLOW]),
    # ── empresas ──
    ("P41", "revocar una invitación no filtra por empresa", INVPY, "inv = one(c, \"SELECT id,status FROM invitations WHERE id=:i AND company_id=:c AND COALESCE(kind,'employee')='employee'\", i=invitation_id, c=company_id)", "inv = one(c, \"SELECT id,status FROM invitations WHERE id=:i AND COALESCE(kind,'employee')='employee'\", i=invitation_id)", [INV]),
    ("P42", "la lista de invitaciones no filtra por empresa", INVPY, "FROM invitations WHERE company_id=:c AND COALESCE(kind,'employee')='employee' ORDER BY created_at DESC, id LIMIT 100\", c=company_id)", "FROM invitations WHERE COALESCE(kind,'employee')='employee' ORDER BY created_at DESC, id LIMIT 100\")", [INV]),
    ("P43", "se le escribe a un correo que ya pertenece a OTRA empresa", INVPY, "(None if elsewhere else ekey)", "ekey", [INV]),
    ("P44", "una ruta anónima NUEVA sin documentar", API, "    if settings.static_dir.exists():", "    @app.get(\"/api/zzz\")\n    def zzz():\n        return {\"ok\": True}\n\n    if settings.static_dir.exists():", [AUDIT]),
    ("P45", "una consulta NUEVA sin company_id sobre las invitaciones", INVPY, "        if inv[\"status\"] == \"revoked\":", "        _x = one(c, \"SELECT * FROM invitations WHERE id=:i\", i=invitation_id)\n        if inv[\"status\"] == \"revoked\":", [AUDIT]),
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
