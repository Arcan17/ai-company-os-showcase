"""V0.7 P2 (Issue #23) — el flujo de acceso con un proveedor de identidad: inicio (state + PKCE S256 + nonce + cookie de correlación, de un solo uso y 10 minutos) y callback.

Reglas (cada una con prueba en tests/test_v07_p2_*.py):
  · El navegador NO aporta ningún atributo de identidad: ni rol, ni empresa, ni correo. El callback lee `code` y `state`; lo demás lo decide NUESTRA base.
  · El flujo se QUEMA antes de hablar con el proveedor: un código reutilizado, un intento fallido o un proveedor caído no se pueden reintentar con el mismo `state`.
  · La cookie de correlación ata el callback al navegador que lo inició (defensa contra «login CSRF»); una cookie distinta falla SIN quemar el flujo (un tercero no puede anular el de otra persona).
  · Aceptar una invitación es ATÓMICO: o se crean usuario + vínculo + sesión y la invitación queda aceptada, o no cambia nada.
  · Todo fallo es el MISMO `FlowRefused`: el navegador recibe siempre la misma respuesta (no se distingue «sin invitación», «correo sin verificar», «proveedor caído»…). La auditoría (con empresa conocida) sí guarda que hubo un rechazo.
  · El inicio posterior usa (proveedor, subject), NUNCA el correo: cambiar de correo en el proveedor no cambia de cuenta.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from . import clock
from .company_admin import EMPLOYEE_ROLES, ConfigError, insert_employee
from .config import settings
from .db import now, one, q
from .events import audit
from .identity import Identity, IdentityError, IdentityProvider, pkce_pair
from .security import FUTURE_SKEW_SECONDS, _h, auth_fresh, create_session, delete_session, lookup_session

log = logging.getLogger("aicos")
FLOW_TTL_SECONDS = 600
REAUTH_SLACK_SECONDS = 5            # el proveedor redondea `auth_time` al segundo: una reautenticación real no puede ser MÁS VIEJA que el inicio del flujo menos esto
UNUSABLE_PASSWORD_PREFIX = "!"      # sin «$»: ni argon2 ni scrypt-heredado lo aceptan jamás, con ninguna contraseña


class FlowRefused(Exception):
    """Rechazo genérico. `company_id`/`actor_id`/`action`/`resource` (si se conocen) son SOLO para la auditoría de la empresa; nunca llegan al navegador."""

    def __init__(self, company_id: str | None = None, actor_id: str = "-", action: str = "auth.login", resource: str = "-"):
        super().__init__("acceso rechazado")
        self.company_id, self.actor_id, self.action, self.resource = company_id, actor_id, action, resource


def unusable_password() -> str:
    return UNUSABLE_PASSWORD_PREFIX + secrets.token_hex(24)


# ───────────────────────────── inicio ─────────────────────────────

def invitation_by_token(c, token: str) -> dict | None:
    return one(c, "SELECT * FROM invitations WHERE token_hash=:h AND status='pending' AND expires_at>:n", h=_h(token), n=now())


def start_flow(eng, idp: IdentityProvider, *, purpose: str, invitation: dict | None = None, user_id: str | None = None, session_token: str | None = None) -> tuple[str, str]:
    """Crea el flujo y devuelve (url del proveedor, secreto de la cookie de correlación)."""
    state, secret, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()
    n = now()
    exp = (clock.utcnow() + timedelta(seconds=FLOW_TTL_SECONDS)).isoformat(timespec="milliseconds")
    with eng.begin() as c:
        q(c, "DELETE FROM auth_flows WHERE expires_at<:n", n=n)
        q(c, "INSERT INTO auth_flows(state_hash,cookie_hash,nonce,verifier,purpose,invitation_id,user_id,session_hash,created_at,expires_at) VALUES(:s,:k,:o,:v,:p,:i,:u,:h,:t,:x)",
          s=_h(state), k=_h(secret), o=nonce, v=verifier, p=purpose, i=invitation["id"] if invitation else None, u=user_id, h=_h(session_token) if session_token else None, t=n, x=exp)
    return idp.authorize_url(state, nonce, challenge, login_hint=invitation["email_key"] if invitation else None, force_reauth=purpose == "reauth"), secret


# ───────────────────────────── callback ─────────────────────────────

def finish(eng, idp: IdentityProvider, *, code: str | None, state: str | None, flow_secret: str | None, presented_session: str | None) -> tuple[dict, str]:
    """→ (usuario, token de la sesión nueva). Cualquier problema = FlowRefused."""
    if not code or not state or not flow_secret or len(code) > 2048 or len(state) > 512 or len(flow_secret) > 512:
        raise FlowRefused()
    with eng.begin() as c:                                                      # FASE 1: validar la correlación y QUEMAR el flujo (antes de cualquier llamada externa)
        f = one(c, "SELECT * FROM auth_flows WHERE state_hash=:h", h=_h(state))
        if not f or not hmac.compare_digest(f["cookie_hash"], _h(flow_secret)):
            raise FlowRefused()
        if q(c, "UPDATE auth_flows SET used_at=:n WHERE state_hash=:h AND used_at IS NULL AND expires_at>:n", n=now(), h=f["state_hash"]).rowcount != 1:
            raise FlowRefused()
    where = _where(eng, f)
    try:                                                                         # FASE 2: hablar con el proveedor FUERA de toda transacción (nunca se retiene el candado de escritura durante una llamada de red)
        ident = idp.exchange(code, f["verifier"], nonce=f["nonce"])
        auth_iso = _validate(ident, idp)
    except FlowRefused:
        raise FlowRefused(**where)
    except Exception as e:                                                       # un proveedor caído o con un error: se registra SOLO la clase (nunca el mensaje, el código, el state ni el correo) y el acceso falla CERRADO
        log.warning("proveedor de identidad: el canje del código falló (%s); el acceso se rechaza", type(e).__name__)
        raise FlowRefused(**where)
    with eng.begin() as c:                                                      # FASE 3: aplicar (atómico)
        try:
            user = {"invite": _accept, "login": _login, "reauth": _reauth}[f["purpose"]](c, f, ident, auth_iso, presented_session)
        except FlowRefused:
            raise
        except ConfigError:
            raise FlowRefused(**where)
        if presented_session:
            delete_session(c, presented_session)                                # una sesión nueva REEMPLAZA a la que presentó el navegador (sin apilar ni fijación)
        token = create_session(c, user, settings.session_hours, auth_time=auth_iso)
    return user, token


def _where(eng, f: dict) -> dict:
    """La empresa a la que atribuir un rechazo en la auditoría, si se conoce ANTES de hablar con el proveedor (invitación o reautenticación)."""
    with eng.connect() as c:
        if f["invitation_id"]:
            inv = one(c, "SELECT id,company_id,kind FROM invitations WHERE id=:i", i=f["invitation_id"])
            return {"company_id": inv["company_id"], "action": _action(inv), "resource": inv["id"]} if inv else {}
        if f["user_id"]:
            u = one(c, "SELECT id,company_id FROM users WHERE id=:i", i=f["user_id"])
            return {"company_id": u["company_id"], "actor_id": u["id"], "action": "auth.reauth", "resource": "-"} if u else {}
    return {}


def _validate(ident: Identity, idp: IdentityProvider) -> str | None:
    """Lo que el proveedor afirma debe ser exactamente lo que esperamos. → `auth_time` en ISO (o None si el proveedor no lo informó)."""
    if not isinstance(ident, Identity) or ident.provider != idp.name:
        raise FlowRefused()
    if not isinstance(ident.subject, str) or not 1 <= len(ident.subject) <= 255 or not isinstance(ident.email, str) or not ident.email or len(ident.email) > 254:
        raise FlowRefused()
    if ident.email_verified is not True:                                         # «true», 1, None… no cuentan: solo el booleano verdadero
        raise FlowRefused()
    if ident.auth_time is None:
        return None
    if isinstance(ident.auth_time, bool) or not isinstance(ident.auth_time, int):
        raise FlowRefused()
    try:
        t = datetime.fromtimestamp(ident.auth_time, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise FlowRefused()
    if (t - clock.utcnow()).total_seconds() > FUTURE_SKEW_SECONDS:               # un proveedor que dice haber autenticado en el FUTURO no inicia sesión
        raise FlowRefused()
    return t.isoformat(timespec="milliseconds")


def _action(inv: dict) -> str:
    return "auth.owner_link" if inv.get("kind") == "owner_link" else "auth.invite_accept"


def _accept(c, f: dict, ident: Identity, auth_iso: str | None, presented: str | None) -> dict:
    n = now()
    inv = one(c, "SELECT * FROM invitations WHERE id=:i", i=f["invitation_id"])
    ctx = {"company_id": inv["company_id"], "action": _action(inv), "resource": inv["id"]} if inv else {}
    if not inv or inv["status"] != "pending" or inv["expires_at"] <= n:
        raise FlowRefused(**ctx)
    if ident.email != ident.email.strip() or ident.email.lower() != inv["email_key"]:         # coincidencia EXACTA (solo mayúsculas/minúsculas): ni espacios, ni «+etiqueta», ni homoglifos
        raise FlowRefused(**ctx)
    if one(c, "SELECT 1 AS x FROM identities WHERE provider=:p AND subject=:s", p=ident.provider, s=ident.subject):
        raise FlowRefused(**ctx)                                                 # esa identidad ya pertenece a alguien: nunca se re-vincula
    kind = inv["kind"] or "employee"                                             # NULL = invitación de una base de P2
    if kind == "owner_link":
        return _link_owner(c, inv, ident, n, ctx)
    if kind != "employee" or inv["role"] not in EMPLOYEE_ROLES:                  # una invitación NUNCA crea otro dueño, aunque alguien edite la fila
        raise FlowRefused(**ctx)
    if q(c, "UPDATE invitations SET status='accepted', accepted_at=:n WHERE id=:i AND company_id=:c AND status='pending' AND expires_at>:n", n=n, i=inv["id"], c=inv["company_id"]).rowcount != 1:
        raise FlowRefused(**ctx)                                                 # otro flujo la aceptó un instante antes: un solo uso
    try:
        uid_, _ = insert_employee(c, inv["company_id"], inv["created_by"], name=inv["name"], email=inv["email_key"], role=inv["role"], password_hash=unusable_password(), via="invitation")
    except ConfigError:
        raise FlowRefused(**ctx)
    q(c, "UPDATE invitations SET accepted_user_id=:u WHERE id=:i AND company_id=:c", u=uid_, i=inv["id"], c=inv["company_id"])
    q(c, "INSERT INTO identities(provider,subject,user_id,company_id,created_at,last_login_at) VALUES(:p,:s,:u,:c,:n,:n)", p=ident.provider, s=ident.subject, u=uid_, c=inv["company_id"], n=n)
    audit(c, inv["company_id"], "user", uid_, "auth.invite_accept", inv["id"], "ok", "")
    return one(c, "SELECT * FROM users WHERE id=:i AND company_id=:c", i=uid_, c=inv["company_id"])


def _link_owner(c, inv: dict, ident: Identity, n: str, ctx: dict) -> dict:
    """V0.7 P2.5: vincula la identidad al DUEÑO EXISTENTE que el operador señaló. No se crea ninguna persona: el objetivo sale de la fila (de ESA empresa), debe seguir siendo el dueño activo con el
    mismo correo, y no puede tener ya otra identidad. El proveedor solo confirmó el correo exacto; rol y empresa no salen de él ni del navegador."""
    target = one(c, "SELECT * FROM users WHERE id=:i AND company_id=:c AND role='owner' AND COALESCE(active,1)=1", i=inv["target_user_id"], c=inv["company_id"])
    if not target or target["email"].strip().lower() != inv["email_key"]:
        raise FlowRefused(**ctx)
    if one(c, "SELECT 1 AS x FROM identities WHERE user_id=:u AND company_id=:c", u=target["id"], c=inv["company_id"]):
        raise FlowRefused(**ctx)                                                 # un segundo vínculo exige desvincular antes (unlink-owner, auditado)
    if q(c, "UPDATE invitations SET status='accepted', accepted_at=:n, accepted_user_id=:u WHERE id=:i AND company_id=:c AND status='pending' AND expires_at>:n", n=n, u=target["id"], i=inv["id"], c=inv["company_id"]).rowcount != 1:
        raise FlowRefused(**ctx)
    q(c, "INSERT INTO identities(provider,subject,user_id,company_id,created_at,last_login_at) VALUES(:p,:s,:u,:c,:n,:n)", p=ident.provider, s=ident.subject, u=target["id"], c=inv["company_id"], n=n)
    audit(c, inv["company_id"], "user", target["id"], "auth.owner_link", inv["id"], "ok", "")
    return target


def _login(c, f: dict, ident: Identity, auth_iso: str | None, presented: str | None) -> dict:
    row = one(c, "SELECT * FROM identities WHERE provider=:p AND subject=:s", p=ident.provider, s=ident.subject)
    if not row:
        raise FlowRefused()                                                      # sin invitación y sin vínculo: nada se crea (sin aprovisionamiento automático)
    user = one(c, "SELECT * FROM users WHERE id=:i AND company_id=:c AND COALESCE(active,1)=1", i=row["user_id"], c=row["company_id"])
    if not user:
        raise FlowRefused(company_id=row["company_id"], actor_id=row["user_id"], action="auth.login")
    q(c, "UPDATE identities SET last_login_at=:n WHERE provider=:p AND subject=:s AND company_id=:c", n=now(), p=ident.provider, s=ident.subject, c=row["company_id"])
    audit(c, user["company_id"], "user", user["id"], "auth.login", "-", "ok", "via=idp")
    return user


def _reauth(c, f: dict, ident: Identity, auth_iso: str | None, presented: str | None) -> dict:
    found = lookup_session(c, presented) if presented else None                  # la sesión que pidió reautenticar debe seguir viva (un cierre intermedio no se «resucita»): la empresa y la persona salen de ELLA
    user = found[1] if found else None
    ctx = {"company_id": user["company_id"], "actor_id": user["id"], "action": "auth.reauth"} if user else {}
    if not user or user["id"] != f["user_id"] or not hmac.compare_digest(_h(presented), f["session_hash"] or ""):
        raise FlowRefused(**ctx)
    row = one(c, "SELECT subject FROM identities WHERE user_id=:u AND company_id=:c AND provider=:p", u=user["id"], c=user["company_id"], p=ident.provider)
    if not row or not hmac.compare_digest(row["subject"], ident.subject):
        raise FlowRefused(**ctx)                                                 # otra identidad no reautentica a esta persona
    started = datetime.fromisoformat(f["created_at"])
    if not auth_iso or datetime.fromisoformat(auth_iso) < started - timedelta(seconds=REAUTH_SLACK_SECONDS) or not auth_fresh({"auth_time": auth_iso}):
        raise FlowRefused(**ctx)                                                 # el proveedor reutilizó su sesión vieja: eso NO es una autenticación nueva
    audit(c, user["company_id"], "user", user["id"], "auth.reauth", "-", "ok", "via=idp")
    return user
