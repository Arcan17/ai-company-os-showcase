"""V0.7 P1 (Issue #19) — modo piloto `AICOS_ENV=pilot`: la versión de la aplicación que se puede exponer a un grupo CERRADO de personas de prueba.

Sin `AICOS_ENV` (o con `local`) NADA de este módulo cambia el comportamiento. Con `pilot`:
  · el arranque FALLA CERRADO si la configuración es insegura (`validate_startup`), antes de abrir o crear la base y sin imprimir valores;
  · no hay demostración: ni empresas sembradas, ni rutas `/demo*`, `/api/demo/*`, `/api/auth/demo-accounts`, `/api/setup/*` (no se registran: 404 real);
  · la cookie de sesión es `__Host-aicos_session` (siempre `Secure`, `Path=/`, sin `Domain`);
  · cabeceras de seguridad en TODAS las respuestas y `Cache-Control: no-store` en `/api/*`;
  · solo se atiende a los nombres de host declarados en `AICOS_TRUSTED_ORIGINS` (nada que venga de internet decide el esquema, el host o la dirección del cliente).
Diseño: docs/v07/P0_ARQUITECTURA_Y_PLAN.md §4 y §6; operación y límites: docs/v07/P1_MODO_PILOTO.md.
"""
from __future__ import annotations

import ipaddress
import json
import sqlite3

from .config import settings

ENVS = ("", "local", "pilot")
COOKIE_LOCAL = "aicos_session"
COOKIE_FLOW_LOCAL = "aicos_flow"            # cookie de correlación del flujo con el proveedor (V0.7 P2): HttpOnly, SameSite=Lax (el callback es una navegación GET de primer nivel), 10 minutos
COOKIE_FLOW_PILOT = "__Host-aicos_flow"
COOKIE_PILOT = "__Host-aicos_session"      # el prefijo `__Host-` obliga al navegador a exigir Secure + Path=/ + sin Domain (RFC 6265bis §4.1.3)
EX_CONFIG = 78                             # código de salida (sysexits) cuando el arranque se niega por configuración

# Los únicos intermediarios cuya cabecera X-Forwarded-For puede creerse en el piloto: la propia máquina o una red PRIVADA. Nunca un rango público (0.0.0.0/0, ::/0, …).
_PRIVATE = tuple(ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))

