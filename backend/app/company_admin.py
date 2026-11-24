"""Company configuration (V0.5): create a fictitious company, add employees with roles, assign personal-assistant templates, enable/disable assignments.

Everything is plain DB configuration (no source edits, no JSON files) and every change is AUDITED. Hard rules:
- only the company's OWNER administers its employees/assignments (checked here AND in the API); nothing crosses companies;
- no self-elevation: nobody changes their own role or deactivates themselves; employees can only be `admin` or `technician` (one owner, created with the company);
- configuring an assistant never grants permissions (effective = template ∩ the employee's role);
- creating a company is a LOCAL/DEMO action (API gated by AICOS_ALLOW_COMPANY_CREATION + loopback; CLI `python -m app.company_admin`) — never a public sign-up, never real data.
CLI:  python -m app.company_admin create --name "Aire Austral" --owner-name "Elena Rojas" --owner-email elena@aireaustral.test [--preset hvac] [--db path]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys

from sqlalchemy.engine import Engine

from .db import all_, jdump, jload, make_engine, now, one, q, uid
from .directory import ROLE_BY_TEMPLATE, TEMPLATE_BY_ROLE
from .events import audit
from .permissions import AGENT_META, AGENT_PERMISSIONS
from .security import hash_password
from .seed import COMPANIES, add_slots, init_db

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,30}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[^@\s]{2,}$")
EMPLOYEE_ROLES = ("admin", "technician")
MAX_COMPANIES = 25
MAX_EMPLOYEES = 40
PRESETS = {"hvac": ("clima-sur", "Servicio técnico de aire acondicionado"), "taller": ("motor-norte", "Taller mecánico"), "generico": (None, "Servicios técnicos")}
GENERIC_SERVICES = [("DIAG", "diagnostico", "Visita de diagnóstico", 25000), ("MANT", "mantencion", "Mantención preventiva", 35000), ("INSTAL", "instalacion", "Instalación", 90000)]


class ConfigError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def slugify(name: str) -> str:
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFD", name.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:30] or "empresa"


def _check_person(name: str, email: str, password: str | None) -> tuple[str, str]:
    name, email = " ".join((name or "").split()), (email or "").strip().lower()
    if not (2 <= len(name) <= 80):
        raise ConfigError("el nombre debe tener entre 2 y 80 caracteres")
    if not EMAIL_RE.match(email):
        raise ConfigError("correo inválido")
    if password is not None and not (8 <= len(password) <= 100):
        raise ConfigError("la contraseña debe tener entre 8 y 100 caracteres")
    return name, email


def _new_agent(c, company_id: str, template: str, user_id: str | None) -> str:
    nm, desc, _ = AGENT_META[template]
    aid = uid("agt_")
    q(c, "INSERT INTO agents(id,company_id,role,name,description,user_id,permissions,config,enabled) VALUES(:i,:c,:r,:n,:d,:u,:p,:cf,1)",
      i=aid, c=company_id, r=template, n=nm, d=desc, u=user_id, p=jdump(sorted(AGENT_PERMISSIONS[template])), cf=jdump({"provider": "configurable", "assigned_from_template": True}))
    return aid


# ---------------------------------------------------------------- company
def create_company(eng: Engine, *, name: str, owner_name: str, owner_email: str, slug: str | None = None, industry: str | None = None, preset: str = "hvac",
                   owner_password: str | None = None, actor: str = "system") -> dict:
    """New FICTITIOUS company: owner + shared agents (reception, coordinator) + the owner's assistant + a starter catalogue (services/parts) from a preset."""
    name = " ".join((name or "").split())
    if not (3 <= len(name) <= 80):
        raise ConfigError("el nombre de la empresa debe tener entre 3 y 80 caracteres")
    slug = (slug or slugify(name)).strip().lower()
    if not SLUG_RE.match(slug):
        raise ConfigError("slug inválido (3–31 caracteres: minúsculas, números y guiones)")
    if preset not in PRESETS:
        raise ConfigError(f"plantilla de industria desconocida: {preset}")
    owner_name, owner_email = _check_person(owner_name, owner_email, owner_password)
    password = owner_password or secrets.token_urlsafe(9)
    src_slug, default_industry = PRESETS[preset]
    cat = COMPANIES[src_slug] if src_slug else {"parts": [], "services": GENERIC_SERVICES}
    init_db(eng)
    with eng.begin() as c:
        if one(c, "SELECT COUNT(*) AS n FROM companies")["n"] >= MAX_COMPANIES:
            raise ConfigError("límite de empresas de este entorno local alcanzado", 409)
        if one(c, "SELECT 1 AS x FROM companies WHERE slug=:s", s=slug):
            raise ConfigError("ya existe una empresa con ese identificador", 409)
        if one(c, "SELECT 1 AS x FROM users WHERE lower(email)=:e", e=owner_email):
            raise ConfigError("ya existe una cuenta con ese correo", 409)
        cid, oid = uid("co_"), uid("usr_")
        q(c, "INSERT INTO companies(id,slug,name,industry,created_at) VALUES(:i,:s,:n,:in,:t)", i=cid, s=slug, n=name, **{"in": (industry or default_industry)}, t=now())
        q(c, "INSERT INTO users(id,company_id,email,name,role,password_hash,is_demo,created_at,active) VALUES(:i,:c,:e,:n,'owner',:p,0,:t,1)", i=oid, c=cid, e=owner_email, n=owner_name, p=hash_password(password), t=now())
        for tpl in ("reception", "coordinator"):
            _new_agent(c, cid, tpl, None)
        _new_agent(c, cid, "owner_assistant", oid)
        for sku, nm, st, pr, alt in cat["parts"]:
            q(c, "INSERT INTO parts(id,company_id,sku,name,stock,unit_price,alt_sku) VALUES(:i,:c,:s,:n,:st,:p,:a)", i=uid("prt_"), c=cid, s=sku, n=nm, st=st, p=pr, a=alt)
        for code, cg, nm, pr in cat["services"]:
            q(c, "INSERT INTO services(id,company_id,code,category,name,price) VALUES(:i,:c,:cd,:ca,:n,:p)", i=uid("svc_"), c=cid, cd=code, ca=cg, n=nm, p=pr)
        for kind, nm in [("whatsapp", "WhatsApp Business"), ("email", "Correo electrónico"), ("calendar", "Calendario"), ("voice", "Telefonía / voz"), ("payments", "Pagos")]:
            q(c, "INSERT INTO integrations(id,company_id,kind,name,mode,enabled,note) VALUES(:i,:c,:k,:n,'simulado',0,'No conectado en esta versión (entorno local)')", i=uid("int_"), c=cid, k=kind, n=nm)
        audit(c, cid, "system" if actor == "system" else "user", actor, "config.company_create", cid, "ok", jdump({"slug": slug, "preset": preset}))
    return {"company_id": cid, "slug": slug, "name": name, "owner_email": owner_email, "owner_password": None if owner_password else password, "preset": preset,
            "note": "Empresa FICTICIA de entorno local. La contraseña generada solo se muestra ahora." if not owner_password else "Empresa FICTICIA de entorno local."}


