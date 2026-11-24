"""V0.7 P1 (Issue #19, AC5/AC6) — MATRIZ DINÁMICA de aislamiento entre empresas, generada desde el inventario de rutas (OpenAPI de la app viva) y del registro de herramientas.

Qué prueba (empresa A = la «víctima», con datos de todos los tipos; empresa B = la «atacante», con sesión de dueño/administración/técnico):
  1. Por cada ruta autenticada con un identificador (en la ruta, la consulta o el cuerpo) y por cada identificador: B repite una llamada VÁLIDA de B cambiando ese identificador por el de A.
     ORÁCULO: la respuesta debe ser IDÉNTICA a la que recibe con un identificador inexistente («misma respuesta para ausente y de otra empresa», sin canal de existencia) y nunca 2xx;
     ni una cadena de A en la respuesta; los datos de A no cambian (huella SHA-256 de todas sus filas antes y después). CONTROL: la llamada sin cambiar nada SÍ funciona y SÍ difiere
     de la respuesta del id inexistente (si no, la prueba sería vacía: fallaría por otra razón).
  2. Las listas de B (todas las rutas GET autenticadas sin identificador) no contienen nada de A.   3. SSE: B no puede abrir los flujos de A y los suyos no transportan eventos de A.
  4. Capa de herramientas: las 47 herramientas del registro, llamadas directamente con un principal de B, igual que 1 (alcanza también las que solo llaman los agentes).
  5. Canal público: el token y los códigos de A no abren nada con el slug de B.
  6. COMPLETITUD: una ruta con identificador (o una herramienta) SIN caso ni justificación hace fallar la prueba.
Datos 100 % ficticios, bases temporales."""
import asyncio
import itertools
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import create_app
from app.config import settings
from app.events import fetch
from app.tools import Denied, Principal, ToolError, invoke
from app.tools.registry import REGISTRY

from .pilot_util import PILOT_HOST, PILOT_URL, PW, Session, make_company, pilot_on, snapshot

GAS_MSG = "Hola, mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas."
PLACA_MSG = "Mi aire no enciende y la pantalla muestra error E6"
MAINT_MSG = "Hola, quiero agendar una mantención preventiva de mi split del living."
IDEMPOTENCY_KEYS = {"client_msg_id", "client_plan_id", "client_request_id"}          # claves de idempotencia que ELIGE el cliente: no son referencias a objetos de nadie
SKU = "GAS-R410"


class Out(NamedTuple):
    status: int
    text: str


# ───────────────────────────── mundo: empresa A (víctima) y B (atacante) ─────────────────────────────

def _rows(app, sql, **p):
    with app.state.eng.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), p)]


def _uniq():
    return uuid.uuid4().hex[:10]


def b_pending(w, co, tag):
    """Chat → run esperando aprobación (con aprobación pendiente, solicitud, cliente, conversación y un horario libre del técnico)."""
    name = f"Canary{tag} Cliente {_uniq()}"
    r = w.anon.post(f"/api/public/{co.slug}/chat", json={"message": GAS_MSG, "name": name, "phone": "+56 9 5555 0101", "client_msg_id": f"{tag}-{_uniq()}"})
    assert r.status_code == 200, r.text
    run = co.owner.get(f"/api/runs/{r.json()['run_id']}").json()
    assert run["status"] == "waiting_approval", run
    ap = [a for a in co.owner.get("/api/dashboard").json()["pending_approvals"] if a["run_id"] == run["id"]][0]
    conv = _rows(w.app, "SELECT id FROM conversations WHERE company_id=:c AND token=:t", c=co.company_id, t=r.json()["conversation_token"])[0]["id"]
    cust = _rows(w.app, "SELECT customer_id AS x FROM requests WHERE id=:i AND company_id=:c", i=run["request_id"], c=co.company_id)[0]["x"]
    slot = _rows(w.app, "SELECT id FROM availability WHERE company_id=:c AND status='free' ORDER BY slot_start LIMIT 1", c=co.company_id)[0]["id"]
    return {"run": run["id"], "request": run["request_id"], "approval": ap["id"], "customer": cust, "token": r.json()["conversation_token"], "conv": conv, "slot": slot, "customer_name": name}


def b_blocked(w, co, tag):
    r = w.anon.post(f"/api/public/{co.slug}/chat", json={"message": PLACA_MSG, "name": f"Canary{tag} Placa {_uniq()}", "phone": "+56 9 5555 0102", "client_msg_id": f"{tag}-{_uniq()}"})
    assert r.status_code == 200, r.text
    run = co.owner.get(f"/api/runs/{r.json()['run_id']}").json()
    assert run["status"] == "blocked", run
    return {"blocked_run": run["id"], "blocked_request": run["request_id"]}


