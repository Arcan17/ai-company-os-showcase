"""Ayudas de las pruebas del modo piloto (V0.7 P1, Issue #19). Datos 100 % sintéticos: empresas FICTICIAS creadas con la misma función que usa la CLI del operador (no la siembra de demostración)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import company_admin as cfg
from app.config import settings
from app.db import md

PILOT_HOST = "pilot.example.test"
PILOT_URL = f"https://{PILOT_HOST}"
PW = "clave-ficticia-123"
SAFE = {"GET", "HEAD", "OPTIONS"}


def pilot_on(mp, **over):
    """Aplica una configuración de piloto VÁLIDA (más las variaciones `over`) sobre `settings` con un MonkeyPatch. Devuelve el mismo `mp`."""
    base = dict(env="pilot", cookie_secure="1", trusted_origins=(PILOT_HOST,), demo_mode=False, allow_company_creation=False, trusted_proxies=())
    for k, v in {**base, **over}.items():
        mp.setattr(settings, k, v, raising=False)
    return mp


class Session:
    """Cliente con sesión que lleva SU token CSRF (el atajo de conftest solo conoce la cookie `aicos_session`, que en el piloto se llama distinto)."""

    def __init__(self, app, base_url: str = PILOT_URL):
        self.app, self.http, self.csrf = app, TestClient(app, base_url=base_url), None

    def login(self, email: str, password: str = PW) -> "Session":
        r = self.http.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]
        return self

    def request(self, method: str, url: str, **kw):
        if method.upper() not in SAFE and self.csrf:
            kw["headers"] = {**(kw.get("headers") or {}), "X-CSRF-Token": self.csrf}
        return self.http.request(method, url, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.http.cookies.jar)


@dataclass
class Co:
    """Una empresa ficticia con su dueño, una persona de administración y un técnico (cada uno con su asistente), ya con sesión iniciada."""
    slug: str
    name: str
    company_id: str
    owner: Session
    admin: Session
    tech: Session
    ids: dict = field(default_factory=dict)       # owner_id, admin_id, tech_id


def make_company(app, slug: str, name: str, base_url: str = PILOT_URL) -> Co:
    made = cfg.create_company(app.state.eng, name=name, slug=slug, owner_name=f"Dueño de {name}", owner_email=f"dueno@{slug}.test", owner_password=PW, actor="test")
    owner = Session(app, base_url).login(f"dueno@{slug}.test")
    co = Co(slug, name, made["company_id"], owner, None, None)       # type: ignore[arg-type]
    for key, role in (("admin", "admin"), ("tech", "technician")):
        r = owner.post("/api/config/employees", json={"name": f"{key.title()} de {name}", "email": f"{key}@{slug}.test", "role": role, "password": PW})
        assert r.status_code == 200, r.text
        co.ids[key + "_id"] = r.json()["user_id"]
        setattr(co, key, Session(app, base_url).login(f"{key}@{slug}.test"))
    co.ids["owner_id"] = owner.get("/api/auth/me").json()["id"]
    return co


# ---------------------------------------------------------------- instantánea de los datos de UNA empresa (para probar «no cambió nada»)
def tenant_tables() -> list[str]:
    return sorted(t.name for t in md.sorted_tables if "company_id" in t.c)


def snapshot(eng, company_id: str) -> dict:
    """{tabla: sha256 de TODAS las filas de esa empresa}: si un intento ajeno cambia o borra algo, cambia el sha."""
    out = {}
    with eng.connect() as c:
        for t in tenant_tables():
            rows = [tuple(r) for r in c.execute(text(f"SELECT * FROM {t} WHERE company_id=:c"), {"c": company_id}).fetchall()]
            out[t] = hashlib.sha256(json.dumps(sorted(map(repr, rows))).encode()).hexdigest()
    return out