# ---------------------------------------------------------------- employees & assignments (owner only)
def _owner(c, company_id: str, actor: dict) -> None:
    if actor["company_id"] != company_id or actor["role"] != "owner":
        raise ConfigError("solo el dueño de la empresa puede configurar empleados y asistentes", 403)


def _user(c, company_id: str, user_id: str) -> dict:
    u = one(c, "SELECT id,name,email,role,COALESCE(active,1) AS active FROM users WHERE id=:i AND company_id=:c", i=user_id, c=company_id)
    if not u:
        raise ConfigError("empleado no encontrado", 404)   # same answer for «missing» and «other company»
    return u


def _open_tasks(c, company_id: str, user_id: str) -> int:
    return one(c, "SELECT COUNT(*) AS n FROM tasks WHERE company_id=:c AND assignee_user_id=:u AND status IN ('pendiente','en_espera')", c=company_id, u=user_id)["n"]


def insert_employee(c, company_id: str, actor_id: str, *, name: str, email: str, role: str, password_hash: str, assign_assistant: bool = True, via: str = "owner") -> tuple[str, str | None]:
    """Alta de UN empleado dentro de la transacción del llamador (límite de plantilla, correo único en toda la base, asistente personal y turnos del técnico, auditoría). Compartido por el alta directa
    del dueño y por la aceptación de una invitación (V0.7 P2): una sola regla. → (id del usuario, id del asistente o None)."""
    if one(c, "SELECT COUNT(*) AS n FROM users WHERE company_id=:c", c=company_id)["n"] >= MAX_EMPLOYEES:
        raise ConfigError("límite de empleados alcanzado", 409)
    if one(c, "SELECT 1 AS x FROM users WHERE lower(email)=:e", e=email):
        raise ConfigError("ya existe una cuenta con ese correo", 409)
    uid_ = uid("usr_")
    q(c, "INSERT INTO users(id,company_id,email,name,role,password_hash,is_demo,created_at,active) VALUES(:i,:c,:e,:n,:r,:p,0,:t,1)", i=uid_, c=company_id, e=email, n=name, r=role, p=password_hash, t=now())
    agent_id = _new_agent(c, company_id, TEMPLATE_BY_ROLE[role], uid_) if assign_assistant else None
    slots = add_slots(c, company_id, uid_) if role == "technician" else 0
    audit(c, company_id, "user", actor_id, "config.employee_add", uid_, "ok", jdump({"role": role, "assistant": bool(agent_id), "slots": slots, **({"via": via} if via != "owner" else {})}))
    return uid_, agent_id