HSTS = b"max-age=31536000; includeSubDomains"                      # un año; sin `preload` (no se puede deshacer fácilmente)
CSP_ENFORCED = b"frame-ancestors 'none'"                            # solo la protección contra incrustación se APLICA; el resto va en modo informe
CSP_REPORT_ONLY = (b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                   b"object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
OWNED = {b"strict-transport-security", b"x-content-type-options", b"referrer-policy", b"x-frame-options", b"content-security-policy", b"content-security-policy-report-only"}
BASE_HEADERS = [(b"strict-transport-security", HSTS), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                (b"content-security-policy", CSP_ENFORCED), (b"content-security-policy-report-only", CSP_REPORT_ONLY)]


class PilotConfigError(RuntimeError):
    """El arranque en modo piloto se niega a continuar: la configuración es insegura. El mensaje nombra las variables, NUNCA sus valores."""


def mode() -> str:
    return (settings.env or "").strip().lower()


def is_pilot() -> bool:
    return mode() == "pilot"


def cookie_name() -> str:
    return COOKIE_PILOT if is_pilot() else COOKIE_LOCAL


def flow_cookie_name() -> str:
    return COOKIE_FLOW_PILOT if is_pilot() else COOKIE_FLOW_LOCAL


# ───────────────────────────── arranque seguro ─────────────────────────────

def _proxy_problem() -> bool:
    """¿Hay en AICOS_TRUSTED_PROXIES algo que no sea la propia máquina o una red privada (o que no se pueda interpretar)?"""
    for raw in settings.trusted_proxies:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            return True
        if not any(net.version == p.version and net.subnet_of(p) for p in _PRIVATE):
            return True
    return False


def _demo_accounts_problem(db_url: str) -> str | None:
    """None si la base NO tiene cuentas de demostración; si no, el motivo (sin datos). Abre la base SOLO LECTURA: nunca crea tablas, ni migra, ni escribe."""
    if not db_url.startswith("sqlite:///"):
        return "AICOS_DB_URL: el modo piloto solo sabe comprobar bases SQLite"
    path = db_url[len("sqlite:///"):].split("?")[0]
    if path in ("", ":memory:"):
        return None
    import os
    if not os.path.exists(path):
        return None                                                    # base nueva: la creará `init_db`, vacía
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
                return None                                            # una base sin tabla de usuarios no tiene cuentas de demostración
            n = db.execute("SELECT COUNT(*) FROM users WHERE is_demo=1").fetchone()[0]
    except sqlite3.Error:
        return "AICOS_DB_URL: no se pudo comprobar la base (el piloto no arranca sobre una base que no puede verificar)"
    return "AICOS_DB_URL apunta a una base con cuentas de demostración (is_demo=1): el piloto necesita una base propia, creada vacía" if n else None


REAUTH_MAX_PILOT = 900                     # el piloto exige autenticación reciente de 15 minutos como MÁXIMO (V0.7 P2)


def validate_startup(db_url: str, provider=None) -> None:
    """Se llama al PRINCIPIO de `create_app`, antes de abrir la base. Lanza PilotConfigError (y nada más ocurre) si el modo no se reconoce o, en piloto, si algo es inseguro.
    `provider` = el proveedor de identidad inyectado (V0.7 P2), si lo hay."""
    m = mode()
    if m not in ENVS:
        raise PilotConfigError("AICOS_ENV no tiene un valor reconocido (use «local» o «pilot»); el arranque se detiene antes de abrir la base de datos.")
    if (settings.idp_provider or "").strip():                           # en TODOS los entornos: ningún valor de configuración selecciona un proveedor en esta versión (el simulado solo se inyecta desde las pruebas)
        raise PilotConfigError("AICOS_IDP_PROVIDER no admite ningún valor en esta versión: no hay proveedor de identidad configurable (la integración real es la etapa P3 y el proveedor "
                               "simulado solo se inyecta desde las pruebas). Deje la variable vacía; el arranque se detiene antes de abrir la base de datos.")
    if m != "pilot":
        return
    bad = []
    if provider is not None and getattr(provider, "is_fake", True) is not False:
        bad.append("el proveedor de identidad SIMULADO no puede usarse en el piloto: solo un proveedor que declare no ser simulado puede iniciar sesiones aquí")
    if not 1 <= settings.reauth_max_age_seconds <= REAUTH_MAX_PILOT:
        bad.append("AICOS_REAUTH_MAX_AGE_SECONDS debe estar entre 1 y 900: la autenticación reciente que exigen las operaciones sensibles no puede pasar de 15 minutos")
    if settings.cookie_secure != "1":
        bad.append("AICOS_COOKIE_SECURE debe ser «1»: en el piloto la cookie de sesión es siempre Secure")
    if not settings.trusted_origins:
        bad.append("AICOS_TRUSTED_ORIGINS no puede estar vacío: declare el nombre de host público del piloto (solo se atiende a esos nombres)")
    if settings.demo_mode:
        bad.append("AICOS_DEMO_MODE no puede estar activo: el piloto no tiene superficie de demostración")
    if settings.allow_company_creation:
        bad.append("AICOS_ALLOW_COMPANY_CREATION no puede estar activo: las empresas las da de alta el operador con la CLI, no la aplicación")
    if _proxy_problem():
        bad.append("AICOS_TRUSTED_PROXIES solo puede nombrar la propia máquina o redes privadas: no se confía en X-Forwarded-For de direcciones públicas ni en rangos que las incluyan")
    why = _demo_accounts_problem(db_url)
    if why:
        bad.append(why)
    if bad:
        raise PilotConfigError("AICOS_ENV=pilot se niega a arrancar (configuración insegura):\n  - " + "\n  - ".join(bad))


# ───────────────────────────── cabeceras y host (ASGI puro, como csrf.py: no rompe SSE) ─────────────────────────────

class SecurityHeaders:
    """Añade las cabeceras de seguridad a TODA respuesta http (también a los 400/403/429/503 que generan otros middlewares: por eso es el más externo) y `no-store` a /api/*."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        api = scope["path"].startswith("/api/")

        async def wrapped(msg):
            if msg["type"] == "http.response.start":
                drop = OWNED | ({b"cache-control"} if api else set())
                headers = [(k, v) for k, v in msg.get("headers", []) if k.lower() not in drop] + BASE_HEADERS + ([(b"cache-control", b"no-store")] if api else [])
                msg = {**msg, "headers": headers}
            await send(msg)

        await self.app(scope, receive, wrapped)


class HostGuard:
    """Solo atiende a los nombres de host declarados (AICOS_TRUSTED_ORIGINS, formato host[:puerto]): un Host inventado no llega a ninguna ruta."""

    def __init__(self, app, allowed: tuple):
        self.app, self.allowed = app, frozenset(a.lower() for a in allowed)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = next((v.decode("latin-1").strip().lower() for k, v in scope["headers"] if k == b"host"), "")
            if host not in self.allowed:
                body = json.dumps({"detail": "host no permitido"}).encode()
                await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        return await self.app(scope, receive, send)
