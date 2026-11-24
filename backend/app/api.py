"""HTTP API. Company comes ONLY from the authenticated session; permissions are checked here and again in the tool layer."""
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from . import assistant as assistant_mod
from . import company_admin as cfg
from . import identity_flow as flow_mod
from . import invitations as inv_mod
from . import lease
from . import pilot as pilot_mod
from . import ratelimit
from .gate import ActiveGate
from .pilot import HostGuard, SecurityHeaders
from .templates import describe_templates
from .config import settings
from .db import all_, init_db, jload, make_engine, now, one, q, uid as uid_
from .engine.orchestrator import ChatInput, Conflict, Orchestrator
from .engine.planner import ConflictError, PlanEngine
from .events import audit, fetch
from .permissions import ROLE_PERMISSIONS
from .csrf import CsrfMiddleware
from .security import (DUMMY_HASH, auth_fresh, create_session, csrf_for, delete_session, email_key, hash_password, lookup_session, needs_rehash, purge_expired,
                       record_attempt, revoke_user_sessions, session_hash, set_auth_time, throttle_wait, touch_due, touch_session, verify_password)
from .seed import DEMO_PASSWORD, reset_company_scenarios, seed, sync_agents
from .tools import Denied, Principal, ToolError, invoke
from .tools.registry import REGISTRY, describe_tools

COOKIE = "aicos_session"
STEP_UP_PERMS = {"config.manage", "approvals.decide"}        # V0.7 P2: las ESCRITURAS con estos permisos exigen autenticación reciente (`auth_time` ≤ AICOS_REAUTH_MAX_AGE_SECONDS)
IDP_OK, IDP_FAIL = "/#/dashboard", "/#/login?error=idp"      # destinos FIJOS del callback (nunca un parámetro del navegador: no hay redirección abierta)
IDP_START_FAIL = "no se pudo iniciar el acceso"
LOCAL_HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\]|([a-z0-9-]+\.)+localhost|testserver)(:\d+)?")


class UTF8JSONResponse(JSONResponse):
    """Real Safari (found in the demo QA) renders a bare `application/json` document as Latin-1 («demostraciÃ³n»): declare the charset."""
    media_type = "application/json; charset=utf-8"

HISTORIC_REQUEST = ("cerrada", "ejecucion_fallida", "presupuesto_rechazado")
TERMINAL = {"completed", "failed"}


def blocked_reason(t: dict) -> str | None:
    """Tasks that depend on the CUSTOMER are not actionable by the team. A prep-visit task stops being blocked the moment the customer confirms."""
    k = t.get("idem_key") or ""
    if k.endswith(":await-customer"):
        return "Espera la confirmación del cliente (nadie del equipo puede cerrarla)"
    if k.endswith(":prep-visit") and not t.get("vc"):
        return "Bloqueada hasta que el cliente confirme la visita"
    return None