def add_employee(eng: Engine, company_id: str, actor: dict, *, name: str, email: str, role: str, password: str | None = None, assign_assistant: bool = True) -> dict:
    name, email = _check_person(name, email, password)
    if role not in EMPLOYEE_ROLES:
        raise ConfigError("el rol debe ser «admin» (administración) o «technician» (técnico)")
    pw = password or secrets.token_urlsafe(9)
    with eng.begin() as c:
        _owner(c, company_id, actor)
        uid_, agent_id = insert_employee(c, company_id, actor["id"], name=name, email=email, role=role, password_hash=hash_password(pw), assign_assistant=assign_assistant)
    return {"user_id": uid_, "name": name, "email": email, "role": role, "agent_id": agent_id, "password": None if password else pw}


def assign_template(eng: Engine, company_id: str, actor: dict, *, user_id: str, template: str) -> dict:
    """Assign a PERSONAL assistant template to an employee (idempotent; re-enables a disabled assignment)."""
    if template not in ROLE_BY_TEMPLATE:
        raise ConfigError("plantilla desconocida o compartida: solo se asignan asistentes personales (owner/admin/tech)")
    with eng.begin() as c:
        _owner(c, company_id, actor)
        u = _user(c, company_id, user_id)
        if u["role"] != ROLE_BY_TEMPLATE[template]:
            raise ConfigError(f"la plantilla «{template}» es para el rol {ROLE_BY_TEMPLATE[template]}; {u['name']} es {u['role']}")
        if not u["active"]:
            raise ConfigError("el empleado está desactivado")
        ex = one(c, "SELECT id,enabled FROM agents WHERE company_id=:c AND user_id=:u AND role=:t", c=company_id, u=user_id, t=template)
        if ex:
            if ex["enabled"]:
                return {"agent_id": ex["id"], "deduplicated": True}
            q(c, "UPDATE agents SET enabled=1 WHERE id=:i AND company_id=:c", i=ex["id"], c=company_id)
            audit(c, company_id, "user", actor["id"], "config.assignment_enable", ex["id"], "ok", jdump({"user": user_id, "template": template}))
            return {"agent_id": ex["id"], "deduplicated": False}
        aid = _new_agent(c, company_id, template, user_id)
        audit(c, company_id, "user", actor["id"], "config.assignment_add", aid, "ok", jdump({"user": user_id, "template": template}))
    return {"agent_id": aid, "deduplicated": False}


def set_agent_enabled(eng: Engine, company_id: str, actor: dict, *, agent_id: str, enabled: bool) -> dict:
    with eng.begin() as c:
        _owner(c, company_id, actor)
        a = one(c, "SELECT id,role,user_id,enabled FROM agents WHERE id=:i AND company_id=:c", i=agent_id, c=company_id)
        if not a:
            raise ConfigError("agente no encontrado", 404)
        if bool(a["enabled"]) == enabled:
            return {"agent_id": agent_id, "enabled": enabled, "deduplicated": True}
        if enabled and a["user_id"]:   # same template ↔ role rule as assign_template(): a stale assistant of the employee's PREVIOUS role can never be re-enabled
            u = _user(c, company_id, a["user_id"])
            if ROLE_BY_TEMPLATE.get(a["role"]) != u["role"]:
                raise ConfigError(f"la plantilla «{a['role']}» es para el rol {ROLE_BY_TEMPLATE.get(a['role'])}; {u['name']} es {u['role']}")
        if not enabled and a["user_id"] and _open_tasks(c, company_id, a["user_id"]):
            raise ConfigError(f"el empleado tiene {_open_tasks(c, company_id, a['user_id'])} tarea(s) abierta(s): ciérralas o cancela el plan antes de desactivar su asistente", 409)
        q(c, "UPDATE agents SET enabled=:e WHERE id=:i AND company_id=:c", e=int(enabled), i=agent_id, c=company_id)
        audit(c, company_id, "user", actor["id"], "config.assignment_enable" if enabled else "config.assignment_disable", agent_id, "ok", jdump({"template": a["role"], "user": a["user_id"]}))
    return {"agent_id": agent_id, "enabled": enabled, "deduplicated": False}