def b_maint(w, co, tag):
    """Escenario D: chat → aprobación → el técnico pide material por su asistente → solicitud de material, compromisos y tareas de la empresa."""
    name = f"Canary{tag} Mant {_uniq()}"
    r = w.anon.post(f"/api/public/{co.slug}/chat", json={"message": MAINT_MSG, "name": name, "phone": "+56 9 5555 0103", "client_msg_id": f"{tag}-{_uniq()}"})
    run = co.owner.get(f"/api/runs/{r.json()['run_id']}").json()
    ap = [a for a in co.owner.get("/api/dashboard").json()["pending_approvals"] if a["run_id"] == run["id"]][0]
    assert co.owner.post(f"/api/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code == 200
    out = co.tech.post("/api/assistant/chat", json={"message": f"necesito dos cilindros de gas para la visita de {name} antes de ir"}).json()
    assert out["tool_calls"][0]["ok"], out
    req = run["request_id"]
    mat = _rows(w.app, "SELECT id FROM material_requests WHERE company_id=:c AND request_id=:r", c=co.company_id, r=req)[0]["id"]
    cmt = _rows(w.app, "SELECT id FROM commitments WHERE company_id=:c AND request_id=:r ORDER BY created_at LIMIT 1", c=co.company_id, r=req)[0]["id"]
    return {"maint_run": run["id"], "maint_request": req, "material": mat, "commitment": cmt}


_SEQ = itertools.count(1)


def _word(n: int, k: int) -> str:
    """Una «palabra» de 7 letras distinta para cada (n, k): inyectiva (n·7919 + k·104729 + c no se repite), sin azar."""
    v, out = n * 7919 + k * 104729 + 12345, ""
    for _ in range(7):
        v, r = divmod(v, 26)
        out += chr(97 + r)
    return out.title()


def _manual_request(co) -> str:
    """Una solicitud manual de un cliente ÚNICO. → la frase que el coordinador entiende.
    El cliente se reconoce por TELÉFONO y por palabras del nombre: un teléfono o una palabra repetidos fusionan clientes y el coordinador pide aclaración. Por eso nada de esto usa azar
    (una versión con 4 dígitos aleatorios colisionaba ~18 % de las corridas: lo mostró el primer CI)."""
    n = next(_SEQ)
    cust = f"{_word(n, 0)} {_word(n, 1)}"
    r = co.owner.post("/api/requests/manual", json={"customer_name": cust, "phone": f"+56 9 7{n:07d}", "description": "Quiero una mantención preventiva del split del local, por favor.",
                                                     "client_request_id": "m" + _uniq()})
    assert r.status_code == 200, r.text
    return f"Revisa la mantención pendiente de {cust}; pide a administración que verifique materiales."


def b_plan(w, co, tag):
    pl = co.owner.post("/api/plans", json={"text": _manual_request(co), "client_plan_id": "p" + _uniq()}).json()["plan"]
    assert pl["status"] == "awaiting_user_confirmation", pl
    return {"plan": pl["id"]}


def b_plan_task(w, co, tag):
    pl = co.owner.post("/api/plans", json={"text": _manual_request(co), "client_plan_id": "p" + _uniq()}).json()["plan"]
    r = co.owner.post(f"/api/plans/{pl['id']}/confirm")
    assert r.status_code == 200, (pl["status"], r.text)
    t = [x for x in co.admin.get("/api/tasks").json()["tasks"] if x["run_id"] == pl["id"]][0]
    return {"plan_done": pl["id"], "task": t["id"]}


def b_clarify(w, co, tag):
    """Un plan que PREGUNTA a quién asignar (hay dos personas de administración): el único estado en que `clarify` valida de verdad los ids de usuario."""
    r = co.owner.post("/api/config/employees", json={"name": f"Segundo Admin {_uniq()}", "email": f"adm2-{_uniq()}@{co.slug}.test", "role": "admin", "password": PW})
    assert r.status_code == 200, r.text
    pl = co.owner.post("/api/plans", json={"text": _manual_request(co), "client_plan_id": "p" + _uniq()}).json()["plan"]
    assert pl["status"] == "needs_clarification", pl
    assert co.owner.post(f"/api/config/employees/{r.json()['user_id']}/active", json={"active": False}).status_code == 200      # la pregunta ya está hecha; el resto de escenarios vuelve a tener UNA persona de administración
    return {"clarify_plan": pl["id"]}


BUILD: dict[str, Callable] = {"pending": b_pending, "blocked": b_blocked, "maint": b_maint, "plan": b_plan, "plan_task": b_plan_task, "clarify": b_clarify}
ALL_NEEDS = tuple(BUILD)


def ghost(v: str) -> str:
    """Un id con la misma forma que `v` que NO existe."""
    head = v.split("_")[0] + "_" if "_" in v[:6] else ""
    return head + secrets.token_hex(max(6, (len(v) - len(head)) // 2))


@dataclass
class World:
    app: object
    anon: TestClient
    A: object
    B: object
    victim: dict = field(default_factory=dict)        # kind → id de A
    markers: dict = field(default_factory=dict)       # nombre → cadena de A que NUNCA debe aparecer en una respuesta a B

    def static(self, co):
        with self.app.state.eng.begin() as c:                                      # una nota privada de la persona de administración (la lee `notes.read_others`)
            if not c.execute(text("SELECT 1 FROM private_notes WHERE company_id=:c AND user_id=:u"), {"c": co.company_id, "u": co.ids["admin_id"]}).fetchone():
                c.execute(text("INSERT INTO private_notes(id,company_id,user_id,body,created_at) VALUES(:i,:c,:u,:b,'2026-01-01T00:00:00.000+00:00')"),
                          {"i": "not_" + _uniq(), "c": co.company_id, "u": co.ids["admin_id"], "b": f"Nota privada ficticia de {co.name} (Canary{co.name.split()[0]})"})
        return {"owner_user": co.ids["owner_id"], "admin_user": co.ids["admin_id"], "tech_user": co.ids["tech_id"],
                "agent": _rows(self.app, "SELECT id FROM agents WHERE company_id=:c AND role='coordinator'", c=co.company_id)[0]["id"]}

    def fresh(self, co, needs, tag):
        from app.seed import add_slots
        with self.app.state.eng.begin() as c:
            add_slots(c, co.company_id, co.ids["tech_id"])                       # cada escenario aprobado consume un horario del técnico: se reponen para que ninguno quede «sin disponibilidad»
            if co is self.B:                                                      # la empresa atacante cierra sus planes abiertos (hay un tope por empresa); los de A, la víctima, NO se tocan jamás
                c.execute(text("UPDATE plans SET status='cancelled' WHERE company_id=:c AND status IN ('awaiting_user_confirmation','needs_clarification','running','waiting_human')"), {"c": co.company_id})
        ids = self.static(co)
        for n in needs:
            ids.update(BUILD[n](self, co, tag))
        return ids


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        pilot_on(mp)
        mp.setattr(settings, "step_delay", 0)
        mp.setattr(settings, "sync_runs", True)
        mp.setattr(settings, "llm_provider", "deterministic")
        mp.setattr(settings, "rl_chat_src", "100000/3600")                  # la matriz crea decenas de conversaciones desde UNA dirección: se aflojan SOLO sus límites de alta, aquí, explícitamente
        mp.setattr(settings, "rl_chat_co", "100000/3600")
        app = create_app(f"sqlite:///{tmp_path_factory.mktemp('matrix') / 'm.db'}")
        A, B = make_company(app, "alfa-aisl", "Alfa Aislamiento"), make_company(app, "beta-aisl", "Beta Aislamiento")
        w = World(app, TestClient(app, base_url=PILOT_URL), A, B)
        w.victim = w.fresh(A, ALL_NEEDS, "Alfa")
        # el código de confirmación de la visita y el segundo token público de A (visibles solo con el token de A)
        ap = A.owner.post(f"/api/approvals/{w.victim['approval']}/decide", json={"decision": "approved"})
        assert ap.status_code == 200
        box = w.anon.get(f"/api/public/{A.slug}/outbox", params={"token": w.victim["token"]}).json()
        w.victim["visit_code"] = [m for m in box["messages"] if m["status"] == "preparado_no_enviado"][0]["body"].split("confirmación: ")[1].strip(".")
        w.markers = {"empresa": A.name, "slug": A.slug, "dominio": f"@{A.slug}.test", "cliente": "CanaryAlfa", **{f"id:{k}": v for k, v in w.victim.items() if isinstance(v, str) and len(v) >= 6}}
        w.markers.update({f"id:{k}": v for k, v in w.static(A).items()})
        yield w


# ───────────────────────────── utilidades de la matriz ─────────────────────────────

def leaks(w, body: str, sent: str = "") -> list[str]:
    """Marcas de A presentes en `body`, salvo las que el propio atacante ENVIÓ (un eco de su entrada no es una fuga)."""
    return [n for n, s in w.markers.items() if s in body and s not in sent]


def norm(text_: str, *ids_groups) -> str:
    vals = sorted({v for g in ids_groups for v in g.values() if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True)
    for v in vals:
        text_ = text_.replace(v, "<id>")
    return re.sub(r"\b[a-z]{3}_[0-9a-f]{12}\b", "<id>", text_)                                                           # y los ids que la propia llamada GENERA (tsk_…, mat_…): distintos en cada intento


def http(actor: Session, method: str, path: str, body=None) -> Out:
    r = actor.request(method, path, json=body) if body is not None else actor.request(method, path)
    return Out(r.status_code, r.text)


async def _asgi_get(app, path: str, cookie: str, host: str, hold: float = 0.6):
    """GET en streaming sobre la app REAL (TestClient no entrega flujos infinitos): devuelve (estado, texto recibido durante `hold` segundos). Cierra con `http.disconnect`."""
    state, chunks, first, stop = {"status": None}, [], asyncio.Event(), asyncio.Event()

    async def receive():
        await stop.wait()
        return {"type": "http.disconnect"}

    async def send(m):
        if m["type"] == "http.response.start":
            state["status"] = m["status"]
        elif m["type"] == "http.response.body":
            chunks.append(m.get("body", b""))
            first.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", host.encode()), (b"cookie", cookie.encode())], "scheme": "https", "server": (host, 443), "client": ("testclient", 50000)}
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first.wait(), 3)
        await asyncio.sleep(hold)
    except asyncio.TimeoutError:
        pass
    stop.set()
    try:
        await asyncio.wait_for(task, 3)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
    return state["status"], b"".join(chunks).decode()


def sse(w, actor: Session, path: str) -> Out:
    return Out(*asyncio.run(_asgi_get(w.app, path, actor.cookie_header, PILOT_HOST)))


# ───────────────────────────── 1. rutas con identificador ─────────────────────────────

@dataclass
class C:
    actor: str                                   # owner | admin | tech (de B)
    needs: tuple
    path: Callable
    swap: tuple
    body: Callable | None = None
    method: str = "GET"
    ok: tuple = (200,)
    stream: bool = False


ROUTES: dict[tuple, list[C]] = {
    ("GET", "/api/requests/{rid}"): [C("owner", ("pending",), lambda i: f"/api/requests/{i['request']}", ("request",))],
    ("POST", "/api/approvals/{aid}/decide"): [C("owner", ("pending",), lambda i: f"/api/approvals/{i['approval']}/decide", ("approval",), lambda i: {"decision": "approved"}, "POST")],
    ("GET", "/api/runs/{rid}"): [C("owner", ("pending",), lambda i: f"/api/runs/{i['run']}", ("run",))],
    ("GET", "/api/runs/{rid}/events"): [C("owner", ("pending",), lambda i: f"/api/runs/{i['run']}/events", ("run",))],
    ("GET", "/api/runs/{rid}/stream"): [C("owner", ("pending",), lambda i: f"/api/runs/{i['run']}/stream", ("run",), stream=True)],
    ("POST", "/api/runs/{rid}/resolve"): [C("owner", ("blocked",), lambda i: f"/api/runs/{i['blocked_run']}/resolve", ("blocked_run",), lambda i: {"action": "diagnostic_only"}, "POST")],
    ("POST", "/api/commitments/{cid}/reassign"): [C("owner", ("maint",), lambda i: f"/api/commitments/{i['commitment']}/reassign", ("commitment", "admin_user"),
                                                     lambda i: {"new_owner_user_id": i["admin_user"]}, "POST")],
    ("POST", "/api/tasks/{tid}/complete"): [C("admin", ("plan_task",), lambda i: f"/api/tasks/{i['task']}/complete", ("task",), lambda i: {"evidence": "Revisé el inventario ficticio: todo completo."}, "POST")],
    ("POST", "/api/config/agents/{agent_id}/enabled"): [C("owner", (), lambda i: f"/api/config/agents/{i['agent']}/enabled", ("agent",), lambda i: {"enabled": True}, "POST")],
    ("POST", "/api/config/employees/{user_id}/active"): [C("owner", (), lambda i: f"/api/config/employees/{i['admin_user']}/active", ("admin_user",), lambda i: {"active": True}, "POST")],
    ("POST", "/api/config/employees/{user_id}/role"): [C("owner", (), lambda i: f"/api/config/employees/{i['admin_user']}/role", ("admin_user",), lambda i: {"role": "admin"}, "POST")],
    ("POST", "/api/config/assignments"): [C("owner", (), lambda i: "/api/config/assignments", ("admin_user",), lambda i: {"user_id": i["admin_user"], "template": "admin_assistant"}, "POST")],
    ("GET", "/api/plans/{pid}"): [C("owner", ("plan",), lambda i: f"/api/plans/{i['plan']}", ("plan",))],
    ("GET", "/api/plans/{pid}/events"): [C("owner", ("plan",), lambda i: f"/api/plans/{i['plan']}/events", ("plan",))],
    ("GET", "/api/plans/{pid}/stream"): [C("owner", ("plan",), lambda i: f"/api/plans/{i['plan']}/stream", ("plan",), stream=True)],
    ("POST", "/api/plans/{pid}/confirm"): [C("owner", ("plan",), lambda i: f"/api/plans/{i['plan']}/confirm", ("plan",), None, "POST")],
    ("POST", "/api/plans/{pid}/cancel"): [C("owner", ("plan",), lambda i: f"/api/plans/{i['plan']}/cancel", ("plan",), None, "POST")],
    ("POST", "/api/plans/{pid}/clarify"): [C("owner", ("clarify",), lambda i: f"/api/plans/{i['clarify_plan']}/clarify", ("clarify_plan", "admin_user"),
                                             lambda i: {"admin_user_id": i["admin_user"]}, "POST")],
    ("GET", "/api/customers/{kid}/access-log"): [C("owner", ("pending",), lambda i: f"/api/customers/{i['customer']}/access-log", ("customer",))],
    ("GET", "/api/customers/{kid}/export"): [C("owner", ("pending",), lambda i: f"/api/customers/{i['customer']}/export", ("customer",))],
    ("POST", "/api/customers/{kid}/anonymize"): [C("owner", ("pending",), lambda i: f"/api/customers/{i['customer']}/anonymize", ("customer",),
                                                   lambda i: {"confirm_name": "no coincide"}, "POST", ok=(400,))],       # el control propio llega al cliente y responde 400 (confirmación): distinto del 404 de un id ajeno/ausente; nada se anonimiza
    ("POST", "/api/tools/{name}/invoke"): [C("owner", ("pending",), lambda i: "/api/tools/requests.get/invoke", ("request",), lambda i: {"args": {"request_id": i["request"]}}, "POST")],
}

# Rutas autenticadas SIN identificador que NO son listas de lectura: la empresa sale de la sesión y no reciben referencias a objetos (cada una justificada).
NO_ID_ROUTES = {
    ("POST", "/api/auth/logout"): "borra la sesión presentada", ("POST", "/api/auth/logout-all"): "revoca las sesiones del usuario de la sesión",
    ("POST", "/api/assistant/chat"): "el texto se interpreta con la empresa de la sesión (las herramientas se prueban en la matriz de herramientas)",
    ("POST", "/api/requests/manual"): "crea filas de la empresa de la sesión; `client_request_id` es una clave de idempotencia del cliente",
    ("POST", "/api/plans"): "ídem: `client_plan_id` es una clave de idempotencia",
    ("POST", "/api/commitments/process-due"): "procesa los compromisos vencidos de la empresa de la sesión (fecha, no identificadores)",
    ("POST", "/api/config/employees"): "alta de un empleado en la empresa de la sesión (correo, nombre, rol)",
    ("POST", "/api/auth/reauth"): "V0.7 P2: confirma la contraseña de la persona de la sesión (no recibe identificadores; actúa sobre la sesión presentada)",
    ("POST", "/api/auth/login"): "anónima: la crea la sesión", ("POST", "/api/demo/reset-scenarios"): "SOLO existe fuera del piloto; borra datos de la empresa de la sesión y exige escribir REINICIAR",
    ("POST", "/api/setup/companies"): "anónima y apagada fuera de local",
}
NON_TENANT_PATH_PARAMS = {"name": "nombre de una herramienta del registro (no es un id de empresa): la matriz de herramientas lo cubre", "slug": "canal público: test_public_*",
                          "persona": "solo /demo-login (anónima; no existe en el piloto)"}


def is_id(name: str) -> bool:
    return name not in IDEMPOTENCY_KEYS and (name in ("id", "token") or name.endswith("_id") or name.endswith("_ids"))


def id_surface(app) -> dict[tuple, dict]:
    """{(MÉTODO, ruta): {'path': [...], 'query': [...], 'body': [...]}} leído del OpenAPI de la aplicación VIVA (no de una lista escrita a mano)."""
    spec, out = app.openapi(), {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            params = op.get("parameters", [])
            sch = ((op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")) or {}
            props = spec["components"]["schemas"][sch["$ref"].split("/")[-1]].get("properties", {}) if "$ref" in sch else sch.get("properties", {})
            out[(method.upper(), path)] = {"path": [p["name"] for p in params if p["in"] == "path"], "query": [p["name"] for p in params if p["in"] == "query" and is_id(p["name"])],
                                           "body": [n for n in props if is_id(n)]}
    return out


def _both_apps(tmp_path_factory, mp):
    """La unión de las rutas de un arranque `local` y uno `pilot`: así una ruta de demostración NUEVA también exige su decisión."""
    apps = []
    mp.setattr(settings, "sync_runs", True)                                                                                  # (sin hilos de fondo en estas apps de solo lectura de rutas)
    mp.setattr(settings, "env", "", raising=False)
    mp.setattr(settings, "cookie_secure", "auto")
    apps.append(create_app(f"sqlite:///{tmp_path_factory.mktemp('loc') / 'l.db'}"))
    pilot_on(mp)
    apps.append(create_app(f"sqlite:///{tmp_path_factory.mktemp('pil') / 'p.db'}"))
    return apps


@pytest.fixture(scope="module")
def surface(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        apps = _both_apps(tmp_path_factory, mp)
    merged = {}
    for a in apps:
        for k, v in id_surface(a).items():
            merged[k] = v
    return merged


def _anonymous():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("audit_identity_tenancy", Path(__file__).resolve().parents[2] / "qa" / "audit_identity_tenancy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.ANONYMOUS_ROUTES)


def test_every_authenticated_route_with_an_identifier_has_a_matrix_case(surface):
    """AC5: si alguien agrega una ruta con un identificador (en la ruta, la consulta o el cuerpo) y no la cubre aquí, ESTA prueba falla."""
    anon = _anonymous()
    need_case = {k for k, s in surface.items() if k not in anon and (set(s["path"]) - set(NON_TENANT_PATH_PARAMS)) | set(s["query"]) | set(s["body"])}
    need_case |= {k for k, s in surface.items() if k not in anon and "name" in s["path"]}                                    # /api/tools/{name}/invoke: ids dentro de `args`
    assert need_case - set(ROUTES) == set(), f"rutas con identificadores SIN caso en la matriz: {sorted(need_case - set(ROUTES))}"
    assert set(ROUTES) <= set(surface), f"casos de rutas que ya no existen: {sorted(set(ROUTES) - set(surface))}"
    assert all(c.swap for cs in ROUTES.values() for c in cs)


def test_every_other_authenticated_route_is_a_list_or_justified(surface):
    anon = _anonymous()
    rest = {k for k, s in surface.items() if k not in anon and k not in ROUTES and not (s["path"] or s["query"] or s["body"])}
    lists = {k for k in rest if k[0] == "GET"}
    assert rest - lists - set(NO_ID_ROUTES) == set(), f"rutas sin identificador que ni son listas de lectura ni están justificadas: {sorted(rest - lists - set(NO_ID_ROUTES))}"
    assert set(NO_ID_ROUTES) <= set(surface) | anon | {("POST", "/api/auth/login")}, "justificaciones de rutas que ya no existen"


CASES = [(k, i, c) for k, cs in ROUTES.items() for i, c in enumerate(cs)]


@pytest.mark.parametrize("key,idx,case", CASES, ids=[f"{k[0]} {k[1]}" for k, _, _ in CASES])
def test_cross_tenant_matrix(world, key, idx, case):
    w, B, A = world, world.B, world.A
    own = w.fresh(B, case.needs, "Beta")
    actor = getattr(B, case.actor)
    before = snapshot(w.app.state.eng, A.company_id)

    def call(ids: dict) -> Out:
        body = case.body(ids) if case.body else None
        path = case.path(ids)
        if case.stream:
            return sse(w, actor, path)
        return http(actor, case.method, path, body)

    first_ghost = None
    for kind in case.swap:
        g_ids = {**own, kind: ghost(own[kind])}
        a_ids = {**own, kind: w.victim[kind]}
        g, a = call(g_ids), call(a_ids)
        first_ghost = first_ghost or g
        sent = case.path(a_ids) + json.dumps(case.body(a_ids) if case.body else "")
        assert a.status >= 400, f"{key} [{kind}]: la llamada con un id de A NO fue rechazada ({a.status}): {a.text[:200]}"
        assert (a.status, norm(a.text, own, w.victim, g_ids)) == (g.status, norm(g.text, own, w.victim, g_ids)), \
            f"{key} [{kind}]: B distingue un id de A de uno inexistente\n  con id de A:        {a.status} {a.text[:200]}\n  con id inexistente: {g.status} {g.text[:200]}"
        assert not leaks(w, a.text, sent), f"{key} [{kind}]: la respuesta contiene datos de A: {leaks(w, a.text, sent)}"
    assert snapshot(w.app.state.eng, A.company_id) == before, f"{key}: los datos de A CAMBIARON por una llamada de B"
    ctrl = call(own)
    assert ctrl.status in case.ok, f"{key}: el CONTROL (B con sus propios ids) falló: {ctrl.status} {ctrl.text[:300]}"
    assert (ctrl.status, norm(ctrl.text, own, w.victim)) != (first_ghost.status, norm(first_ghost.text, own, w.victim)), f"{key}: control y fantasma idénticos → la prueba sería vacía"
    if case.stream:
        assert ctrl.text.startswith("retry:"), ctrl


# ───────────────────────────── 2. listas ─────────────────────────────

def _list_routes(surface):
    anon = _anonymous()
    return sorted(k[1] for k, s in surface.items() if k[0] == "GET" and k not in anon and k not in ROUTES and not (s["path"] or s["query"] or s["body"]))


def test_lists_of_b_never_contain_anything_of_a(world, surface):
    w, found = world, 0
    for path in _list_routes(surface):
        if path in ("/api/health",):
            continue
        for role in ("owner", "admin", "tech"):
            r = getattr(w.B, role).get(path)
            assert r.status_code in (200, 403), (path, role, r.status_code)
            assert not leaks(w, r.text), (path, role, leaks(w, r.text))
            found += r.status_code == 200
    assert found >= 20


def test_the_leak_scanner_can_see_company_a_data_when_it_is_really_there(world):
    """Sin esto, «no hay fugas» podría significar «el escáner no ve nada»: A SÍ ve sus propios datos en sus listas con las mismas marcas."""
    w = world
    seen = set()
    for path in ("/api/requests", "/api/customers", "/api/runs", "/api/team", "/api/dashboard", "/api/plans", "/api/commitments", "/api/attention"):
        seen |= set(leaks(w, w.A.owner.get(path).text))
    assert {"cliente", "id:request", "id:run", "id:commitment", "id:plan", "id:admin_user"} <= seen, seen


# ───────────────────────────── 3. SSE ─────────────────────────────

def test_b_cannot_open_the_streams_of_a_and_its_own_streams_carry_no_event_of_a(world):
    w = world
    for path in (f"/api/runs/{w.victim['run']}/stream", f"/api/plans/{w.victim['plan']}/stream"):
        g = sse(w, w.B.owner, path.rsplit("/", 2)[0] + "/" + ghost(w.victim["run"]) + "/stream")
        a = sse(w, w.B.owner, path)
        assert a.status == 404 == g.status and a.text == g.text and not leaks(w, a.text), (path, a, g)
        assert sse(w, w.A.owner, path).status == 200                                                       # control: su dueña sí lo abre
    # flujo vivo: B mira SU run mientras A genera eventos; después, lo recibido no contiene nada de A
    own = w.fresh(w.B, ("pending",), "Beta")

    async def scenario():
        b_stream = asyncio.create_task(_asgi_get(w.app, f"/api/runs/{own['run']}/stream", w.B.owner.cookie_header, PILOT_HOST, hold=1.2))
        await asyncio.sleep(0.3)
        await asyncio.to_thread(lambda: w.A.owner.post(f"/api/runs/{w.victim['blocked_run']}/resolve", json={"action": "diagnostic_only"}))      # A emite eventos NUEVOS durante el flujo de B
        return await b_stream

    status, body = asyncio.run(scenario())
    assert status == 200 and "run.started" in body and not leaks(w, body), (status, leaks(w, body))
    a_status, a_body = asyncio.run(_asgi_get(w.app, f"/api/runs/{w.victim['blocked_run']}/stream", w.A.owner.cookie_header, PILOT_HOST))
    assert a_status == 200 and "run." in a_body                                                              # control: A sí ve los suyos
    # y a nivel de datos: leer los eventos de A con la empresa de B no devuelve nada, con la de A sí
    assert fetch(w.app.state.eng, w.B.company_id, w.victim["run"], 0) == [] and fetch(w.app.state.eng, w.A.company_id, w.victim["run"], 0) != []


# ───────────────────────────── 4. capa de herramientas (todas, también las de los agentes) ─────────────────────────────

TOOLS = {
    "approvals.request": ("owner", lambda i: {"request_id": i["request"], "subject": "Prueba ficticia", "summary": "Resumen ficticio", "amount": 1.0}, ("request",)),
    "collab.request_material": ("tech", lambda i: {"request_id": i["maint_request"], "sku": SKU, "qty": 1}, ("maint_request",)),
    "commitments.create": ("owner", lambda i: {"request_id": i["request"], "owner_user_id": i["admin_user"], "title": "Seguimiento ficticio", "due_at": "2099-01-01T10:00:00+00:00", "idem_key": "k" + _uniq()},
                           ("request", "admin_user")),
    "commitments.reassign": ("owner", lambda i: {"commitment_id": i["commitment"], "new_owner_user_id": i["admin_user"]}, ("commitment", "admin_user")),
    "commitments.update_status": ("owner", lambda i: {"commitment_id": i["commitment"], "status": "cumplido", "evidence": "Hecho (ficticio)"}, ("commitment",)),
    "customers.history": ("owner", lambda i: {"customer_id": i["customer"]}, ("customer",)),
    "issues.report": ("tech", lambda i: {"request_id": i["maint_request"], "description": "Problema ficticio en la visita"}, ("maint_request",)),
    "materials.request": ("tech", lambda i: {"sku": SKU, "qty": 1, "request_id": i["maint_request"]}, ("maint_request",)),
    "materials.update_status": ("admin", lambda i: {"material_request_id": i["material"], "status": "aprobado"}, ("material",)),
    "messages.prepare_customer": ("owner", lambda i: {"request_id": i["request"], "kind": "ack", "body": "Recibimos tu solicitud (ficticia)."}, ("request",)),
    "notes.read_others": ("owner", lambda i: {"user_id": i["admin_user"]}, ("admin_user",)),
    "plans.activate_step": ("owner", lambda i: {"plan_id": i["plan"], "seq": 1}, ("plan",)),
    "plans.assign_task": ("owner", lambda i: {"plan_id": i["plan"], "seq": 1}, ("plan",)),
    "plans.finish": ("owner", lambda i: {"plan_id": i["plan"]}, ("plan",)),
    "plans.review": ("owner", lambda i: {"request_id": i["request"]}, ("request",)),
    "plans.verify_step": ("owner", lambda i: {"plan_id": i["plan"], "seq": 1}, ("plan",)),
    "quotes.prepare": ("admin", lambda i: {"request_id": i["request"], "service_codes": ["DIAG"]}, ("request",)),
    "quotes.set_status": ("admin", lambda i: {"request_id": i["request"], "status": "aprobado"}, ("request",)),
    "requests.classify": ("owner", lambda i: {"request_id": i["request"]}, ("request",)),
    "requests.create": ("owner", lambda i: {"customer_id": i["customer"], "description": "Solicitud ficticia", "idem_key": "k" + _uniq()}, ("customer",)),
    "requests.get": ("owner", lambda i: {"request_id": i["request"]}, ("request",)),
    "requests.update_status": ("owner", lambda i: {"request_id": i["request"], "status": "clasificada"}, ("request",)),
    "results.record": ("owner", lambda i: {"request_id": i["request"], "kind": "nota", "summary": "Resultado ficticio"}, ("request",)),
    "tasks.cancel": ("admin", lambda i: {"task_id": i["task"], "reason": "Prueba ficticia"}, ("task",)),
    "tasks.complete": ("admin", lambda i: {"task_id": i["task"], "evidence": "Revisé el inventario ficticio: todo completo."}, ("task",)),
    "tasks.create": ("admin", lambda i: {"request_id": i["request"], "title": "Tarea ficticia", "assignee_user_id": i["admin_user"], "commitment_id": i["commitment"], "idem_key": "k" + _uniq()},
                     ("request", "admin_user", "commitment")),
    "visits.propose": ("admin", lambda i: {"request_id": i["request"], "availability_id": i["slot"]}, ("request", "slot")),
    "workorders.acknowledge": ("tech", lambda i: {"request_id": i["maint_request"]}, ("maint_request",)),
    "workorders.annotate": ("admin", lambda i: {"request_id": i["maint_request"], "status": "material_listo", "note": "Nota ficticia"}, ("maint_request",)),
    "workorders.create": ("admin", lambda i: {"request_id": i["request"], "technician_id": i["tech_user"]}, ("request", "tech_user")),
    "workorders.update_status": ("tech", lambda i: {"request_id": i["maint_request"], "status": "en_camino"}, ("maint_request",)),
}
TOOLS_NO_ID = "sin propiedades que referencien objetos: la empresa sale del principal"


def _tool_id_props(t):
    return [p for p in t.Input.model_json_schema().get("properties", {}) if is_id(p)]


def test_every_registered_tool_with_an_identifier_has_a_matrix_case():
    with_ids = {n for n, t in REGISTRY.items() if _tool_id_props(t)}
    assert with_ids - set(TOOLS) == set(), f"herramientas con identificadores SIN caso: {sorted(with_ids - set(TOOLS))}"
    assert set(TOOLS) <= set(REGISTRY), f"casos de herramientas que ya no existen: {sorted(set(TOOLS) - set(REGISTRY))}"


def _principal(w, co, role, perm):
    uid = {"owner": co.ids["owner_id"], "admin": co.ids["admin_id"], "tech": co.ids["tech_id"]}[role]
    urole = {"owner": "owner", "admin": "admin", "tech": "technician"}[role]
    return Principal("user", uid, f"{role}-{co.slug}", urole, co.company_id, {perm}, user_id=uid, user_role=urole)       # SOLO el permiso de esta herramienta: lo que se prueba es la empresa, no los permisos


# Herramientas que solo tienen sentido DENTRO de una ejecución (insertan filas con run_id): se llaman con el run PROPIO de B.
RUN_CONTEXT = {"approvals.request": "run", "collab.request_material": "maint_run", "materials.request": "maint_run"}


def tool_call(w, co, role, name, args, run_id=None) -> Out:
    try:
        out = invoke(w.app.state.eng, _principal(w, co, role, REGISTRY[name].permission), name, args, run_id=run_id, provider=w.app.state.orch.provider)
        return Out(200, json.dumps(out, default=str, ensure_ascii=False))
    except Denied as e:
        return Out(403, str(e))
    except ToolError as e:
        return Out(400, str(e))


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_layer_never_resolves_an_identifier_of_another_company(world, name):
    w = world
    role, args, swap = TOOLS[name]
    own = w.fresh(w.B, ALL_NEEDS, "Beta")
    run_id = own[RUN_CONTEXT[name]] if name in RUN_CONTEXT else None
    before = snapshot(w.app.state.eng, w.A.company_id)
    first_ghost = None
    for kind in swap:
        g_ids, a_ids = {**own, kind: ghost(own[kind])}, {**own, kind: w.victim[kind]}
        g, a = tool_call(w, w.B, role, name, args(g_ids), run_id), tool_call(w, w.B, role, name, args(a_ids), run_id)
        first_ghost = first_ghost or g
        assert (a.status, norm(a.text, own, w.victim, g_ids)) == (g.status, norm(g.text, own, w.victim, g_ids)), \
            f"{name} [{kind}]: distingue un id de A de uno inexistente\n  A:         {a.status} {a.text[:200]}\n  inexistente: {g.status} {g.text[:200]}"
        assert not leaks(w, a.text, json.dumps(args(a_ids))), (name, kind, leaks(w, a.text))
    assert snapshot(w.app.state.eng, w.A.company_id) == before, f"{name}: los datos de A cambiaron"
    ctrl = tool_call(w, w.B, role, name, args(own), run_id)
    assert (ctrl.status, norm(ctrl.text, own, w.victim)) != (first_ghost.status, norm(first_ghost.text, own, w.victim)), \
        f"{name}: el control con ids propios es idéntico al de un id inexistente (prueba vacía): {ctrl.status} {ctrl.text[:200]}"


# ───────────────────────────── 5. canal público (AC6) ─────────────────────────────

def test_public_token_is_scoped_to_its_company(world):
    w, A, B = world, world.A, world.B
    tok = w.victim["token"]
    before = snapshot(w.app.state.eng, A.company_id)
    other = w.anon.get(f"/api/public/{B.slug}/outbox", params={"token": tok})
    ghost_ = w.anon.get(f"/api/public/{B.slug}/outbox", params={"token": ghost(tok)})
    assert other.status_code == 404 == ghost_.status_code and other.text == ghost_.text and not leaks(w, other.text)       # el token de A no abre nada con el slug de B, y no se distingue de uno inexistente
    assert snapshot(w.app.state.eng, A.company_id) == before                                                                 # (antes del control: la petición legítima de A gasta SUS propios cubos de límite)
    own = w.anon.get(f"/api/public/{A.slug}/outbox", params={"token": tok})
    assert own.status_code == 200 and own.json()["messages"], own.text                                                       # control: con el slug de A sí


def test_a_visit_code_and_request_of_a_do_nothing_through_the_slug_of_b(world):
    w, A, B = world, world.A, world.B
    before = snapshot(w.app.state.eng, A.company_id)
    body = {"request_id": w.victim["request"], "code": w.victim["visit_code"]}
    via_b = w.anon.post(f"/api/public/{B.slug}/visits/confirm", json=body)
    ghost_ = w.anon.post(f"/api/public/{B.slug}/visits/confirm", json={**body, "request_id": ghost(body["request_id"])})
    assert via_b.status_code == 400 == ghost_.status_code and via_b.text == ghost_.text and not leaks(w, via_b.text, json.dumps(body))
    assert snapshot(w.app.state.eng, A.company_id) == before                                                                 # la visita de A NO se confirmó
    assert w.anon.post(f"/api/public/{A.slug}/visits/confirm", json=body).json()["status"] == "confirmada"                   # control: por su propio slug, sí


def test_the_same_client_message_id_in_two_companies_never_returns_the_other_companys_conversation(world):
    """La clave de idempotencia del chat es POR EMPRESA: repetir en B el `client_msg_id` de A abre una conversación NUEVA de B (token distinto, ningún dato de A)."""
    w, A, B = world, world.A, world.B
    mid = f"compartido-{_uniq()}"
    ra = w.anon.post(f"/api/public/{A.slug}/chat", json={"message": GAS_MSG, "name": "CanaryAlfa Repetido", "client_msg_id": mid}).json()
    rb = w.anon.post(f"/api/public/{B.slug}/chat", json={"message": GAS_MSG, "name": "CanaryBeta Repetido", "client_msg_id": mid}).json()
    assert rb["deduplicated"] is False and rb["conversation_token"] != ra["conversation_token"] and rb["run_id"] != ra["run_id"]
    assert not leaks(w, json.dumps(rb))
    box_b = w.anon.get(f"/api/public/{B.slug}/outbox", params={"token": rb["conversation_token"]}).text
    assert "CanaryAlfa" not in box_b


def test_the_public_routes_with_identifiers_are_exactly_the_ones_covered_here(surface):
    public = {k: s for k, s in surface.items() if k[1].startswith("/api/public/")}
    covered = {("POST", "/api/public/{slug}/chat"), ("GET", "/api/public/{slug}/outbox"), ("POST", "/api/public/{slug}/visits/confirm")}
    assert set(public) == covered, f"canal público con rutas nuevas sin caso AC6: {sorted(set(public) ^ covered)}"