def create_app(db_url: str | None = None, *, identity_provider=None) -> FastAPI:
    """`identity_provider` (V0.7 P2): el adaptador de identidad (interfaz de identity.py). SOLO se inyecta en código: ninguna variable de entorno lo elige, y el simulado (idp_fake) se niega en el piloto."""
    pilot_mod.validate_startup(db_url or settings.db_url, identity_provider)   # V0.7 P1: AICOS_ENV=pilot se niega a arrancar con una configuración insegura, ANTES de abrir la base (sin AICOS_ENV no hace nada)
    pilot = pilot_mod.is_pilot()
    cookie = pilot_mod.cookie_name()                         # `aicos_session` (siempre, antes de V0.7) | `__Host-aicos_session` (piloto)
    idp = identity_provider                                  # None = sin proveedor: NO se registra ninguna ruta de identidad (P2) y todo funciona como en P1
    flow_cookie = pilot_mod.flow_cookie_name()
    enforce_reauth = pilot or idp is not None                # fuera del piloto y sin proveedor, `auth_time` se guarda pero nada lo exige (compatibilidad absoluta de local/demo)
    password_off = pilot and idp is not None                 # con un proveedor REAL operativo en el piloto la contraseña local deja de ser una puerta
    ratelimit.validate()   # a malformed limit or proxy list stops the server here, before any database is opened
    if settings.demo_mode:  # the commercial demo can never run on the lab database
        from .demo_seed import assert_demo_path
        assert_demo_path(db_url or settings.db_url)
    eng = make_engine(db_url or settings.db_url)
    if (db_url or settings.db_url).startswith("sqlite:///") and "memory" not in (db_url or settings.db_url):
        import pathlib
        pathlib.Path((db_url or settings.db_url).replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
    seed(eng)
    sync_agents(eng)  # upgraded DBs get new agent permissions (code is the source of truth)
    orch = Orchestrator(eng)
    orch.plans = PlanEngine(orch)

    # V0.6 — one ACTIVE instance per database (docs/v06/RECUPERACION_DE_EJECUCIONES.md). The keeper acquires/renews the lease; on every gain (start or failover) it recovers:
    #   runs left `running` by a dead owner → declared interrupted, atomically (never replayed); plans mid-flight → the idempotent reconciler re-runs (failed/blocked are never retried).
    # An instance without the lease is a read-only STANDBY (gate.py) and keeps trying to take over.
    orch.boot_id = uid_("boot_")

    def _on_gain():
        orch.recover_runs()
        orch.plans.recover()

    keeper = lease.LeaseKeeper(eng, orch.boot_id, on_gain=_on_gain, every=settings.lease_heartbeat_seconds)
    orch.keeper = keeper
    keeper.start(background=not settings.sync_runs)
    if not keeper.held:
        logging.getLogger("aicos").warning("otra instancia viva usa esta base de datos: esta queda en espera (solo lectura) y tomará el relevo si la otra se detiene")

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:                                  # clean shutdown frees the lease so a standby can take over at once
            keeper.stop()

    app = FastAPI(title="AI Company OS", version="0.4.0", lifespan=lifespan, default_response_class=UTF8JSONResponse)
    app.state.eng, app.state.orch, app.state.idp = eng, orch, idp
    # Middleware order matters. Starlette wraps in REVERSE order of registration: the LAST one added is the OUTERMOST and sees the request first.
    #   request → CsrfMiddleware → ActiveGate → PublicRateLimit → routes
    # An invalid request (forged origin / missing or wrong CSRF token) therefore gets the SAME 403 on every instance, active or standby: it never reaches the role check, so the
    # answer does not reveal which instance holds the lease and a 503 + Retry-After is never sent to a request that no retry could make valid. Only a request that already passed
    # CSRF is refused with 503 by a standby, still before any route code touches state. (Pinned by test_v06_integration.py.)
    # The public-channel limiter (V0.6 P4) is the innermost: /api/public/* is exempt from CSRF by design, and a write that a standby refuses never spends budget. Its state lives in the
    # database, so the active instance and a standby share ONE budget.
    limiter = ratelimit.Limiter(eng)
    app.state.limiter = limiter
    app.add_middleware(ratelimit.PublicRateLimit, limiter=limiter)
    if idp is not None:   # V0.7 P2.5: el cupo de idp/start se cobra a nivel ASGI, antes de analizar el cuerpo (hallazgo de la revisión del PR #26)
        app.add_middleware(ratelimit.IdpStartLimit, limiter=limiter)
    app.add_middleware(ActiveGate, is_active=lambda: orch.active)
    app.add_middleware(CsrfMiddleware, eng=eng, cookie_name=cookie)
    if pilot:   # V0.7 P1 — los MÁS externos: el rechazo de host y los 400/403/429/503 de los demás middlewares también llevan las cabeceras
        app.add_middleware(HostGuard, allowed=settings.trusted_origins)
        app.add_middleware(SecurityHeaders)

    @app.get("/api/health")
    def health():
        """Unauthenticated liveness + role (no data): `active` is False on a standby instance."""
        return {"ok": True, "active": orch.active}

    def current_user(request: Request) -> dict:
        token = request.cookies.get(cookie)
        with eng.connect() as c:
            found = lookup_session(c, token)
        if not found:
            raise HTTPException(401, "no autenticado")
        s, u = found
        if touch_due(s):   # sliding idle timeout, written at most once a minute per session (NO toca auth_time: un panel abierto no equivale a volver a autenticarse)
            with eng.begin() as c:
                touch_session(c, token)
        return {**u, "_auth_time": s.get("auth_time"), "_auth_fresh": auth_fresh(s), "_write": request.method not in ("GET", "HEAD", "OPTIONS")}

    def client_ip(request: Request) -> str:
        return ratelimit.client_address(request.scope)   # the TCP peer, or the real client behind a proxy listed in AICOS_TRUSTED_PROXIES (uvicorn's own --proxy-headers is OFF)

    def is_local(request: Request) -> bool:
        """For endpoints that must only ever answer the machine they run on. A reverse proxy on the same host makes every visitor look like 127.0.0.1, so this ALSO
        requires: no forwarding headers (a proxy added them) and a loopback Host name (stops DNS-rebinding and public host names)."""
        h = request.headers
        if not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):
            return False
        if any(k in h for k in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip")):
            return False
        return bool(LOCAL_HOST.fullmatch((h.get("host") or "").lower()))

    def set_session_cookie(request: Request, response: Response, token: str):
        secure = pilot or settings.cookie_secure == "1" or (settings.cookie_secure == "auto" and request.url.scheme == "https")   # piloto: Secure SIEMPRE (el esquema de la petición no lo decide)
        response.set_cookie(cookie, token, httponly=True, samesite="lax", secure=secure, max_age=settings.session_hours * 3600, path="/")

    def clear_session_cookie(response: Response):
        if pilot:   # un navegador IGNORA un borrado de una cookie `__Host-` que no cumpla Secure + Path=/ : sin esto la cookie vieja quedaría viva en el navegador
            response.delete_cookie(cookie, path="/", secure=True, httponly=True, samesite="lax")
        else:
            response.delete_cookie(cookie, path="/")

    def need(user: dict, perm: str):
        if perm not in ROLE_PERMISSIONS[user["role"]]:
            with eng.begin() as c:
                audit(c, user["company_id"], "user", user["id"], f"api:{perm}", "-", "denied")
            raise HTTPException(403, f"permiso requerido: {perm}")
        if enforce_reauth and perm in STEP_UP_PERMS and user.get("_write") and not user.get("_auth_fresh"):   # DESPUÉS del 403: a quien no tiene el permiso no se le pide reautenticar
            with eng.begin() as c:
                audit(c, user["company_id"], "user", user["id"], f"api:{perm}", "-", "reauth_required")
            raise HTTPException(401, "reauth_required")

    def public(u: dict) -> dict:
        return {k: u[k] for k in ("id", "email", "name", "role", "company_id", "is_demo")}


    # ---------------- commercial demo (only when AICOS_DEMO_MODE=1; see run_demo_comercial.sh) ----------------
    def _demo_info() -> dict:
        import os
        url = db_url or settings.db_url
        return {"demo": settings.demo_mode, "db": os.path.basename(url.replace("sqlite:///", "")) if settings.demo_mode else None, "mode": orch.provider.mode,
                "mode_label": "determinista (sin LLM, sin red)" if orch.provider.mode != "llm" else f"LLM local ({orch.provider.name}) · propone, el motor valida", "commit": settings.commit}

    if not pilot:   # V0.7 P1: en el piloto no existe ninguna ruta de demostración (404 real, no solo oculta)
        @app.get("/api/demo/info")
        def demo_info():
            return _demo_info() if settings.demo_mode else {"demo": False}

    if settings.demo_mode:
        PERSONAS = {"owner": "owner", "admin": "admin", "tech": "technician"}

        @app.get("/demo", response_class=HTMLResponse)
        def demo_page(request: Request):
            from .demo_page import page
            from .demo_seed import summary
            return HTMLResponse(page(_demo_info(), request.url.port or 80, summary(eng)), headers={"Cache-Control": "no-store"})

        @app.get("/demo-login/{persona}")
        def demo_login(persona: str, request: Request, next: str = "/dashboard"):
            """One-click login for the FICTITIOUS demo accounts (loopback only, demo mode only): lets one browser hold 3 sessions via 3 host names."""
            if persona not in PERSONAS or not is_local(request) or not re.fullmatch(r"/[a-z0-9_/\-]{0,80}", next):
                raise HTTPException(404, "no encontrado")
            with eng.begin() as c:
                u = one(c, "SELECT u.* FROM users u JOIN companies co ON co.id=u.company_id WHERE co.slug='clima-sur' AND u.role=:r", r=PERSONAS[persona])
                if not u:
                    raise HTTPException(404, "no encontrado")
                token = create_session(c, u, settings.session_hours)
            resp = RedirectResponse(f"/#{next}", status_code=303)
            set_session_cookie(request, resp, token)
            return resp

    # ---------------- auth ----------------
    class Login(BaseModel):
        email: str
        password: str

    if not pilot:   # devuelve la contraseña de demostración: SOLO fuera del piloto
        @app.get("/api/auth/demo-accounts")
        def demo_accounts():
            with eng.connect() as c:
                rows = all_(c, "SELECT u.email,u.name,u.role,c.name AS company,c.slug FROM users u JOIN companies c ON c.id=u.company_id WHERE u.is_demo=1 ORDER BY c.name,u.role")
            return {"notice": "Cuentas FICTICIAS de demostración (datos de prueba). Autenticación real con contraseña; no es un selector de roles.",
                    "password": DEMO_PASSWORD, "accounts": rows}

    @app.post("/api/auth/login")
    def login(body: Login, request: Request, response: Response):
        if password_off:   # V0.7 P2: el proveedor de identidad es la única puerta; ni se mira la contraseña ni se gasta ni registra ningún intento
            raise HTTPException(403, "El inicio de sesión con contraseña está desactivado en este piloto: use el proveedor de identidad.")
        ip, ekey = client_ip(request), email_key(body.email)
        outcome, user, token, wait = "fail", None, None, 0
        with eng.begin() as c:   # everything below commits BEFORE any error is raised: a failed attempt must leave its trace
            purge_expired(c)
            wait = throttle_wait(c, ekey, ip)
            if not wait:
                u = one(c, "SELECT * FROM users WHERE lower(email)=:e AND COALESCE(active,1)=1", e=ekey)
                good = verify_password(body.password, u["password_hash"] if u else DUMMY_HASH)   # unknown e-mail costs the same hash work as a known one
                if u and good:
                    delete_session(c, request.cookies.get(cookie))   # a new login replaces the session the browser presented (no stacking / fixation)
                    token = create_session(c, u, settings.session_hours)
                    if needs_rehash(u["password_hash"]):             # legacy scrypt (or weaker argon2 parameters) → upgraded now that we hold the plaintext
                        q(c, "UPDATE users SET password_hash=:p WHERE id=:i", p=hash_password(body.password), i=u["id"])
                    record_attempt(c, ekey, ip, True)
                    audit(c, u["company_id"], "user", u["id"], "auth.login", "-", "ok", f"ip={ip}")
                    outcome, user = "ok", u
                else:
                    record_attempt(c, ekey, ip, False)
                    if u:
                        audit(c, u["company_id"], "user", u["id"], "auth.login", "-", "denied", f"ip={ip}")
                        if throttle_wait(c, ekey, ip):
                            audit(c, u["company_id"], "user", u["id"], "auth.locked", "-", "denied", f"ip={ip}")
        if wait:   # same answer for known and unknown e-mails
            raise HTTPException(429, "Demasiados intentos fallidos. Espera unos minutos antes de volver a intentarlo.", headers={"Retry-After": str(wait)})
        if outcome != "ok":
            raise HTTPException(401, "credenciales inválidas")
        set_session_cookie(request, response, token)
        with eng.begin() as c:
            tok = csrf_for(c, token)
        return {**public(user), "csrf_token": tok}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        with eng.begin() as c:
            found = lookup_session(c, request.cookies.get(cookie))
            if found:
                audit(c, found[1]["company_id"], "user", found[1]["id"], "auth.logout", "-", "ok")
            delete_session(c, request.cookies.get(cookie))
        clear_session_cookie(response)
        return {"ok": True}

    @app.post("/api/auth/logout-all")
    def logout_all(request: Request, response: Response, user=Depends(current_user)):
        """«Cerrar sesión en todos lados»: revokes EVERY session of this user (lost laptop, shared computer)."""
        with eng.begin() as c:
            n = revoke_user_sessions(c, user["id"], user["company_id"])
            audit(c, user["company_id"], "user", user["id"], "auth.logout_all", "-", "ok", f"sesiones={n}")
        clear_session_cookie(response)
        return {"ok": True, "revoked": n}

    @app.get("/api/auth/me")
    def me(request: Request, user=Depends(current_user)):
        with eng.begin() as c:
            co = one(c, "SELECT id,slug,name,industry FROM companies WHERE id=:i", i=user["company_id"])
            tok = csrf_for(c, request.cookies.get(cookie))
        return {**public(user), "company": co, "permissions": sorted(ROLE_PERMISSIONS[user["role"]]), "csrf_token": tok, "auth_time": user["_auth_time"],
                "step_up": {"enforced": enforce_reauth, "fresh": user["_auth_fresh"], "max_age_seconds": settings.reauth_max_age_seconds}}

    # ---------------- V0.7 P2: reautenticación y acceso con proveedor de identidad ----------------
    class Reauth(BaseModel):
        password: str = Field(min_length=1, max_length=200)

    @app.post("/api/auth/reauth")
    def reauth(body: Reauth, request: Request, user=Depends(current_user)):
        """Confirma la contraseña de LA PERSONA DE LA SESIÓN (no la de otra) y renueva `auth_time` de ESA sesión. Comparte el límite de intentos del inicio de sesión."""
        if password_off:
            raise HTTPException(403, "La reautenticación con contraseña está desactivada en este piloto: use el proveedor de identidad.")
        ip, ekey, outcome, wait, at = client_ip(request), email_key(user["email"]), "fail", 0, None
        with eng.begin() as c:   # igual que el inicio de sesión: un intento fallido deja su rastro ANTES de cualquier error
            wait = throttle_wait(c, ekey, ip)
            if not wait:
                if verify_password(body.password, user["password_hash"]):
                    at = now()
                    set_auth_time(c, request.cookies.get(cookie), at)
                    record_attempt(c, ekey, ip, True)
                    audit(c, user["company_id"], "user", user["id"], "auth.reauth", "-", "ok", f"ip={ip}")
                    outcome = "ok"
                else:
                    record_attempt(c, ekey, ip, False)
                    audit(c, user["company_id"], "user", user["id"], "auth.reauth", "-", "denied", f"ip={ip}")
                    if throttle_wait(c, ekey, ip):
                        audit(c, user["company_id"], "user", user["id"], "auth.locked", "-", "denied", f"ip={ip}")
        if wait:
            raise HTTPException(429, "Demasiados intentos fallidos. Espera unos minutos antes de volver a intentarlo.", headers={"Retry-After": str(wait)})
        if outcome != "ok":
            raise HTTPException(401, "credenciales inválidas")
        return {"ok": True, "auth_time": at}

    if idp is not None:   # SIN proveedor inyectado estas rutas no existen (404): la superficie de identidad solo aparece cuando hay con qué atenderla
        class IdpStart(BaseModel):
            model_config = ConfigDict(extra="forbid")        # el navegador no aporta rol, empresa, correo ni auth_time: cualquier campo extra es un 422
            invite_token: str | None = Field(default=None, min_length=1, max_length=300)
            reauth: bool = False

            @model_validator(mode="after")
            def _one_purpose(self):
                if self.reauth and self.invite_token:
                    raise ValueError("reauth e invite_token son excluyentes")
                return self

        def set_flow_cookie(request: Request, response: Response, secret: str):
            secure = pilot or settings.cookie_secure == "1" or (settings.cookie_secure == "auto" and request.url.scheme == "https")
            response.set_cookie(flow_cookie, secret, httponly=True, samesite="lax", secure=secure, max_age=flow_mod.FLOW_TTL_SECONDS, path="/")

        def clear_flow_cookie(response: Response):
            response.delete_cookie(flow_cookie, path="/", **({"secure": True} if pilot else {}), httponly=True, samesite="lax")

        @app.post("/api/auth/idp/start")
        def idp_start(body: IdpStart, request: Request, response: Response):
            ip = client_ip(request)
            ekey = "idp:" + ip                                    # el límite de fallos de enlaces de invitación va por dirección (no por token: no hay correo que bloquear)
            wait = 0
            with eng.begin() as c:
                purge_expired(c)
                wait = throttle_wait(c, ekey, ip)
            if wait:
                raise HTTPException(429, "Demasiados intentos fallidos. Espera unos minutos antes de volver a intentarlo.", headers={"Retry-After": str(wait)})
            invitation = None
            if body.invite_token:
                with eng.begin() as c:
                    invitation = flow_mod.invitation_by_token(c, body.invite_token)
                    if not invitation:
                        record_attempt(c, ekey, ip, False)        # se confirma ANTES de responder: el intento fallido deja su rastro
                if not invitation:
                    raise HTTPException(400, IDP_START_FAIL)      # la misma respuesta para «inventado», «usado», «vencido» y «revocado»
            if body.reauth:
                token = request.cookies.get(cookie)
                with eng.connect() as c:
                    found = lookup_session(c, token)
                    ident = one(c, "SELECT subject FROM identities WHERE user_id=:u AND company_id=:c AND provider=:p", u=found[1]["id"], c=found[1]["company_id"], p=idp.name) if found else None
                if not found:
                    raise HTTPException(401, "no autenticado")
                if not ident:
                    raise HTTPException(400, IDP_START_FAIL)
                url, secret = flow_mod.start_flow(eng, idp, purpose="reauth", user_id=found[1]["id"], session_token=token)
            else:
                url, secret = flow_mod.start_flow(eng, idp, purpose="invite" if invitation else "login", invitation=invitation)
            set_flow_cookie(request, response, secret)
            response.headers["Cache-Control"] = "no-store"
            return {"authorize_url": url}

        @app.get("/api/auth/callback")
        def idp_callback(request: Request, code: str | None = None, state: str | None = None):
            """Navegación GET de redirección desde el proveedor: exenta de CSRF por ser GET, pero atada a state + PKCE + la cookie de correlación de UN solo uso. TODO fallo = la misma respuesta."""
            ok, token = False, None
            try:
                _, token = flow_mod.finish(eng, idp, code=code, state=state, flow_secret=request.cookies.get(flow_cookie), presented_session=request.cookies.get(cookie))
                ok = True
            except flow_mod.FlowRefused as e:
                if e.company_id:   # solo se audita en una empresa CONOCIDA; el navegador no se entera
                    with eng.begin() as c:
                        audit(c, e.company_id, "user", e.actor_id, e.action, e.resource, "denied")
            except Exception:
                logging.getLogger("aicos").warning("callback del proveedor de identidad: error interno (el acceso se rechaza); sin datos de la persona")
            resp = RedirectResponse(IDP_OK if ok else IDP_FAIL, status_code=303)
            resp.headers["Cache-Control"] = "no-store"
            clear_flow_cookie(resp)
            if ok:
                set_session_cookie(request, resp, token)
            return resp

    # ---------------- read models ----------------
    @app.get("/api/dashboard")
    def dashboard(user=Depends(current_user)):
        cid = user["company_id"]
        with eng.connect() as c:
            by = {r["status"]: r["n"] for r in all_(c, "SELECT status,COUNT(*) AS n FROM requests WHERE company_id=:c GROUP BY status", c=cid)}
            runs = all_(c, """SELECT ru.id,ru.status,ru.request_id,ru.updated_at,cu.name AS customer FROM runs ru LEFT JOIN requests r ON r.id=ru.request_id AND r.company_id=ru.company_id
                              LEFT JOIN customers cu ON cu.id=r.customer_id AND cu.company_id=ru.company_id WHERE ru.company_id=:c ORDER BY ru.created_at DESC LIMIT 20""", c=cid)
            ap = all_(c, "SELECT id,summary,amount,request_id,run_id,created_at FROM approvals WHERE company_id=:c AND status='pendiente'", c=cid)
            tasks = all_(c, """SELECT t.id,t.title,t.kind,t.assignee_agent,u.name AS assignee,t.created_at,t.idem_key,v.customer_confirmed AS vc FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id AND u.company_id=t.company_id
                               LEFT JOIN visits v ON v.request_id=t.request_id AND v.company_id=t.company_id WHERE t.company_id=:c AND t.status='pendiente' ORDER BY t.created_at DESC LIMIT 30""", c=cid)
            waiting = all_(c, "SELECT t.id,t.title,t.kind,u.name AS assignee FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id AND u.company_id=t.company_id WHERE t.company_id=:c AND t.status='en_espera' ORDER BY t.created_at DESC LIMIT 30", c=cid)
            commit = all_(c, """SELECT v.request_id,v.slot_start,v.status,v.customer_confirmed,cu.name AS customer FROM visits v JOIN requests r ON r.id=v.request_id
                                JOIN customers cu ON cu.id=r.customer_id WHERE v.company_id=:c ORDER BY v.slot_start""", c=cid)
        for t in tasks:
            t["blocked_reason"] = blocked_reason(t)
            t.pop("idem_key", None); t.pop("vc", None)
        def _fail_text(r):
            who = r["customer"] or "chat de cliente sin solicitud registrada"
            return f"Ejecución fallida · {who} · {r['updated_at'][:16].replace('T', ' ')} UTC — requiere revisión; no se reintenta sola"
        alerts = [{"kind": "blocked", "run_id": r["id"], "text": f"Ejecución bloqueada · {r['customer'] or 'sin cliente'}: requiere decisión"} for r in runs if r["status"] == "blocked"]
        alerts += [{"kind": "failed", "run_id": r["id"], "text": _fail_text(r)} for r in runs if r["status"] == "failed"]
        alerts += [{"kind": "approval", "run_id": a["run_id"], "text": f"Aprobación pendiente: {a['summary']}"} for a in ap]
        active = sum(1 for r in runs if r["status"] == "running")
        closed = sum(n for k, n in by.items() if k in HISTORIC_REQUEST)
        return {"requests_by_status": by, "total_requests": sum(by.values()), "active_requests": sum(by.values()) - closed, "historic_requests": closed, "runs": runs, "pending_approvals": ap,
                "pending_tasks": tasks, "waiting_tasks": waiting, "commitments": commit, "alerts": alerts, "active_runs": active, "provider": orch.provider.name, "provider_mode": orch.provider.mode}

    @app.get("/api/agents")
    def agents(user=Depends(current_user)):
        need(user, "agents.read")
        with eng.connect() as c:
            rows = all_(c, """SELECT a.id,a.role,a.name,a.description,a.permissions,a.enabled,a.user_id,u.name AS employee,u.role AS employee_role FROM agents a
                              LEFT JOIN users u ON u.id=a.user_id WHERE a.company_id=:c ORDER BY a.id""", c=user["company_id"])
            live = all_(c, "SELECT id,status FROM runs WHERE company_id=:c AND status IN ('running','waiting_approval','blocked','waiting_technician','waiting_task')", c=user["company_id"])
            live += all_(c, "SELECT id,status FROM plans WHERE company_id=:c AND status IN ('running','waiting_human','awaiting_user_confirmation','needs_clarification')", c=user["company_id"])
            mine = {x["u"]: x["n"] for x in all_(c, "SELECT assignee_user_id AS u,COUNT(*) AS n FROM tasks WHERE company_id=:c AND status='pendiente' AND assignee_user_id IS NOT NULL GROUP BY assignee_user_id", c=user["company_id"])}
        for r in rows:
            r["permissions"] = jload(r["permissions"], [])
            # availability is about the agent being usable, NOT about it "doing something right now" (the old label «inactivo» read as «disabled»)
            if not r["enabled"]:
                r["availability"], r["availability_text"] = "disabled", "Deshabilitado"
            else:
                n = mine.get(r["user_id"], 0) if r["user_id"] else len(live)
                what = f"{n} tarea(s)" if r["user_id"] else f"{n} en curso"
                r["availability"], r["availability_text"] = ("working" if n else "available"), ("Disponible · " + what if n else "Disponible")
        order = ["reception", "coordinator", "owner_assistant", "admin_assistant", "tech_assistant"]
        rows.sort(key=lambda r: order.index(r["role"]))
        return {"agents": rows, "live_runs": live, "provider": orch.provider.name, "provider_mode": orch.provider.mode}

    @app.get("/api/tools")
    def tools(user=Depends(current_user)):
        need(user, "agents.read")
        return {"tools": describe_tools(), "note": "Esquema compatible con MCP (name/description/inputSchema); MCP aún no expuesto como servidor.",
                "human_only": ["approvals.decide"]}

    @app.get("/api/integrations")
    def integrations(user=Depends(current_user)):
        with eng.connect() as c:
            return {"integrations": all_(c, "SELECT kind,name,mode,enabled,note FROM integrations WHERE company_id=:c", c=user["company_id"])}

    @app.get("/api/requests")
    def list_requests(user=Depends(current_user)):
        need(user, "requests.read")
        with eng.connect() as c:
            return {"requests": all_(c, """SELECT r.id,r.description,r.category,r.urgency,r.status,r.created_at,cu.name AS customer FROM requests r
                                           JOIN customers cu ON cu.id=r.customer_id WHERE r.company_id=:c ORDER BY r.created_at DESC""", c=user["company_id"])}

    @app.get("/api/requests/{rid}")
    def get_request(rid: str, user=Depends(current_user)):
        need(user, "requests.read")
        with eng.connect() as c:
            r = one(c, "SELECT * FROM requests WHERE id=:i AND company_id=:c", i=rid, c=user["company_id"])
            if not r:
                raise HTTPException(404, "no encontrada")
            cu = one(c, "SELECT id,name,phone,email,address FROM customers WHERE id=:i AND company_id=:c", i=r["customer_id"], c=user["company_id"])
            hist = all_(c, "SELECT id,description,status,created_at FROM requests WHERE company_id=:c AND customer_id=:k AND id<>:r ORDER BY created_at DESC", c=user["company_id"], k=r["customer_id"], r=rid)
            quotes = all_(c, "SELECT kind,total,status,lines FROM quotes WHERE company_id=:c AND request_id=:r", c=user["company_id"], r=rid)
            visit = one(c, "SELECT id,status,slot_start,customer_confirmed,confirmed_at FROM visits WHERE company_id=:c AND request_id=:r", c=user["company_id"], r=rid)
            wo = one(c, "SELECT status,technician_id,notes FROM work_orders WHERE company_id=:c AND request_id=:r", c=user["company_id"], r=rid)
            msgs = all_(c, "SELECT sender,body,status,created_at FROM messages m WHERE company_id=:c AND conversation_id=:cv ORDER BY created_at", c=user["company_id"], cv=r["conversation_id"]) if r["conversation_id"] else []
            runs = all_(c, "SELECT id,status,created_at FROM runs WHERE company_id=:c AND request_id=:r", c=user["company_id"], r=rid)
            results = all_(c, "SELECT kind,summary,created_at FROM results WHERE company_id=:c AND request_id=:r", c=user["company_id"], r=rid)
            tasks = all_(c, "SELECT title,status,assignee_agent FROM tasks WHERE company_id=:c AND request_id=:r", c=user["company_id"], r=rid)
        for qd in quotes:
            qd["lines"] = jload(qd["lines"], [])
        return {"request": r, "customer": cu, "history": hist, "quotes": quotes, "visit": visit, "work_order": wo, "messages": msgs, "runs": runs, "results": results, "tasks": tasks}

    @app.get("/api/customers")
    def customers(user=Depends(current_user)):
        need(user, "customers.read")
        with eng.connect() as c:
            return {"customers": all_(c, "SELECT id,name,phone,email,address FROM customers WHERE company_id=:c ORDER BY name", c=user["company_id"])}

    @app.get("/api/tasks")
    def tasks(user=Depends(current_user)):
        need(user, "tasks.read")
        try:
            pr = orch.principal(user["company_id"], {"owner": "owner_assistant", "admin": "admin_assistant", "technician": "tech_assistant"}[user["role"]], user["id"])
        except ToolError as e:
            raise HTTPException(403, f"No tienes un asistente personal activo: {e}")
        out = invoke(eng, pr, "tasks.list", {})
        with eng.connect() as c:  # tasks assigned to ME that exist but are waiting for another person (so nobody wonders what comes next)
            waiting = all_(c, """SELECT t.id,t.title,t.kind,du.name AS waiting_for,du.role AS waiting_for_role FROM tasks t
                                 LEFT JOIN plan_steps s ON s.task_id=t.id AND s.company_id=t.company_id
                                 LEFT JOIN plan_steps d ON d.plan_id=s.plan_id AND d.seq=s.depends_on AND d.company_id=t.company_id
                                 LEFT JOIN users du ON du.id=d.assignee_user_id AND du.company_id=t.company_id
                                 WHERE t.company_id=:c AND t.status='en_espera' AND t.assignee_user_id=:u ORDER BY t.created_at""", c=user["company_id"], u=user["id"])
            raw = {r["id"]: r for r in all_(c, """SELECT t.id,t.idem_key,v.customer_confirmed AS vc FROM tasks t LEFT JOIN visits v ON v.request_id=t.request_id AND v.company_id=t.company_id
                                                 WHERE t.company_id=:c AND t.status='pendiente' AND t.assignee_user_id=:u""", c=user["company_id"], u=user["id"])}
        for t in out["tasks"]:
            t["blocked_reason"] = blocked_reason(raw.get(t["id"], {}))
        return {"tasks": out["tasks"], "waiting": waiting}

    @app.get("/api/work-orders")
    def work_orders(user=Depends(current_user)):
        with eng.connect() as c:
            sql = """SELECT w.request_id,w.status,w.technician_id,u.name AS technician,r.description,cu.name AS customer FROM work_orders w JOIN requests r ON r.id=w.request_id
                     JOIN customers cu ON cu.id=r.customer_id LEFT JOIN users u ON u.id=w.technician_id WHERE w.company_id=:c"""
            p = {"c": user["company_id"]}
            if user["role"] == "technician":
                sql += " AND w.technician_id=:u"
                p["u"] = user["id"]
            return {"work_orders": all_(c, sql, **p)}

    @app.get("/api/audit")
    def audit_log(user=Depends(current_user)):
        if user["role"] != "owner":
            raise HTTPException(403, "solo el dueño")
        with eng.connect() as c:
            return {"audit": all_(c, "SELECT ts,actor_type,actor_id,action,resource,outcome,detail FROM audit_log WHERE company_id=:c ORDER BY id DESC LIMIT 100", c=user["company_id"])}

    # ---------------- approvals & runs ----------------
    @app.get("/api/approvals")
    def approvals(user=Depends(current_user)):
        need(user, "approvals.read")
        with eng.connect() as c:
            return {"approvals": all_(c, "SELECT * FROM approvals WHERE company_id=:c ORDER BY created_at DESC LIMIT 50", c=user["company_id"])}

    class Decision(BaseModel):
        decision: str = Field(pattern="^(approved|rejected)$")
        comment: str | None = Field(default=None, max_length=300)

    @app.post("/api/approvals/{aid}/decide")
    def decide(aid: str, body: Decision, user=Depends(current_user)):
        need(user, "approvals.decide")  # human-only permission: no agent holds it
        try:
            return orch.decide(user["company_id"], aid, user, body.decision, body.comment)
        except ToolError as e:
            raise HTTPException(404, str(e))
        except Conflict as e:
            raise HTTPException(409, str(e))

    @app.get("/api/runs")
    def list_runs(user=Depends(current_user)):
        need(user, "events.read")
        with eng.connect() as c:
            rows = all_(c, """SELECT ru.id,ru.status,ru.phase,ru.scenario,ru.request_id,ru.created_at,ru.provider,r.description,cu.name AS customer
                              FROM runs ru LEFT JOIN requests r ON r.id=ru.request_id AND r.company_id=ru.company_id
                              LEFT JOIN customers cu ON cu.id=r.customer_id WHERE ru.company_id=:c ORDER BY ru.created_at DESC LIMIT 50""", c=user["company_id"])
        return {"runs": rows}

    def _run(user, rid):
        with eng.connect() as c:
            r = one(c, "SELECT * FROM runs WHERE id=:i AND company_id=:c", i=rid, c=user["company_id"])
        if not r:
            raise HTTPException(404, "ejecución no encontrada")
        return r

    @app.get("/api/runs/{rid}")
    def get_run(rid: str, user=Depends(current_user)):
        need(user, "events.read")
        r = _run(user, rid)
        st = jload(r["state"], {})
        with eng.connect() as c:
            ap = all_(c, "SELECT a.id,a.subject,a.summary,a.amount,a.status,u.name AS decided_by FROM approvals a LEFT JOIN users u ON u.id=a.decided_by_user_id WHERE a.company_id=:c AND a.run_id=:r ORDER BY a.created_at", c=user["company_id"], r=rid)
            cid = user["company_id"]
            tasks_ = all_(c, "SELECT t.id,t.title,t.kind,t.status,t.evidence,u.name AS assignee,u.role AS assignee_role,t.assignee_agent FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id AND u.company_id=t.company_id WHERE t.company_id=:c AND (t.run_id=:r OR t.request_id=:q) ORDER BY t.created_at", c=cid, r=rid, q=r["request_id"])
            dels = all_(c, "SELECT from_agent,to_agent,task,status FROM delegations WHERE company_id=:c AND run_id=:r ORDER BY created_at", c=cid, r=rid)
            cms = all_(c, "SELECT m.title,m.status,m.due_at,m.last_evidence,u.name AS owner,u.role AS owner_role FROM commitments m JOIN users u ON u.id=m.owner_user_id WHERE m.company_id=:c AND m.run_id=:r ORDER BY m.due_at", c=cid, r=rid)
            ress = all_(c, "SELECT kind,summary FROM results WHERE company_id=:c AND run_id=:r", c=cid, r=rid)
            mats = all_(c, "SELECT sku,qty,status,cost,reason FROM material_requests WHERE company_id=:c AND run_id=:r", c=cid, r=rid)
            drafts = all_(c, "SELECT m.status,m.body FROM messages m WHERE m.company_id=:c AND m.status IN ('preparado_no_enviado','requiere_aprobacion','cancelado') AND m.conversation_id IN (SELECT conversation_id FROM requests WHERE id=:q AND company_id=:c)", c=cid, q=r["request_id"])
        return {"id": r["id"], "status": r["status"], "phase": r["phase"], "request_id": r["request_id"], "provider": r["provider"], "scenario": r["scenario"],
                "alternatives": st.get("alternatives", []) if r["status"] == "blocked" else [], "approvals": ap, "created_at": r["created_at"],
                "tasks": tasks_, "delegations": dels, "commitments": cms, "results": ress, "materials": mats, "drafts": drafts}

    @app.get("/api/runs/{rid}/events")
    def run_events(rid: str, after: int = 0, user=Depends(current_user)):
        need(user, "events.read")
        _run(user, rid)
        return {"events": fetch(eng, user["company_id"], rid, after)}

    def _sse(request: Request, cid: str, rid: str, after: int, allow: set | None = None):
        last_hdr = request.headers.get("last-event-id")
        last = int(last_hdr) if last_hdr and last_hdr.isdigit() else after

        async def gen():
            nonlocal last
            idle = 0
            yield "retry: 1500\n\n"
            while True:
                if await request.is_disconnected():
                    return
                evs = await run_in_threadpool(fetch, eng, cid, rid, last)
                for e in evs:
                    last = e["seq"]
                    if allow is not None and e["type"] not in allow:
                        continue
                    yield f"id: {e['seq']}\nevent: run-event\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                idle = 0 if evs else idle + 1
                if idle % 20 == 19:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{rid}/stream")
    async def run_stream(rid: str, request: Request, after: int = 0, user=Depends(current_user)):
        need(user, "events.read")
        _run(user, rid)
        return _sse(request, user["company_id"], rid, after)

    class Resolve(BaseModel):
        action: str

    @app.post("/api/runs/{rid}/resolve")
    def resolve(rid: str, body: Resolve, user=Depends(current_user)):
        need(user, "runs.resolve")
        _run(user, rid)
        try:
            return orch.resolve(user["company_id"], rid, user, body.action)
        except ToolError as e:
            raise HTTPException(400, str(e))
        except Conflict as e:
            raise HTTPException(409, str(e))

    # ---------------- public customer channel ----------------
    class Chat(BaseModel):
        message: str = Field(min_length=3, max_length=2000)
        name: str | None = Field(default=None, max_length=120)
        phone: str | None = Field(default=None, max_length=40)
        email: str | None = Field(default=None, max_length=120)
        client_msg_id: str = Field(min_length=6, max_length=80, pattern=r"^[A-Za-z0-9_\-]+$")        # identifica el mensaje; NO es una credencial (puede conocerse)
        retry_secret: str | None = Field(default=None, min_length=22, max_length=128, pattern=r"^[A-Za-z0-9_\-]+$")   # lo genera el cliente (≥128 bits, CSPRNG): prueba POSESIÓN para reintentar (Issue #20)

        @field_validator("retry_secret")
        @classmethod
        def _looks_generated(cls, v):
            if v is not None and len(set(v)) < 10:            # cordura, no entropía: «aaaaaaaa…» o «abab…» no son un secreto generado
                raise ValueError("retry_secret debe generarse con un generador aleatorio criptográfico")
            return v

    @app.post("/api/public/{slug}/chat")
    def public_chat(slug: str, body: Chat):
        try:
            return orch.start_chat(slug, ChatInput(body.message, body.name, body.phone, body.email, body.client_msg_id, body.retry_secret))
        except ToolError as e:
            raise HTTPException(404, str(e))

    def _spend(request: Request, bucket):
        """Spend one request from a bucket the middleware could not know about (it needs the token / the request id). → the bucket, to refund it if wanted. 429 when empty."""
        if bucket is None:
            return None                                     # unknown company: the middleware already charged the shared «unknown» buckets
        ok, wait = limiter.take([bucket])
        if not ok:
            raise HTTPException(429, ratelimit.message(wait), headers={"Retry-After": str(wait)})
        return bucket

    @app.get("/api/public/{slug}/outbox")
    def public_outbox(slug: str, token: str, request: Request):
        # A token that finds nothing is an enumeration attempt: its budget is spent BEFORE the lookup and given back when the lookup succeeds. Once it is exhausted a valid and an
        # invalid token get the same 429, so the limit cannot be used as an oracle (and a flood of misses cannot be mixed with hits).
        miss = _spend(request, ratelimit.miss_bucket(request.scope))
        with eng.connect() as c:
            conv = one(c, "SELECT cv.id,cv.company_id FROM conversations cv JOIN companies co ON co.id=cv.company_id WHERE cv.token=:t AND co.slug=:s", t=token, s=slug)
            if not conv:
                raise HTTPException(404, "conversación no encontrada")
            msgs = all_(c, "SELECT sender,body,status,created_at FROM messages WHERE company_id=:c AND conversation_id=:i ORDER BY created_at", c=conv["company_id"], i=conv["id"])
            req = one(c, "SELECT id,status FROM requests WHERE company_id=:c AND conversation_id=:i", c=conv["company_id"], i=conv["id"])
        if miss:
            limiter.refund(miss)
        return {"messages": msgs, "request": req, "channel_note": "Canal de cliente SIMULADO: los mensajes 'preparado_no_enviado' no se envían a nadie."}

    class Confirm(BaseModel):
        request_id: str = Field(min_length=1, max_length=80)
        code: str = Field(min_length=4, max_length=12)

    @app.post("/api/public/{slug}/visits/confirm")
    def public_confirm(slug: str, body: Confirm, request: Request):
        # The code is 6 hex characters: what really stops guessing is the budget of attempts on THIS request, whichever address they come from (the per-source and per-company
        # budgets were already spent by the middleware). It counts every attempt, existing request or not, so it is not an oracle for which request ids exist.
        _spend(request, ratelimit.target_bucket(request.scope, body.request_id))
        try:
            return orch.confirm_visit(slug, body.request_id, body.code)
        except ToolError as e:
            raise HTTPException(400, str(e))

    # ---------------- employee assistant & generic tool endpoint ----------------
    class AssistantMsg(BaseModel):
        message: str = Field(min_length=1, max_length=1000)

    @app.post("/api/assistant/chat")
    def assistant_chat(body: AssistantMsg, user=Depends(current_user)):
        return assistant_mod.handle(eng, orch, user, body.message)

    @app.get("/api/assistant/history")
    def assistant_history(user=Depends(current_user)):
        with eng.connect() as c:
            conv = one(c, "SELECT id FROM conversations WHERE company_id=:c AND user_id=:u AND channel='assistant'", c=user["company_id"], u=user["id"])
            msgs = all_(c, "SELECT sender,body,status,created_at FROM messages WHERE company_id=:c AND conversation_id=:i ORDER BY created_at", c=user["company_id"], i=conv["id"]) if conv else []
        return {"messages": msgs}

    class ToolCall(BaseModel):
        args: dict = Field(default_factory=dict)

    @app.post("/api/tools/{name}/invoke")
    def invoke_tool(name: str, body: ToolCall, user=Depends(current_user)):
        """A human invokes a tool with THEIR OWN role permissions (same validation/audit path as agents)."""
        p = Principal("user", user["id"], user["name"], user["role"], user["company_id"], set(ROLE_PERMISSIONS[user["role"]]), user_id=user["id"], user_role=user["role"])
        try:
            return invoke(eng, p, name, body.args, provider=orch.provider)
        except Denied as e:
            raise HTTPException(403, str(e))
        except ToolError as e:
            raise HTTPException(400, str(e))

    if not pilot:   # borra datos de la empresa de la sesión: SOLO fuera del piloto
        class ResetScenarios(BaseModel):
            confirm: str

        @app.post("/api/demo/reset-scenarios")
        def reset_scenarios(body: ResetScenarios, user=Depends(current_user)):
            """Local, explicit, owner-only: removes THIS company's scenario runs only (users, seed data and other companies stay). Backs up the DB file first."""
            if user["role"] != "owner":
                raise HTTPException(403, "solo el dueño puede reiniciar los escenarios")
            if body.confirm != "REINICIAR":
                raise HTTPException(400, "escribe REINICIAR para confirmar")
            with eng.connect() as c:
                slug = one(c, "SELECT slug FROM companies WHERE id=:i", i=user["company_id"])["slug"]
            url = db_url or settings.db_url
            path = url.replace("sqlite:///", "") if url.startswith("sqlite:///") else None
            try:
                return reset_company_scenarios(eng, slug, path)
            except KeyError:   # companies created from configuration have no seed to go back to
                raise HTTPException(400, "El reinicio de escenarios solo existe para las empresas de demostración sembradas (Clima Sur, Motor Norte)")

    # ---------------- V0.3: attention, commitments, task completion ----------------
    @app.get("/api/attention")
    def attention(user=Depends(current_user)):
        """«Qué requiere mi atención» — role-aware, from persisted state (no traces, no personal data beyond names already visible to the role)."""
        cid, uid_, role = user["company_id"], user["id"], user["role"]
        items = []
        with eng.connect() as c:
            if role == "owner":
                for a in all_(c, "SELECT id,summary,amount,run_id FROM approvals WHERE company_id=:c AND status='pendiente'", c=cid):
                    items.append({"kind": "approval", "severity": "high", "text": f"Aprobar: {a['summary']}", "run_id": a["run_id"]})
            if role == "owner":
                for pl in all_(c, "SELECT id,status,summary,text FROM plans WHERE company_id=:c AND status IN ('awaiting_user_confirmation','needs_clarification')", c=cid):
                    items.append({"kind": "plan", "severity": "high", "plan_id": pl["id"], "text": ("Confirmar el plan del coordinador: " if pl["status"] == "awaiting_user_confirmation" else "El coordinador necesita una aclaración: ") + (pl["summary"] or pl["text"])[:110]})
            if role in ("owner", "admin"):
                for r in all_(c, """SELECT ru.id,ru.status,ru.updated_at,cu.name AS customer FROM runs ru LEFT JOIN requests rq ON rq.id=ru.request_id AND rq.company_id=ru.company_id
                                    LEFT JOIN customers cu ON cu.id=rq.customer_id AND cu.company_id=ru.company_id WHERE ru.company_id=:c AND ru.status IN ('blocked','failed') ORDER BY ru.updated_at DESC""", c=cid):
                    who = r["customer"] or "chat de cliente sin solicitud registrada"
                    items.append({"kind": r["status"], "severity": "high", "run_id": r["id"], "text": f"Ejecución bloqueada · {who}: requiere una decisión" if r["status"] == "blocked"
                                  else f"Ejecución fallida · {who} · {r['updated_at'][:16].replace('T', ' ')} UTC: requiere revisión; no se reintenta sola"})
            sql = "SELECT id,title,kind,run_id FROM tasks WHERE company_id=:c AND status='pendiente' AND assignee_user_id=:u"
            for t in all_(c, sql, c=cid, u=uid_):
                items.append({"kind": "task", "severity": "medium", "text": f"Tu tarea: {t['title']}", "run_id": t["run_id"], "task_id": t["id"]})
            ov = "SELECT title,due_at,run_id FROM commitments WHERE company_id=:c AND status IN ('abierto','vencido')" + ("" if role == "owner" else " AND owner_user_id=:u")
            from datetime import datetime, timezone
            for m in all_(c, ov, c=cid, **({} if role == "owner" else {"u": uid_})):
                due = datetime.fromisoformat(m["due_at"]); due = due if due.tzinfo else due.replace(tzinfo=timezone.utc)
                if due <= datetime.now(timezone.utc):
                    items.append({"kind": "overdue", "severity": "high", "text": f"Compromiso vencido: {m['title']}", "run_id": m["run_id"]})
            if role == "technician":
                for r in all_(c, """SELECT ru.id FROM work_orders w JOIN runs ru ON ru.request_id=w.request_id AND ru.company_id=w.company_id
                                    WHERE w.company_id=:c AND w.technician_id=:u AND ru.status='waiting_technician'""", c=cid, u=uid_):
                    items.append({"kind": "report_material", "severity": "medium", "text": "Una visita espera tu informe de material (escríbelo en «Mi asistente»)", "run_id": r["id"]})
        order = {"high": 0, "medium": 1}
        return {"items": sorted(items, key=lambda x: order[x["severity"]])}

    @app.get("/api/team")
    def team(user=Depends(current_user)):
        need(user, "commitments.write")
        with eng.connect() as c:
            return {"users": all_(c, "SELECT id,name,role FROM users WHERE company_id=:c AND COALESCE(active,1)=1 ORDER BY role,name", c=user["company_id"])}

    def _human_principal(user):
        return Principal("user", user["id"], user["name"], user["role"], user["company_id"], set(ROLE_PERMISSIONS[user["role"]]), user_id=user["id"], user_role=user["role"])

    @app.get("/api/commitments")
    def commitments(user=Depends(current_user)):
        need(user, "commitments.read")
        return invoke(eng, _human_principal(user), "commitments.list", {})

    class ProcessDueBody(BaseModel):
        as_of: str | None = Field(default=None, max_length=40)

    @app.post("/api/commitments/process-due")
    def process_due(body: ProcessDueBody, user=Depends(current_user)):
        need(user, "commitments.process")
        try:
            return orch.process_due(user, body.as_of)
        except ToolError as e:
            raise HTTPException(400, str(e))

    class ReassignBody(BaseModel):
        new_owner_user_id: str

    @app.post("/api/commitments/{cid}/reassign")
    def reassign(cid: str, body: ReassignBody, user=Depends(current_user)):
        need(user, "commitments.write")
        try:
            return invoke(eng, _human_principal(user), "commitments.reassign", {"commitment_id": cid, "new_owner_user_id": body.new_owner_user_id})
        except ToolError as e:
            raise HTTPException(400, str(e))

    class CompleteBody(BaseModel):
        evidence: str = Field(min_length=10, max_length=500)

    @app.post("/api/tasks/{tid}/complete")
    def complete_task(tid: str, body: CompleteBody, user=Depends(current_user)):
        try:
            return orch.complete_task(user, tid, body.evidence, assistant_mod.ASSISTANT_BY_ROLE[user["role"]])
        except Denied as e:
            raise HTTPException(403, str(e))
        except ToolError as e:
            raise HTTPException(400, str(e))



    # ---------------- V0.5: configurable companies, employees and assistant templates ----------------
    def _cfg(fn):
        try:
            return fn()
        except cfg.ConfigError as e:
            raise HTTPException(e.status, str(e))

    if not pilot:   # alta local de empresas: SOLO fuera del piloto (allí las da de alta el operador con la CLI)
        @app.get("/api/setup/info")
        def setup_info():
            """Public (no data): is local company creation switched on, and which industry presets exist."""
            return {"company_creation": settings.allow_company_creation, "presets": [{"key": k, "label": v[1]} for k, v in cfg.PRESETS.items()],
                    "note": "Alta de empresas FICTICIAS solo en entorno local; no es un registro público."}

        class NewCompany(BaseModel):
            name: str = Field(min_length=3, max_length=80)
            slug: str | None = Field(default=None, max_length=31)
            industry: str | None = Field(default=None, max_length=120)
            preset: str = Field(default="hvac", max_length=20)
            owner_name: str = Field(min_length=2, max_length=80)
            owner_email: str = Field(min_length=5, max_length=190)
            owner_password: str | None = Field(default=None, min_length=8, max_length=100)

        @app.post("/api/setup/companies")
        def setup_company(body: NewCompany, request: Request):
            if not settings.allow_company_creation or not is_local(request):
                raise HTTPException(404, "no encontrado")   # off by default; loopback only
            return _cfg(lambda: cfg.create_company(eng, name=body.name, slug=body.slug, industry=body.industry, preset=body.preset, owner_name=body.owner_name,
                                                   owner_email=body.owner_email, owner_password=body.owner_password, actor="local-setup"))

    @app.get("/api/config/overview")
    def config_overview(user=Depends(current_user)):
        need(user, "config.manage")
        return {**cfg.overview(eng, user["company_id"]), "templates": describe_templates()}

    @app.get("/api/config/templates")
    def config_templates(user=Depends(current_user)):
        need(user, "config.manage")
        return {"templates": describe_templates()}

    class NewEmployee(BaseModel):
        name: str = Field(min_length=2, max_length=80)
        email: str = Field(min_length=5, max_length=190)
        role: str = Field(max_length=20)
        password: str | None = Field(default=None, min_length=8, max_length=100)
        assign_assistant: bool = True

    @app.post("/api/config/employees")
    def config_add_employee(body: NewEmployee, user=Depends(current_user)):
        need(user, "config.manage")
        return _cfg(lambda: cfg.add_employee(eng, user["company_id"], user, name=body.name, email=body.email, role=body.role, password=body.password, assign_assistant=body.assign_assistant))

    class Assign(BaseModel):
        user_id: str = Field(max_length=40)
        template: str = Field(max_length=40)

    @app.post("/api/config/assignments")
    def config_assign(body: Assign, user=Depends(current_user)):
        need(user, "config.manage")
        return _cfg(lambda: cfg.assign_template(eng, user["company_id"], user, user_id=body.user_id, template=body.template))

    class Enabled(BaseModel):
        enabled: bool

    @app.post("/api/config/agents/{agent_id}/enabled")
    def config_agent_enabled(agent_id: str, body: Enabled, user=Depends(current_user)):
        need(user, "config.manage")
        return _cfg(lambda: cfg.set_agent_enabled(eng, user["company_id"], user, agent_id=agent_id, enabled=body.enabled))

    class Active(BaseModel):
        active: bool

    @app.post("/api/config/employees/{user_id}/active")
    def config_employee_active(user_id: str, body: Active, user=Depends(current_user)):
        need(user, "config.manage")
        res = _cfg(lambda: cfg.set_user_active(eng, user["company_id"], user, user_id=user_id, active=body.active))
        if idp is not None and not body.active and not res.get("deduplicated"):   # la baja LOCAL ya es efectiva (sesiones borradas, `active=0`); además se pide la del proveedor
            with eng.connect() as c:
                subjects = [r["subject"] for r in all_(c, "SELECT subject FROM identities WHERE user_id=:u AND company_id=:c AND provider=:p", u=user_id, c=user["company_id"], p=idp.name)]
            for sub in subjects:
                try:
                    idp.disable(sub)
                    outcome = "ok"
                except Exception:
                    outcome = "failed"
                with eng.begin() as c:
                    audit(c, user["company_id"], "user", user["id"], "auth.idp_disable", user_id, outcome, "")
        return res

    class NewRole(BaseModel):
        role: str = Field(max_length=20)

    @app.post("/api/config/employees/{user_id}/role")
    def config_employee_role(user_id: str, body: NewRole, user=Depends(current_user)):
        need(user, "config.manage")
        return _cfg(lambda: cfg.change_role(eng, user["company_id"], user, user_id=user_id, role=body.role))

    if idp is not None:
        class NewInvitation(BaseModel):
            model_config = ConfigDict(extra="forbid")        # empresa, rol de otro, estado o caducidad NO vienen del cuerpo
            email: str = Field(min_length=5, max_length=190)
            name: str = Field(min_length=2, max_length=80)
            role: str = Field(max_length=20)

        @app.get("/api/config/invitations")
        def config_invitations(user=Depends(current_user)):
            need(user, "config.manage")
            return {"invitations": inv_mod.listing(eng, user["company_id"])}

        @app.post("/api/config/invitations")
        def config_invite(body: NewInvitation, user=Depends(current_user)):
            need(user, "config.manage")
            if user["role"] != "owner":      # (config.manage solo lo tiene el dueño; se comprueba de nuevo antes de gastar el límite)
                raise HTTPException(403, "solo el dueño de la empresa puede invitar")
            ok, wait = limiter.take([ratelimit.Bucket("idp.invite.co", user["company_id"], "-")])
            if not ok:
                raise HTTPException(429, "Demasiadas invitaciones en poco tiempo. Espera antes de crear más.", headers={"Retry-After": str(wait)})
            inv, notify = _cfg(lambda: inv_mod.create(eng, user["company_id"], user, email=body.email, name=body.name, role=body.role))
            if notify:   # el proveedor escribe SOLO al correo invitado, y nunca a uno que ya es de otra empresa; un fallo del proveedor no deshace la invitación (el enlace sigue sirviendo)
                try:
                    idp.invite(notify)
                except Exception:
                    with eng.begin() as c:
                        audit(c, user["company_id"], "user", user["id"], "config.invitation_notify", inv["id"], "failed", "")
            return inv

        @app.post("/api/config/invitations/{iid}/revoke")
        def config_revoke_invitation(iid: str, user=Depends(current_user)):
            need(user, "config.manage")
            return _cfg(lambda: inv_mod.revoke(eng, user["company_id"], user, iid))

    class ManualRequest(BaseModel):
        customer_name: str = Field(min_length=2, max_length=120)
        phone: str | None = Field(default=None, max_length=40)
        description: str = Field(min_length=5, max_length=1000)
        client_request_id: str = Field(min_length=6, max_length=80, pattern=r"^[A-Za-z0-9_\-]+$")

    @app.post("/api/requests/manual")
    def manual_request(body: ManualRequest, user=Depends(current_user)):
        """Owner/administration register a customer request by hand (a company just configured has none). Same registry path as reception: customers.* → requests.create → classify."""
        need(user, "requests.create_manual")
        cid = user["company_id"]
        try:
            rec, coo = orch.principal(cid, "reception"), orch.principal(cid, "coordinator")
            name = " ".join(body.customer_name.split())
            hits = invoke(eng, rec, "customers.find", {"query": name})["matches"]
            ex = next((h for h in hits if h["name"].lower() == name.lower()), None)
            cust = ex["id"] if ex else invoke(eng, rec, "customers.create", {"name": name, "phone": body.phone})["customer"]["id"]
            with eng.begin() as c:
                key = f"in:manual:{body.client_request_id}"
                m = one(c, "SELECT conversation_id FROM messages WHERE company_id=:c AND idem_key=:k", c=cid, k=key)
                if m:
                    conv = m["conversation_id"]
                else:
                    conv = uid_("cnv_")
                    q(c, "INSERT INTO conversations(id,company_id,customer_id,channel,created_at) VALUES(:i,:c,:k,'interno',:t)", i=conv, c=cid, k=cust, t=now())
                    q(c, "INSERT INTO messages(id,company_id,conversation_id,sender,body,status,idem_key,created_at) VALUES(:i,:c,:cv,:s,:b,'recibido',:k,:t)",
                      i=uid_("msg_"), c=cid, cv=conv, s=f"staff:{user['name']}", b=body.description, k=key, t=now())
            out = invoke(eng, rec, "requests.create", {"customer_id": cust, "description": body.description, "idem_key": f"manual:{body.client_request_id}", "conversation_id": conv})
            if not out["deduplicated"]:
                invoke(eng, coo, "requests.classify", {"request_id": out["request_id"]}, provider=orch.provider)
            return {"request_id": out["request_id"], "customer_id": cust, "deduplicated": out["deduplicated"]}
        except (ToolError, Denied) as e:
            raise HTTPException(400, str(e))

    # ---------------- V0.4: conversational coordinator (plans) ----------------
    def _plan_for(user: dict, pid: str) -> dict:
        """Owner sees every plan of THEIR company; employees only plans where a step is assigned to them; anything else is a 404 (no existence leak)."""
        v = orch.plans.view(user["company_id"], pid)
        if not v:
            raise HTTPException(404, "plan no encontrado")
        if user["role"] == "owner":
            return v
        if not any(s["assignee_user_id"] == user["id"] for s in v["steps"]):
            raise HTTPException(404, "plan no encontrado")
        for k in ("text", "excluded", "clarification", "notes", "result_summary", "status_reason"):
            v[k] = None if k not in ("excluded", "notes") else []
        for st in v["steps"]:
            st["result"] = None
            if st["assignee_user_id"] != user["id"]:
                st["evidence"] = None
        return v

    # Employees assigned to a plan may follow the delegation/task story, never the owner's raw instruction, LLM proposals or tool payloads.
    STAFF_PLAN_EVENTS = {"agent.delegated", "task.assigned", "task.queued", "task.completed", "run.waiting_human", "run.completed", "plan.confirmed", "plan.cancelled", "plan.step_verified"}

    def _plan_allow(user: dict):
        return None if user["role"] == "owner" else STAFF_PLAN_EVENTS

    class PlanIn(BaseModel):
        text: str = Field(min_length=5, max_length=600)
        client_plan_id: str = Field(min_length=6, max_length=80, pattern=r"^[A-Za-z0-9_\-]+$")

    def _plan_errors(fn):
        try:
            return fn()
        except Denied as e:
            raise HTTPException(403, str(e))
        except ConflictError as e:
            raise HTTPException(409, str(e))
        except ToolError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/plans")
    def list_plans(user=Depends(current_user)):
        with eng.connect() as c:
            if user["role"] == "owner":
                rows = all_(c, "SELECT id,text,status,source,summary,request_id,created_at FROM plans WHERE company_id=:c ORDER BY created_at DESC LIMIT 40", c=user["company_id"])
            else:
                rows = all_(c, """SELECT DISTINCT p.id,p.status,p.source,p.summary,p.request_id,p.created_at FROM plans p JOIN plan_steps s ON s.plan_id=p.id AND s.company_id=p.company_id
                                  WHERE p.company_id=:c AND s.assignee_user_id=:u ORDER BY p.created_at DESC LIMIT 40""", c=user["company_id"], u=user["id"])
        return {"plans": rows}

    @app.post("/api/plans")
    def create_plan(body: PlanIn, user=Depends(current_user)):
        need(user, "plans.create")
        out = _plan_errors(lambda: orch.plans.create(user, body.text, body.client_plan_id))
        return {**out, "plan": orch.plans.view(user["company_id"], out["plan_id"])}

    @app.get("/api/plans/{pid}")
    def get_plan(pid: str, user=Depends(current_user)):
        v = _plan_for(user, pid)
        with eng.connect() as c:
            cid = user["company_id"]
            tasks_ = all_(c, "SELECT t.id,t.title,t.kind,t.status,t.evidence,u.name AS assignee,u.role AS assignee_role,t.assignee_agent,t.assignee_user_id FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id AND u.company_id=t.company_id WHERE t.company_id=:c AND t.run_id=:r ORDER BY t.created_at", c=cid, r=pid)
            dels = all_(c, "SELECT from_agent,to_agent,task,status FROM delegations WHERE company_id=:c AND run_id=:r ORDER BY created_at", c=cid, r=pid)
            ress = all_(c, "SELECT kind,summary FROM results WHERE company_id=:c AND run_id=:r", c=cid, r=pid)
        for t in tasks_:
            if user["role"] != "owner" and t.pop("assignee_user_id") != user["id"]:
                t["evidence"] = None
            else:
                t.pop("assignee_user_id", None)
        return {**v, "plan_id": pid, "scenario": "E", "phase": None, "provider": v["source"] or "deterministic", "alternatives": [], "approvals": [], "commitments": [],
                "materials": [], "drafts": [], "tasks": tasks_, "delegations": dels, "results": ress}

    @app.get("/api/plans/{pid}/events")
    def plan_events(pid: str, after: int = 0, user=Depends(current_user)):
        need(user, "events.read")
        _plan_for(user, pid)
        allow = _plan_allow(user)
        return {"events": [e for e in fetch(eng, user["company_id"], pid, after) if allow is None or e["type"] in allow]}

    @app.get("/api/plans/{pid}/stream")
    async def plan_stream(pid: str, request: Request, after: int = 0, user=Depends(current_user)):
        need(user, "events.read")
        _plan_for(user, pid)
        return _sse(request, user["company_id"], pid, after, _plan_allow(user))

    @app.post("/api/plans/{pid}/confirm")
    def confirm_plan(pid: str, user=Depends(current_user)):
        need(user, "plans.confirm")  # human-only: no agent holds it
        _plan_for(user, pid)
        return _plan_errors(lambda: orch.plans.confirm(user, pid))

    @app.post("/api/plans/{pid}/cancel")
    def cancel_plan(pid: str, user=Depends(current_user)):
        need(user, "plans.confirm")
        _plan_for(user, pid)
        return _plan_errors(lambda: orch.plans.cancel(user, pid))

    class ClarifyIn(BaseModel):
        request_id: str | None = Field(default=None, max_length=40)
        customer_id: str | None = Field(default=None, max_length=40)
        admin_user_id: str | None = Field(default=None, max_length=40)
        technician_user_id: str | None = Field(default=None, max_length=40)

    @app.post("/api/plans/{pid}/clarify")
    def clarify_plan(pid: str, body: ClarifyIn, user=Depends(current_user)):
        need(user, "plans.create")
        _plan_for(user, pid)
        out = _plan_errors(lambda: orch.plans.clarify(user, pid, body.model_dump(exclude_none=True)))
        return {**out, "plan": orch.plans.view(user["company_id"], pid)}

    if settings.static_dir.exists():
        app.mount("/assets", StaticFiles(directory=settings.static_dir / "assets"), name="assets") if (settings.static_dir / "assets").exists() else None

        @app.get("/")
        def index():
            return FileResponse(settings.static_dir / "index.html")
    return app