def set_user_active(eng: Engine, company_id: str, actor: dict, *, user_id: str, active: bool) -> dict:
    with eng.begin() as c:
        _owner(c, company_id, actor)
        u = _user(c, company_id, user_id)
        if u["id"] == actor["id"]:
            raise ConfigError("no puedes desactivarte a ti mismo/a", 403)
        if u["role"] == "owner":
            raise ConfigError("no se puede desactivar al dueño de la empresa", 403)
        if bool(u["active"]) == active:
            return {"user_id": user_id, "active": active, "deduplicated": True}
        if not active and _open_tasks(c, company_id, user_id):
            raise ConfigError(f"{u['name']} tiene {_open_tasks(c, company_id, user_id)} tarea(s) abierta(s): ciérralas o cancela el plan antes de desactivar", 409)
        q(c, "UPDATE users SET active=:a WHERE id=:i AND company_id=:c", a=int(active), i=user_id, c=company_id)
        if not active:
            q(c, "DELETE FROM sessions WHERE user_id=:u AND company_id=:c", u=user_id, c=company_id)   # kicks them out at once
        audit(c, company_id, "user", actor["id"], "config.employee_activate" if active else "config.employee_deactivate", user_id, "ok", "")
    return {"user_id": user_id, "active": active, "deduplicated": False}


def change_role(eng: Engine, company_id: str, actor: dict, *, user_id: str, role: str) -> dict:
    """admin ⇄ technician. The old assistant is disabled and the right one assigned. Never to/from owner, never for yourself, never with open tasks."""
    if role not in EMPLOYEE_ROLES:
        raise ConfigError("rol inválido: solo «admin» o «technician»")
    with eng.begin() as c:
        _owner(c, company_id, actor)
        u = _user(c, company_id, user_id)
        if u["id"] == actor["id"] or u["role"] == "owner":
            raise ConfigError("no se puede cambiar el rol del dueño ni el propio (sin autoelevación)", 403)
        if u["role"] == role:
            return {"user_id": user_id, "role": role, "deduplicated": True}
        n = _open_tasks(c, company_id, user_id)
        if n:
            raise ConfigError(f"{u['name']} tiene {n} tarea(s) abierta(s): ciérralas o cancela el plan antes de cambiar su rol", 409)
        q(c, "UPDATE agents SET enabled=0 WHERE company_id=:c AND user_id=:u", c=company_id, u=user_id)
        q(c, "UPDATE users SET role=:r WHERE id=:i AND company_id=:c", r=role, i=user_id, c=company_id)
        tpl = TEMPLATE_BY_ROLE[role]
        ex = one(c, "SELECT id FROM agents WHERE company_id=:c AND user_id=:u AND role=:t", c=company_id, u=user_id, t=tpl)
        if ex:
            q(c, "UPDATE agents SET enabled=1 WHERE id=:i AND company_id=:c", i=ex["id"], c=company_id)
        else:
            _new_agent(c, company_id, tpl, user_id)
        if role == "technician" and not one(c, "SELECT 1 AS x FROM availability WHERE company_id=:c AND technician_id=:u", c=company_id, u=user_id):
            add_slots(c, company_id, user_id)
        audit(c, company_id, "user", actor["id"], "config.role_change", user_id, "ok", jdump({"from": u["role"], "to": role}))
    return {"user_id": user_id, "role": role, "deduplicated": False}


def overview(eng: Engine, company_id: str) -> dict:
    with eng.connect() as c:
        co = one(c, "SELECT id,slug,name,industry FROM companies WHERE id=:i", i=company_id)
        users = all_(c, "SELECT id,name,email,role,COALESCE(active,1) AS active,is_demo FROM users WHERE company_id=:c ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, name", c=company_id)
        agents = all_(c, "SELECT id,role,name,user_id,enabled FROM agents WHERE company_id=:c", c=company_id)
        work = {r["u"]: r["n"] for r in all_(c, "SELECT assignee_user_id AS u,COUNT(*) AS n FROM tasks WHERE company_id=:c AND status IN ('pendiente','en_espera') GROUP BY assignee_user_id", c=company_id)}
    for u in users:
        u["assistants"] = [{"agent_id": a["id"], "template": a["role"], "enabled": bool(a["enabled"]), "compatible": ROLE_BY_TEMPLATE.get(a["role"]) == u["role"]} for a in agents if a["user_id"] == u["id"]]
        u["open_tasks"] = work.get(u["id"], 0)
        u["eligible"] = bool(u["active"]) and any(a["enabled"] and a["role"] == TEMPLATE_BY_ROLE.get(u["role"]) for a in agents if a["user_id"] == u["id"])
        u["is_demo"] = bool(u["is_demo"])
    shared = [{"agent_id": a["id"], "template": a["role"], "name": a["name"], "enabled": bool(a["enabled"])} for a in agents if a["user_id"] is None]
    return {"company": co, "employees": users, "shared_agents": shared, "limits": {"max_employees": MAX_EMPLOYEES}}


