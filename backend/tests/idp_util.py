"""Ayudas de las pruebas de V0.7 P2 (Issue #23): proveedor de identidad SIMULADO inyectado, reloj controlado y empresas ficticias.
Todo con bases TEMPORALES y SIN red: el simulado (`app.idp_fake`) no abre ningún socket y sus direcciones usan el TLD reservado `.invalid`.
No hay forma de elegir el simulado por configuración: solo estas pruebas lo inyectan en `create_app(..., identity_provider=...)`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import company_admin as cfg
from app.api import create_app
from app.config import settings
from app.db import one
from app.security import create_session

from .pilot_util import PILOT_HOST, PILOT_URL, PW, Session, pilot_on

LOCAL_URL = "http://testserver"
STAFF_PW = "clave-de-personal-123"      # distinta de la del dueño (PW): «la clave de otra persona no reautentica»
FAIL_LOCATION = "/#/login?error=idp"          # la ÚNICA respuesta de un callback rechazado, sea cual sea el motivo
OK_LOCATION = "/#/dashboard"
GENERIC_START_FAIL = {"detail": "no se pudo iniciar el acceso"}


def fake_provider(**kw):
    from app.idp_fake import FakeProvider            # import perezoso: el módulo simulado NO se carga salvo que una prueba lo pida
    return FakeProvider(**kw)


class FrozenClock:
    """Reloj de las pruebas: parte del instante real (otras partes del sistema usan la hora real) y SOLO avanza cuando la prueba lo dice."""

    def __init__(self, start: datetime | None = None):
        self.t = start or datetime.now(timezone.utc).replace(microsecond=0)      # sin microsegundos: las marcas se guardan con milisegundos y la prueba de «justo 15:00» debe ser exacta

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)

    def install(self, mp) -> "FrozenClock":
        from app import clock                          # import perezoso: así una prueba falla por SU motivo (falta `app.clock`) y no como error de colección de todo el módulo
        mp.setattr(clock, "utcnow", self)
        return self

    def iso(self, **delta) -> str:
        return (self.t + timedelta(**delta)).isoformat(timespec="milliseconds")


def cookie_names(env: str) -> tuple[str, str]:
    return ("__Host-aicos_session", "__Host-aicos_flow") if env == "pilot" else ("aicos_session", "aicos_flow")


class World:
    """Una aplicación con proveedor inyectado (o sin él) y sus empresas ficticias. `env` = «local» (cookie sin prefijo) o «pilot» (cookie `__Host-`)."""

    def __init__(self, app, prov, env: str, base_url: str):
        self.app, self.prov, self.env, self.base_url = app, prov, env, base_url
        self.session_cookie, self.flow_cookie = cookie_names(env)
        self.eng = app.state.eng
        self.db_path = self.eng.url.database

    # ---- empresas y sesiones
    def company(self, slug: str, name: str | None = None) -> Session:
        """Crea la empresa con la MISMA función que la CLI del operador y devuelve la sesión de su dueño (creada en la base: no depende de que exista el acceso por contraseña)."""
        made = cfg.create_company(self.eng, name=name or slug.title(), slug=slug, owner_name=f"Dueño de {slug}", owner_email=f"dueno@{slug}.test", owner_password=PW, actor="test")
        s = self.session(f"dueno@{slug}.test")
        s.company_id, s.slug = made["company_id"], slug
        return s

    def relogin(self, owner: Session) -> Session:
        """Una sesión NUEVA del dueño (las sesiones duran 12 h: una prueba que mueve el reloj días debe volver a entrar, como lo haría la persona)."""
        s = self.session(f"dueno@{owner.slug}.test")
        s.company_id, s.slug = owner.company_id, owner.slug
        return s

    def staff(self, owner: Session, role: str, n: int = 1) -> Session:
        """Una persona de administración o un técnico de esa empresa (alta con la API del dueño) con sesión propia."""
        email = f"{role}{n}@{owner.slug}.test"
        r = owner.post("/api/config/employees", json={"name": f"{role.title()} {n}", "email": email, "role": role, "password": STAFF_PW})
        assert r.status_code == 200, r.text
        s = self.session(email)
        s.company_id, s.slug, s.user_id = owner.company_id, owner.slug, r.json()["user_id"]
        return s

    def session(self, email: str, *, auth_age: float | None = 0, auth_time: str | None = "default") -> Session:
        """Sesión REAL de la base para `email`. `auth_age` = segundos desde la última autenticación; `auth_time=None` guarda NULL (sesión «sin tiempo de autenticación»)."""
        s = Session(self.app, self.base_url)
        with self.eng.begin() as c:
            u = one(c, "SELECT * FROM users WHERE lower(email)=:e", e=email.lower())
            from app import clock
            at = (clock.utcnow() - timedelta(seconds=auth_age or 0)).isoformat(timespec="milliseconds") if auth_time == "default" else auth_time
            token = create_session(c, u, settings.session_hours, auth_time=at)
        host = urlsplit(self.base_url).hostname
        s.http.cookies.set(self.session_cookie, token, domain=host if "." in host else host + ".local")     # el dominio que el cliente asignaría a la cookie de la propia respuesta (así NO hay dos cookies con el mismo nombre)
        s.csrf = s.http.get("/api/auth/me").json()["csrf_token"]
        return s

    def browser(self, ip: str | None = None) -> TestClient:
        """El navegador de OTRA persona (sin cookies). `ip` = la dirección de origen que verá el servidor (por defecto la del propio cliente de pruebas)."""
        return TestClient(self.app, base_url=self.base_url, **({"client": (ip, 50000)} if ip else {}))

    def all_text(self) -> str:
        """TODO el contenido de TODAS las tablas como texto (para comprobar que un secreto o una dirección no está en ninguna parte)."""
        out = []
        with self.eng.connect() as c:
            raw = c.connection.dbapi_connection
            for (t,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                for row in raw.execute(f'SELECT * FROM "{t}"').fetchall():
                    out.append(" ".join(str(v) for v in row))
        return "\n".join(out)

    # ---- invitaciones y flujo del proveedor
    def invite(self, owner: Session, email: str, *, name: str = "Ana Técnica", role: str = "technician") -> dict:
        r = owner.post("/api/config/invitations", json={"email": email, "name": name, "role": role})
        assert r.status_code == 200, r.text
        return r.json()

    def start(self, http: TestClient, body: dict | None = None, **kw):
        return http.post("/api/auth/idp/start", json=body if body is not None else {}, **kw)

    def go(self, who, subject: str, *, token: str | None = None, auth_age: float = 0, reauth: bool = False, **login_kw):
        """Una persona recorre el flujo: pide el inicio (con su invitación, si la tiene), «inicia sesión» en el proveedor simulado y vuelve por el callback.
        `who` = un navegador (TestClient) o una `Session` (con su token CSRF). → (respuesta del callback, url del proveedor)."""
        http = who.http if isinstance(who, Session) else who
        headers = {"X-CSRF-Token": who.csrf} if isinstance(who, Session) and who.csrf else {}
        body = {"reauth": True} if reauth else ({"invite_token": token} if token else {})
        r = self.start(http, body, headers=headers)
        assert r.status_code == 200, r.text
        url = r.json()["authorize_url"]
        back = self.prov.login(url, subject, auth_age_seconds=auth_age, **login_kw)
        return http.get("/api/auth/callback", params=back, follow_redirects=False), url

    def accept(self, token: str, email: str, *, subject: str | None = None, name: str | None = None, verified: bool = True, auth_age: float = 0):
        """El invitado abre su enlace en SU navegador, crea su cuenta en el proveedor (correo verificado o no) y vuelve. → (navegador, respuesta del callback, subject)."""
        subject = subject or self.prov.account(email, email_verified=verified, name=name)
        http = self.browser()
        resp, _ = self.go(http, subject, token=token, auth_age=auth_age)
        return http, resp, subject

    # ---- lecturas directas de la base (solo para comprobar el estado, nunca para preparar un ataque)
    def rows(self, sql: str, **p) -> list[dict]:
        with self.eng.connect() as c:
            return [dict(r) for r in c.execute(text(sql), p).mappings().all()]

    def count(self, table: str, where: str = "1=1", **p) -> int:
        with self.eng.connect() as c:
            return c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), p).scalar()


def make_world(tmp_path, mp, *, env: str = "local", provider="fake", **settings_over) -> World:
    mp.setattr(settings, "sync_runs", True)
    if env == "pilot":
        pilot_on(mp)
        base = PILOT_URL
    else:
        mp.setattr(settings, "env", "", raising=False)
        mp.setattr(settings, "cookie_secure", "auto")
        base = LOCAL_URL
    for k, v in settings_over.items():
        mp.setattr(settings, k, v, raising=False)
    prov = fake_provider() if provider == "fake" else provider
    app = create_app(f"sqlite:///{tmp_path / 'p2.db'}", identity_provider=prov)
    return World(app, prov, env, base)


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def set_cookies(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


def issues_session(resp, cookie: str) -> bool:
    """¿La respuesta entrega una cookie de sesión VIVA (no un borrado)?"""
    return any(c.startswith(cookie + "=") and "Max-Age=0" not in c and not c.startswith(cookie + '=""') and not c.startswith(cookie + "=;") for c in set_cookies(resp))
