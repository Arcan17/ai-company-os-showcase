#!/usr/bin/env python3
"""Auditoría de SOLO LECTURA de identidad y aislamiento multiempresa (V0.7 P0, Issue #17). No ejecuta la aplicación, no abre ninguna base de datos, no necesita red.

  1. RUTAS: cada ruta de `backend/app/api.py` → ¿exige sesión? ¿qué permiso/rol comprueba? Las rutas SIN sesión son la superficie anónima: cualquiera que se añada debe revisarse.
  2. SQL: cada consulta (literal o f-string) de TODOS los módulos de `backend/app/` (recursivo: `engine/`, `tools/`, `providers/`…) que toca una tabla con `company_id` pero NO filtra por
     `company_id`. Cada una debe estar justificada en ALLOWED_GLOBAL, con la RUTA del módulo relativa a `backend/app/` (p. ej. "engine/planner.py", no "planner.py": dos módulos con el mismo
     nombre en carpetas distintas no comparten justificaciones). Una consulta nueva y sin justificar es un posible cruce entre empresas.

Uso (raíz del repo):  backend/.venv/bin/python qa/audit_identity_tenancy.py [--json] [--check]
  --check  sale con 1 si hay una consulta de empresa sin filtrar que no esté justificada, o una ruta anónima que no esté en ANONYMOUS_ROUTES.
Es heurístico (análisis estático): una consulta que filtra por company_id puede seguir siendo incorrecta, y una sin filtro puede ser inocua. Ayuda a no olvidar; no sustituye a las pruebas
de cruce entre empresas (docs/v07/P0_AUDITORIA_IDENTIDAD_Y_TENANCY.md §5).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

# La superficie ANÓNIMA conocida (sin cookie de sesión). Cambiarla es una decisión de seguridad: se revisa en el PR y se explica aquí.
ANONYMOUS_ROUTES = {
    ("GET", "/api/health"): "vivacidad + rol activo/espera; sin datos",
    ("GET", "/api/demo/info"): "solo informa si el modo demo está activo; NO se registra con AICOS_ENV=pilot (V0.7 P1: 404)",
    ("GET", "/demo"): "página del entorno de demostración (solo existe con AICOS_DEMO_MODE=1)",
    ("GET", "/demo-login/{persona}"): "sesión con cuentas FICTICIAS (solo AICOS_DEMO_MODE=1, loopback y Host local)",
    ("GET", "/api/auth/demo-accounts"): "HALLAZGO V0.7 (P0_AUDITORIA §3): no depende del modo demo y devuelve la contraseña de demostración. V0.7 P1: NO se registra con AICOS_ENV=pilot (404); sigue en local/demo",
    ("POST", "/api/auth/login"): "crea la sesión; limitado por intentos, origen verificado",
    ("POST", "/api/auth/logout"): "borra la sesión presentada, si hay",
    ("POST", "/api/auth/idp/start"): "V0.7 P2: SOLO existe con un proveedor de identidad inyectado (sin él: 404). Crea un flujo (state+PKCE+cookie de un solo uso); el único dato que acepta es el secreto de una invitación (hash de 256 bits) o `reauth` con sesión; los fallos de token se limitan por dirección y responden todos igual",
    ("GET", "/api/auth/callback"): "V0.7 P2: SOLO existe con un proveedor de identidad inyectado (sin él: 404). Navegación GET de redirección del proveedor, exenta de CSRF por ser GET pero atada a state + PKCE + cookie de correlación de UN solo uso; todo fallo = la misma redirección genérica; redirige solo a destinos fijos",
    ("POST", "/api/public/{slug}/chat"): "canal público del cliente; límites persistentes (#13)",
    ("GET", "/api/public/{slug}/outbox"): "canal público: lectura por token de conversación; límites (#13)",
    ("POST", "/api/public/{slug}/visits/confirm"): "canal público: código de visita; límites (#13)",
    ("GET", "/api/setup/info"): "informa si el alta local de empresas está activa; sin datos; NO se registra con AICOS_ENV=pilot (V0.7 P1: 404)",
    ("POST", "/api/setup/companies"): "404 salvo AICOS_ALLOW_COMPANY_CREATION=1 y loopback; NO se registra con AICOS_ENV=pilot (V0.7 P1: 404)",
    ("GET", "/"): "aplicación de una sola página (estáticos)",
}

# Consultas sobre tablas de empresa SIN company_id que son globales A PROPÓSITO. Clave = (RUTA del módulo relativa a backend/app/, SQL COMPLETO normalizado): coincidencia EXACTA, no por prefijo
# (una variante —un `OR`, otra columna, otro módulo con el mismo nombre de archivo— es otra consulta y exige su propia justificación). Si una entrada justifica varias consultas idénticas del
# mismo módulo, su cantidad va en ALLOWED_COUNT; una copia de más se marca como no justificada.
ALLOWED_GLOBAL = {
    # ── autenticación y sesiones: la credencial ES el dato de búsqueda ──
    ("api.py", "SELECT * FROM users WHERE lower(email)=:e AND COALESCE(active,1)=1"): "inicio de sesión: la empresa se DERIVA de la cuenta (el correo es único en toda la base)",
    ("api.py", "UPDATE users SET password_hash=:p WHERE id=:i"): "re-hash tras un inicio de sesión ya verificado; el id sale de la fila autenticada",
    ("company_admin.py", "SELECT 1 AS x FROM users WHERE lower(email)=:e"): "unicidad GLOBAL del correo (a propósito cruza empresas); dos altas distintas (:90 y :135)",
    ("security.py", "SELECT * FROM sessions WHERE token_hash=:h"): "la credencial ES el token (256 bits, solo su hash en la base)",
    ("security.py", "UPDATE sessions SET last_seen_at=:n WHERE token_hash=:h"): "ídem: se actúa sobre la sesión presentada",
    ("security.py", "SELECT csrf FROM sessions WHERE token_hash=:h"): "ídem; dos lecturas (:97 y :102: antes y después de crear el token CSRF perezoso)",
    ("security.py", "UPDATE sessions SET csrf=:x WHERE token_hash=:h AND csrf IS NULL"): "ídem",
    ("security.py", "DELETE FROM sessions WHERE token_hash=:h"): "ídem (cierre de sesión)",
    ("security.py", "DELETE FROM sessions WHERE expires_at<:n OR COALESCE(last_seen_at,created_at)<:i"): "purga global de sesiones vencidas",
    # ── migraciones, copias y herramientas de demostración: operan sobre TODA la base a propósito ──
    ("db.py", "INSERT INTO agents({}) SELECT {} FROM agents_v04"): "migración V0.4→V0.5: copia TODA la tabla en una transacción",
    ("db.py", "UPDATE users SET active=1 WHERE active IS NULL"): "migración aditiva de toda la tabla",
    ("backup.py", "SELECT count(*) FROM users"): "prueba de arranque de una copia: cuenta de toda la base",
    ("backup.py", "SELECT count(*) FROM sessions"): "ídem",
    ("demo_preflight.py", "SELECT COUNT(*) FROM messages WHERE status='enviado'"): "herramienta de demostración: ningún mensaje real enviado, en toda la base",
    ("demo_seed.py", "SELECT 1 AS x FROM audit_log WHERE action='demo.fixture'"): "marca de la fixture de demostración",
    ("demo_seed.py", "SELECT COUNT(*) AS n FROM messages WHERE status='enviado'"): "herramienta de demostración",
    # ── identidad externa (V0.7 P2): la credencial (secreto de invitación, subject del proveedor, sesión) ES el dato de búsqueda; la empresa se DERIVA de la fila encontrada ──
    ("identity_flow.py", "SELECT * FROM invitations WHERE token_hash=:h AND status='pending' AND expires_at>:n"): "el secreto de la invitación (256 bits, solo su hash en la base) ES la clave; la empresa sale de la fila y es la que usan todas las escrituras posteriores",
    ("identity_flow.py", "SELECT * FROM invitations WHERE id=:i"): "_accept: el id sale de un flujo creado por el servidor tras resolver el secreto (nunca del navegador); la empresa se deriva de esta fila y se usa en el UPDATE condicional y el alta",
    ("identity_flow.py", "SELECT 1 AS x FROM identities WHERE provider=:p AND subject=:s"): "unicidad GLOBAL a propósito: una identidad (proveedor, subject) pertenece a UNA persona de UNA empresa en toda la base (PK compuesta)",
    ("identity_flow.py", "SELECT * FROM identities WHERE provider=:p AND subject=:s"): "inicio de sesión posterior: el subject verificado por el proveedor ES la clave; empresa y persona se DERIVAN de la fila (nunca del correo ni del navegador)",
    ("invitations.py", "SELECT 1 AS x FROM users WHERE lower(email)=:e"): "unicidad GLOBAL del correo (a propósito cruza empresas): solo decide si el proveedor escribe a esa dirección; NUNCA se refleja en la respuesta (misma respuesta exista o no)",
    ("security.py", "UPDATE sessions SET auth_time=:a WHERE token_hash=:h"): "ídem: se actúa sobre la sesión presentada (reautenticación con contraseña de la persona de ESA sesión)",
    # ── arranque del modo piloto (V0.7 P1) ──
    ("pilot.py", "SELECT COUNT(*) FROM users WHERE is_demo=1"): "validate_startup ANTES de servir ninguna petición: cuenta las cuentas de demostración de TODA la base, con la base abierta en SOLO LECTURA; no hay empresa que filtrar, la pregunta es si la base entera es apta para el piloto",
    # ── estado de ejecución con ids que genera el servidor ──
    ("events.py", "SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE run_id=:r"): "secuencia por run (ids aleatorios, solo llamadas internas con empresa)",
    ("ratelimit.py", "UPDATE rate_limits SET tat=MAX(tat-:T,:now) WHERE bucket=:b"): "estado de ejecución: el cubo es HMAC(clase|empresa|sujeto), la empresa va DENTRO del hash (#13)",
    ("ratelimit.py", "DELETE FROM rate_limits WHERE tat<=:n"): "purga global de contadores vencidos (#13)",
    ("ratelimit.py", "SELECT bucket,tat FROM rate_limits WHERE bucket IN ({})"): "lectura por nombre de cubo (HMAC con la empresa dentro) (#13)",
    # ── motor de ejecución (PR #18, hallazgo de revisión automatizada): `run_id`/`plan_id` nacen en el servidor (`uid("run_")`) o salen de una fila YA filtrada por empresa (orquestador: decide, resolve, continue_collab,
    #    complete_task; planificador: confirm, _auto_read_only, on_task_event); la empresa se DERIVA de la fila cargada y es la que usan todas las lecturas y escrituras posteriores. Ninguno de estos ids viene de la entrada del usuario ──
    ("engine/orchestrator.py", "SELECT * FROM runs WHERE id=:i"): "ciclo de vida del run (`_execute` :139 y `_fail_run` :155): el run_id lo genera el servidor o viene de una fila filtrada por empresa; `run['company_id']` se deriva de esta fila",
    ("engine/orchestrator.py", "SELECT id,phase FROM runs WHERE status='running'"): "recover_runs: se llama SOLO al ganar el lease (api.py:75) y recorre TODAS las ejecuciones en curso de la instancia a propósito; cada una la cierra `_fail_run`, que deriva la empresa de la fila",
    ("engine/orchestrator.py", "SELECT status FROM runs WHERE id=:i"): "Flow.alive (cerco antes de cada paso): lee solo `status` del PROPIO run (`self.rid`, ya cargado de una fila)",
    ("engine/orchestrator.py", "UPDATE runs SET {},updated_at=:_t WHERE id=:_i{}"): "_set: transición del PROPIO run (`self.rid`), con compare-and-set status='running' si cambia el estado; las columnas salen de llamadas internas literales, nunca de la entrada del usuario",
    ("engine/orchestrator.py", "UPDATE runs SET status='failed',updated_at=:t WHERE id=:i AND status='running'"): "_fail_run: compare-and-set sobre el run leído en la MISMA transacción (:155); una sola fila; la empresa se deriva de ella",
    ("engine/planner.py", "SELECT id FROM plans WHERE status IN ('running','waiting_human')"): "Planner.recover: se llama SOLO al ganar el lease (api.py:76) y recorre TODOS los planes en curso a propósito; `advance` deriva la empresa de la fila del plan y vuelve a filtrar por ella",
    ("tools/plans.py", "SELECT status FROM plan_steps WHERE plan_id=:p AND seq=:s"): "assign_task (:121) y activate_step (:151): `p['id']` sale de `_plan(ctx, …)`, que filtra por id Y company_id=ctx.company_id en la misma función; plan_steps.plan_id acota al plan de esa empresa",
}
# Entradas que justifican más de una consulta idéntica del mismo módulo (por defecto, 1).
ALLOWED_COUNT = {
    ("company_admin.py", "SELECT 1 AS x FROM users WHERE lower(email)=:e"): 2,
    ("security.py", "SELECT csrf FROM sessions WHERE token_hash=:h"): 2,
    ("engine/orchestrator.py", "SELECT * FROM runs WHERE id=:i"): 2,
    ("tools/plans.py", "SELECT status FROM plan_steps WHERE plan_id=:p AND seq=:s"): 2,
}


def _tenant_tables() -> set[str]:
    """Tablas con company_id, leídas del esquema real (no de una lista escrita a mano)."""
    sys.path.insert(0, str(BACKEND))
    try:
        from app.db import md
    finally:
        sys.path.pop(0)
    return {t.name for t in md.sorted_tables if "company_id" in t.c}


def _sql_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def sql_inventory() -> dict:
    tenant = _tenant_tables()
    total, found = 0, []
    app_dir = BACKEND / "app"
    scanned = []
    for f in sorted(p for p in app_dir.rglob("*.py") if "__pycache__" not in p.parts):
        rel = f.relative_to(app_dir).as_posix()                                                                  # "api.py", "engine/planner.py": la clave de ALLOWED_GLOBAL
        scanned.append(rel)
        tree = ast.parse(f.read_text())
        inside_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}      # los trozos de un f-string no son consultas aparte
        for n in ast.walk(tree):
            if id(n) in inside_fstring:
                continue
            s = _sql_text(n)
            if s is None or not re.match(r"\s*(SELECT|UPDATE|DELETE|INSERT)\b", s, re.I):
                continue
            norm = " ".join(s.split())
            tabs = set(re.findall(r"(?:FROM|JOIN|UPDATE|INTO)\s+([a-z_]+)", norm, re.I)) & tenant
            if not tabs:
                continue
            total += 1
            if "company_id" in norm:
                continue
            found.append({"file": rel, "line": n.lineno, "tables": sorted(tabs), "sql": norm})
    unfiltered, used = [], {}
    for u in sorted(found, key=lambda r: (r["file"], r["line"])):                                                 # por línea: si sobra una copia idéntica, se marca la ÚLTIMA
        key = (u["file"], u["sql"])
        if key not in ALLOWED_GLOBAL:
            unfiltered.append({**u, "allowed": False, "why": None})
            continue
        used[key] = used.get(key, 0) + 1
        ok = used[key] <= ALLOWED_COUNT.get(key, 1)
        unfiltered.append({**u, "allowed": ok, "why": ALLOWED_GLOBAL[key] if ok else f"SOBRA: esta entrada justifica {ALLOWED_COUNT.get(key, 1)} consulta(s) idéntica(s) en el módulo y hay más"})
    return {"tenant_tables": sorted(tenant), "files_scanned": scanned, "statements_on_tenant_tables": total, "allowlist_usage": {f"{k[0]} :: {k[1]}": n for k, n in used.items()}, "without_company_id": unfiltered,
            "unjustified": [u for u in unfiltered if not u["allowed"]]}


def route_inventory() -> list[dict]:
    src = (BACKEND / "app" / "api.py").read_text()
    lines = src.splitlines()
    rows = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post", "put", "patch", "delete")
                    and getattr(d.func.value, "id", "") == "app" and d.args and isinstance(d.args[0], ast.Constant)):
                body = "\n".join(lines[n.lineno - 1:n.end_lineno])
                session = "Depends(current_user)" in body[:600] or "user=Depends(current_user)" in body
                perms = sorted(set(re.findall(r'need\(user,\s*"([a-z_.]+)"\)', body)))
                inline = sorted(set(re.findall(r'user\["role"\]\s*(?:!=|==)\s*"([a-z]+)"', body)))
                if perms:
                    control = "permiso: " + ", ".join(perms)
                elif inline:
                    control = "rol (en el cuerpo): " + ", ".join(inline)
                elif session:
                    control = "solo sesión (el cuerpo filtra por la empresa/usuario de la sesión)"
                else:
                    control = "anónima"
                rows.append({"method": d.func.attr.upper(), "path": d.args[0].value, "session": session, "control": control, "line": n.lineno})
    return sorted(rows, key=lambda r: r["line"])


def audit() -> dict:
    routes, sql = route_inventory(), sql_inventory()
    anon = [r for r in routes if not r["session"]]
    unknown_anon = [r for r in anon if (r["method"], r["path"]) not in ANONYMOUS_ROUTES]
    return {"routes": routes, "anonymous": anon, "anonymous_unknown": unknown_anon, "session_only": [r for r in routes if r["session"] and r["control"].startswith("solo sesión")], "sql": sql}


def _print(a: dict) -> None:
    rs = a["routes"]
    print(f"RUTAS: {len(rs)} · con sesión: {sum(r['session'] for r in rs)} · anónimas: {len(a['anonymous'])} · anónimas NO documentadas: {len(a['anonymous_unknown'])}")
    print("\nAnónimas:")
    for r in a["anonymous"]:
        print(f"  {r['method']:5} {r['path']:42} {ANONYMOUS_ROUTES.get((r['method'], r['path']), '¡NO DOCUMENTADA!')}")
    print("\nCon sesión pero sin permiso ni rol explícito (el cuerpo debe filtrar por la sesión):")
    for r in a["session_only"]:
        print(f"  {r['method']:5} {r['path']}")
    s = a["sql"]
    print(f"\nSQL sobre tablas con company_id: {s['statements_on_tenant_tables']} consultas · sin company_id: {len(s['without_company_id'])} · SIN justificar: {len(s['unjustified'])}")
    for u in s["without_company_id"]:
        print(f"  {u['file']}:{u['line']:<4} {'OK ' if u['allowed'] else '¡¡ '} {u['sql'][:90]}   {'— ' + u['why'] if u['why'] else ''}")


if __name__ == "__main__":
    result = audit()
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        _print(result)
    bad = bool(result["anonymous_unknown"] or result["sql"]["unjustified"])
    if "--check" in sys.argv and bad:
        print("\nFALLO: superficie anónima o consulta de empresa sin justificar (ver arriba)", file=sys.stderr)
        sys.exit(1)