# ---------------------------------------------------------------- CLI
def _cli_engine(db: str):
    """El motor de una operación LOCAL del operador sobre una base que YA existe (nunca se crea una vacía por un error al teclear la ruta)."""
    if not db:
        raise ConfigError("indica --db o AICOS_DB_URL")
    url = db if db.startswith("sqlite") else f"sqlite:///{db}"
    path = url[len("sqlite:///"):].split("?")[0] if url.startswith("sqlite:///") else ""
    if path and path != ":memory:" and not os.path.exists(path):
        raise ConfigError(f"la base de datos no existe: {path}")
    eng = make_engine(url)
    init_db(eng)                                                            # migración aditiva (una base de V0.7 P2 aún no tiene las columnas de P2.5)
    return eng


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Configuración local de empresas ficticias (sin editar código)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_db = os.environ.get("AICOS_DB_URL", "")
    c = sub.add_parser("create", help="crear una empresa ficticia con su dueño")
    for a in ("--name", "--owner-name", "--owner-email"):
        c.add_argument(a, required=True)
    c.add_argument("--slug"); c.add_argument("--industry"); c.add_argument("--preset", default="hvac", choices=list(PRESETS)); c.add_argument("--owner-password")
    c.add_argument("--db", default=default_db, help="URL o ruta SQLite (por defecto AICOS_DB_URL)")
    lk = sub.add_parser("link-owner", help="V0.7 P2.5: emite el secreto de UN uso con el que el dueño EXISTENTE vincula su identidad del proveedor (se muestra una sola vez)")
    lk.add_argument("--company", required=True, help="slug de la empresa"); lk.add_argument("--ttl-minutes", type=int, default=60, help="vigencia (5–1440; por defecto 60)")
    lk.add_argument("--db", default=default_db)
    rv = sub.add_parser("revoke-owner-link", help="revoca el secreto pendiente de vínculo del dueño")
    rv.add_argument("--company", required=True); rv.add_argument("--db", default=default_db)
    ul = sub.add_parser("unlink-owner", help="quita la identidad vinculada del dueño y cierra sus sesiones (auditado; hace falta para vincular otra)")
    ul.add_argument("--company", required=True); ul.add_argument("--yes", action="store_true", help="confirmar"); ul.add_argument("--db", default=default_db)
    a = ap.parse_args(argv)
    if a.cmd == "create":
        url = a.db if a.db.startswith("sqlite") else f"sqlite:///{a.db}"
        if not a.db:
            print("ERROR: indica --db o AICOS_DB_URL", file=sys.stderr); return 2
        eng = make_engine(url)
        try:
            r = create_company(eng, name=a.name, slug=a.slug, industry=a.industry, preset=a.preset, owner_name=a.owner_name, owner_email=a.owner_email, owner_password=a.owner_password, actor="cli")
        except ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr); return 1
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    from . import invitations                                                # solo las operaciones de vínculo lo necesitan (y NO arrastran ningún proveedor)
    try:
        if a.cmd == "unlink-owner" and not a.yes:
            raise ConfigError("desvincular cierra las sesiones del dueño y exige confirmarlo: repite con --yes")
        eng = _cli_engine(a.db)
        if a.cmd == "link-owner":
            r = invitations.owner_link_create(eng, slug=a.company, ttl_minutes=a.ttl_minutes, actor="cli")
            print("AVISO: el secreto se muestra UNA sola vez; entrégalo al dueño por un canal seguro. Solo sirve con el correo exacto del dueño.", file=sys.stderr)
        elif a.cmd == "revoke-owner-link":
            r = invitations.owner_link_revoke(eng, slug=a.company, actor="cli")
        else:
            r = invitations.owner_unlink(eng, slug=a.company, actor="cli")
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 1
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
