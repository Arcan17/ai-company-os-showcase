"""V0.7 P2 (Issue #23) — invitaciones. El dueño de una empresa (`config.manage`) invita a UNA persona con correo + nombre + rol; la invitación fija empresa, correo y rol (nunca el proveedor ni el navegador),
caduca a los 7 días, se usa UNA vez, se puede revocar y hay una sola vigente por correo y empresa (índice parcial en la base: también a prueba de carreras).
El secreto del enlace se devuelve UNA sola vez y en la base solo existe su hash SHA-256. Todo cambio queda auditado en la empresa, sin el correo ni el secreto.
No se envía ningún correo desde aquí: el enlace lo entrega el dueño; el proveedor de identidad (cuando exista) enviará el suyo."""
from __future__ import annotations

import secrets
from datetime import timedelta

from . import clock
from .company_admin import EMPLOYEE_ROLES, MAX_EMPLOYEES, ConfigError, _check_person, _owner
from .db import all_, jdump, now, one, q, uid
from .events import audit
from .security import _h as token_hash
from .security import email_key

INVITE_DAYS = 7
PUBLIC_FIELDS = "id,email_key,name,role,status,created_at,expires_at,accepted_at,revoked_at"


def _public(row: dict, n: str) -> dict:
    out = {"id": row["id"], "email": row["email_key"], "name": row["name"], "role": row["role"], "status": row["status"], "created_at": row["created_at"], "expires_at": row["expires_at"],
           "accepted_at": row["accepted_at"], "revoked_at": row["revoked_at"]}
    if out["status"] == "pending" and out["expires_at"] <= n:
        out["status"] = "expired"
    return out


def create(eng, company_id: str, actor: dict, *, email: str, name: str, role: str) -> tuple[dict, str | None]:
    """→ (invitación con su secreto, correo al que el proveedor debe escribir o None). Si el correo ya pertenece a OTRA empresa NO se escribe y la respuesta es idéntica: no se revela quién es cliente de quién."""
    name, email = _check_person(name, email, None)
    if role not in EMPLOYEE_ROLES:
        raise ConfigError("el rol debe ser «admin» (administración) o «technician» (técnico)")
    ekey = email_key(email)
    token, iid = secrets.token_urlsafe(32), uid("inv_")
    exp = (clock.utcnow() + timedelta(days=INVITE_DAYS)).isoformat(timespec="milliseconds")
    with eng.begin() as c:
        _owner(c, company_id, actor)
        n = now()
        q(c, "UPDATE invitations SET status='expired' WHERE company_id=:c AND email_key=:e AND status='pending' AND expires_at<=:n", c=company_id, e=ekey, n=n)   # una vencida no bloquea una nueva
        if one(c, "SELECT 1 AS x FROM invitations WHERE company_id=:c AND email_key=:e AND status='pending'", c=company_id, e=ekey):
            raise ConfigError("ya hay una invitación vigente para ese correo: revócala antes de crear otra", 409)
        if one(c, "SELECT 1 AS x FROM users WHERE company_id=:c AND lower(email)=:e", c=company_id, e=ekey):
            raise ConfigError("ya existe un empleado con ese correo en tu empresa", 409)
        if one(c, "SELECT COUNT(*) AS n FROM invitations WHERE company_id=:c AND status='pending' AND expires_at>:n", c=company_id, n=n)["n"] >= MAX_EMPLOYEES:
            raise ConfigError("demasiadas invitaciones vigentes: revoca alguna", 409)
        elsewhere = one(c, "SELECT 1 AS x FROM users WHERE lower(email)=:e", e=ekey) is not None
        q(c, "INSERT INTO invitations(id,company_id,email_key,name,role,token_hash,status,created_by,created_at,expires_at,kind) VALUES(:i,:c,:e,:n,:r,:h,'pending',:u,:t,:x,'employee')",
          i=iid, c=company_id, e=ekey, n=name, r=role, h=token_hash(token), u=actor["id"], t=n, x=exp)
        audit(c, company_id, "user", actor["id"], "config.invitation_create", iid, "ok", jdump({"role": role}))
        row = one(c, f"SELECT {PUBLIC_FIELDS} FROM invitations WHERE id=:i AND company_id=:c", i=iid, c=company_id)
    return {**_public(row, n), "invite_token": token}, (None if elsewhere else ekey)


def listing(eng, company_id: str) -> list[dict]:
    with eng.connect() as c:
        rows = all_(c, f"SELECT {PUBLIC_FIELDS} FROM invitations WHERE company_id=:c AND COALESCE(kind,'employee')='employee' ORDER BY created_at DESC, id LIMIT 100", c=company_id)
    n = now()
    return [_public(r, n) for r in rows]


def revoke(eng, company_id: str, actor: dict, invitation_id: str) -> dict:
    with eng.begin() as c:
        _owner(c, company_id, actor)
        inv = one(c, "SELECT id,status FROM invitations WHERE id=:i AND company_id=:c AND COALESCE(kind,'employee')='employee'", i=invitation_id, c=company_id)   # el vínculo del dueño NO se ve ni se toca desde la API
        if not inv:
            raise ConfigError("invitación no encontrada", 404)                 # la misma respuesta para «no existe» y «es de otra empresa»
        if inv["status"] == "revoked":
            return {"id": inv["id"], "status": "revoked", "deduplicated": True}
        if inv["status"] == "accepted":
            raise ConfigError("la invitación ya fue aceptada: desactiva a la persona desde Configuración", 409)
        q(c, "UPDATE invitations SET status='revoked', revoked_at=:n WHERE id=:i AND company_id=:c AND status IN ('pending','expired')", n=now(), i=inv["id"], c=company_id)
        audit(c, company_id, "user", actor["id"], "config.invitation_revoke", inv["id"], "ok", "")
    return {"id": invitation_id, "status": "revoked", "deduplicated": False}


# ───────────────────────────── V0.7 P2.5: vincular a un DUEÑO EXISTENTE (operación LOCAL del operador, vía la CLI de company_admin) ─────────────────────────────
# Ni una ruta HTTP, ni el navegador, ni el proveedor pueden iniciar esto: hace falta acceso al archivo de la base (el mismo permiso que ya crea empresas y dueños). El secreto es de un solo uso, caduca pronto,
# solo existe como hash y se revoca; NO crea ninguna persona (el dueño ya existe) y el proveedor solo confirma el correo exacto del dueño. Un segundo vínculo exige desvincular antes, con auditoría.
OWNER_LINK_MIN_MINUTES, OWNER_LINK_MAX_MINUTES, OWNER_LINK_DEFAULT_MINUTES = 5, 1440, 60


def _company_and_owner(c, slug: str) -> tuple[dict, dict | None]:
    co = one(c, "SELECT id,slug FROM companies WHERE slug=:s", s=(slug or "").strip().lower())
    if not co:
        raise ConfigError("empresa no encontrada", 404)
    return co, one(c, "SELECT id,name,email,COALESCE(active,1) AS active FROM users WHERE company_id=:c AND role='owner'", c=co["id"])


def owner_link_create(eng, *, slug: str, ttl_minutes: int = OWNER_LINK_DEFAULT_MINUTES, actor: str = "cli") -> dict:
    if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int) or not OWNER_LINK_MIN_MINUTES <= ttl_minutes <= OWNER_LINK_MAX_MINUTES:
        raise ConfigError(f"la vigencia debe estar entre {OWNER_LINK_MIN_MINUTES} y {OWNER_LINK_MAX_MINUTES} minutos")
    token, iid = secrets.token_urlsafe(32), uid("inv_")
    exp = (clock.utcnow() + timedelta(minutes=ttl_minutes)).isoformat(timespec="milliseconds")
    with eng.begin() as c:
        co, owner = _company_and_owner(c, slug)
        if not owner or not owner["active"]:
            raise ConfigError("la empresa no tiene un dueño activo que vincular", 409)
        if one(c, "SELECT 1 AS x FROM identities WHERE user_id=:u AND company_id=:c", u=owner["id"], c=co["id"]):
            raise ConfigError("el dueño ya tiene una identidad vinculada: desvincula antes al dueño con unlink-owner (queda auditado)", 409)
        n = now()
        q(c, "UPDATE invitations SET status='expired' WHERE company_id=:c AND kind='owner_link' AND status='pending' AND expires_at<=:n", c=co["id"], n=n)
        if one(c, "SELECT 1 AS x FROM invitations WHERE company_id=:c AND kind='owner_link' AND status='pending'", c=co["id"]):
            raise ConfigError("ya hay un enlace pendiente para este dueño: revócalo antes con revoke-owner-link", 409)
        q(c, "INSERT INTO invitations(id,company_id,email_key,name,role,token_hash,status,created_by,created_at,expires_at,kind,target_user_id) VALUES(:i,:c,:e,:n,'owner',:h,'pending',:u,:t,:x,'owner_link',:o)",
          i=iid, c=co["id"], e=owner["email"].strip().lower(), n=owner["name"], h=token_hash(token), u=actor, t=n, x=exp, o=owner["id"])
        audit(c, co["id"], "system", actor, "auth.owner_link_create", iid, "ok", jdump({"ttl_minutes": ttl_minutes}))
    return {"company": co["slug"], "owner_email": owner["email"].strip().lower(), "token": token, "expires_at": exp, "invitation_id": iid}


def owner_link_revoke(eng, *, slug: str, actor: str = "cli") -> dict:
    with eng.begin() as c:
        co, _ = _company_and_owner(c, slug)
        n = q(c, "UPDATE invitations SET status='revoked', revoked_at=:n WHERE company_id=:c AND kind='owner_link' AND status IN ('pending','expired')", n=now(), c=co["id"]).rowcount
        if n:
            audit(c, co["id"], "system", actor, "auth.owner_link_revoke", "-", "ok", "")
    return {"revoked": n}


def owner_unlink(eng, *, slug: str, actor: str = "cli") -> dict:
    """Revoca el enlace PENDIENTE (aunque no haya identidad vinculada), quita la identidad del dueño y cierra sus sesiones. No habla con el proveedor: dar de baja allí la identidad es cosa del operador.
    Sin identidad ni enlace pendiente no cambia nada ni deja auditoría."""
    with eng.begin() as c:
        co, owner = _company_and_owner(c, slug)
        links = q(c, "UPDATE invitations SET status='revoked', revoked_at=:n WHERE company_id=:c AND kind='owner_link' AND status='pending'", n=now(), c=co["id"]).rowcount
        n = s = 0
        if owner:
            n = q(c, "DELETE FROM identities WHERE user_id=:u AND company_id=:c", u=owner["id"], c=co["id"]).rowcount
            if n:
                s = q(c, "DELETE FROM sessions WHERE user_id=:u AND company_id=:c", u=owner["id"], c=co["id"]).rowcount
        if n or links:
            audit(c, co["id"], "system", actor, "auth.owner_unlink", owner["id"] if owner else "-", "ok", jdump({"identities": n, "sessions": s, "links": links}))
    return {"unlinked": n, "sessions_revoked": s, "links_revoked": links}
