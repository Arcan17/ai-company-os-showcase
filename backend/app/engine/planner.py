"""V0.4 conversational coordinator: owner instruction → structured, validated, human-confirmed WorkOrderPlan → delegation → verification.

Lifecycle (plans.status):
  draft → needs_clarification | awaiting_user_confirmation | blocked
  awaiting_user_confirmation --confirm(owner)--> running ⇄ waiting_human → completed | blocked | failed ;  cancel → cancelled
Nothing is created (tasks, delegations) before a human confirms. Steps are persisted BEFORE execution so the owner reviews what will happen.
Every effect goes through the tool registry with the COORDINATOR agent principal (not the owner's permissions); all effects are
idempotent (unique keys / compare-and-set), so repeated clicks, concurrent threads and a server restart never duplicate work.
A provider (LLM) may only PROPOSE closed-vocabulary flags; this module re-validates them against the text and the company's data.
Plan events live in the same `events` table, keyed by plan id (prefix `pln_`), so the SSE visualizer is reused unchanged.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import traceback
import unicodedata

from ..config import settings
from ..db import all_, jdump, jload, now, one, q, uid
from ..events import audit, emit
from ..lease import LeaseLost
from ..providers.base import PlanProposal, minimal_requests
from ..tools import Denied, ToolError, invoke

log = logging.getLogger("aicos.planner")

MAX_STEPS = 6
MAX_ROUNDS = 2
MAX_ACTIVE_PLANS = 25
TERMINAL = ("completed", "blocked", "failed", "cancelled")
ACTIVE = ("needs_clarification", "awaiting_user_confirmation", "running", "waiting_human")
TASK_ROLE = {"ask_admin_verify_materials": "admin", "ask_technician_confirm": "technician"}
ACTIONS = {  # the CLOSED catalog: action → (agent, access, permission)
    "review_pending": ("coordinator", "read", "plans.read"),
    "review_request": ("coordinator", "read", "plans.read"),
    "ask_admin_verify_materials": ("admin_assistant", "write", "plans.assign"),
    "ask_technician_confirm": ("tech_assistant", "write", "plans.assign"),
    "report_to_owner": ("coordinator", "write", "plans.finish"),
}
ACTION_LABEL = {"review_pending": "Revisar solicitudes abiertas", "review_request": "Revisar la solicitud y su stock real",
                "ask_admin_verify_materials": "Pedir a administración que verifique materiales", "ask_technician_confirm": "Pedir confirmación al técnico",
                "report_to_owner": "Verificar el registro y avisarte"}


# Por qué un plan salió «por reglas» aunque había un modelo configurado (se muestra en la interfaz y queda en el evento del plan).
FALLBACK_LABEL = {"presupuesto": "se alcanzó el tope de gasto de la IA", "error": "la IA no respondió o falló", "lentitud": "la IA tardó demasiado", "invalida": "la IA devolvió una propuesta inválida",
                  "sin_tareas": "la propuesta de la IA no tenía tareas soportadas"}


def _well_formed(p) -> bool:
    return (isinstance(p, PlanProposal) and all(isinstance(getattr(p, k), bool) for k in ("review_pending", "admin_verify_materials", "technician_confirm", "notify_owner"))
            and (p.request_id is None or isinstance(p.request_id, str)) and isinstance(p.unsupported, list) and all(isinstance(u, str) for u in p.unsupported)
            and isinstance(p.source, str))


def norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


# ---- what the coordinator will NOT do (scanned on the raw text; a provider can add labels but never remove them) ----
def _before_antes_de(t: str, m: re.Match) -> bool:
    return t[max(0, m.start() - 9):m.start()].endswith("antes de ")


FORBIDDEN: list[tuple[str, str, str]] = [
    ("comprar", r"\bcompr(a|as|ar|e|es|en|amos)\b|orden(es)? de compra|pedir? (a|al) proveedor", "Comprar o pedir repuestos a proveedores"),
    ("pagar", r"\b(pag(a|ar|o|os|ue|uen)|cobr(a|ar|o|os)|transfi(ere|erir)|transferencias?|reembols\w*|factur(a|as|ar))\b", "Pagos, cobros o transferencias"),
    ("enviar_mensaje_externo", r"whats\s?app|wasap|\bcorreo\b|e-?mail|\bsms\b|\bllam(a|ar|e|ale)\b|avis(a|ar|ale|ales|e)\s+(a|al)\s+(los\s+|las\s+|el\s+|la\s+)?cliente|mensaje (a|al) cliente|env[ií]a(r|le|les)?\b|\bmanda(r|le|les)?\b",
     "Enviar mensajes o llamadas a clientes u otras personas fuera de la empresa"),
    ("agendar_o_reservar", r"\b(agend(a|ar|e)|reserv(a|ar|e)|program(a|ar))\b", "Agendar o reservar visitas"),
    ("aprobar", r"\bapru[eé]b\w*|\baprobar\b|\bautoriz\w*", "Aprobar presupuestos o gastos (solo una persona decide en Aprobaciones)"),
    ("modificar_stock", r"descuent\w*|ajust(a|ar|e) (el )?(stock|inventario)|rebaj\w*", "Modificar stock o precios"),
    ("eliminar_datos", r"\b(borr(a|ar|e)|elimin(a|ar|e)|cancel(a|ar|e) (la |el )?(visita|solicitud|orden))\b", "Eliminar o cancelar datos"),
]
INJECTION = re.compile(r"[\"']?(review_pending|admin_verify_materials|technician_confirm|unsupported_actions|request_id)[\"']?\s*:|responde\s+(exactamente|solo)\s*[\{\[]|ignor(a|ar|e)\s+(las\s+|todas\s+|tus\s+)?(reglas|instrucciones|restricciones)|olvida\s+(tus|las)\s+(reglas|instrucciones)|sin\s+pedir\s+confirmaci|system prompt|modo\s+(admin|desarrollador|dios)|eres\s+ahora|act[uú]a\s+como\s+(el\s+)?(due[nñ]o|administrador del sistema)")
NAMED_TARGET = re.compile(r"\b(?:[Pp]ide|[Pp][ií]dele|[Aa]signa|[Ee]ncarga|[Ss]olicita|[Dd]ile)\s+(?:a|al)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})")
GENERIC_TARGET = {"administracion", "tecnico", "tecnica", "admin", "contabilidad", "bodega"}
MATERIAL_RE = re.compile(r"material|repuesto|insumo|stock|herramient|cilindro|\bgas\b|r410|inventario")
ADMIN_RE = re.compile(r"administraci|\badmin\b|verif|confirm|revis|chequ|compru|asegur|consult|valid")
TECH_RE = re.compile(r"t[eé]cnic")
TECH_CONFIRM_RE = re.compile(r"confirm|respond|avis|valid|visto bueno|\bok\b|asegur|compru|verif")
REVIEW_RE = re.compile(r"revis|list|muestr|consult|que hay|cuales|ver |pendiente")
REVIEW_OBJ_RE = re.compile(r"pendiente|solicitud|trabajo|mantenci|visita")
NOTIFY_RE = re.compile(r"avis(a|e)me|informame|dime|reporta(me)?|hazme saber|resum")


def scan_forbidden(text: str) -> list[dict]:
    t = norm(text)
    out = []
    for code, rx, label in FORBIDDEN:
        for m in re.finditer(rx, t):
            if code == "agendar_o_reservar" and _before_antes_de(t, m):
                continue  # «avísame si falta algo ANTES DE agendar» is a precondition, not a request to schedule
            out.append({"code": code, "action": label})
            break
    return out


def _first_names(staff: list[dict]) -> dict[str, list[dict]]:
    """first name → ALL employees sharing it (two «Javier» must never collapse into one)."""
    d: dict[str, list[dict]] = {}
    for u in staff:
        d.setdefault(norm(u["name"]).split()[0], []).append(u)
    return d


def _named(t: str, staff: list[dict]) -> list[dict]:
    return [u for fn, us in _first_names(staff).items() if re.search(r"\b" + re.escape(fn) + r"\b", t) for u in us]


def _customers_in_text(text: str, customers: list[dict]) -> list[dict]:
    t = norm(text)
    hits, stop = [], {"oficina", "spa", "calle", "empresa"}
    for c in customers:
        n = norm(c["name"])
        toks = [x for x in n.split() if len(x) >= 4 and x not in stop]
        if re.search(r"\b" + re.escape(n) + r"\b", t) or any(re.search(r"\b" + re.escape(x) + r"\b", t) for x in toks[:1]):
            hits.append(c)
    return hits


def _named_roles(t: str, staff: list[dict]) -> set[str]:
    return {u["role"] for u in _named(t, staff)}


def wants_admin(t: str, staff: list[dict]) -> bool:
    return bool(MATERIAL_RE.search(t) and ("admin" in _named_roles(t, staff) or ADMIN_RE.search(t)))


def wants_technician(t: str, staff: list[dict]) -> bool:
    return bool((TECH_RE.search(t) or "technician" in _named_roles(t, staff)) and TECH_CONFIRM_RE.search(t))


def wants_review(t: str) -> bool:
    return bool(REVIEW_RE.search(t) and REVIEW_OBJ_RE.search(t))


def deterministic_proposal(text: str, staff: list[dict]) -> PlanProposal:
    t = norm(text)
    admin, tech, review = wants_admin(t, staff), wants_technician(t, staff), wants_review(t)
    rid = re.search(r"req_[0-9a-f]{12}", t)
    return PlanProposal(review_pending=review, admin_verify_materials=admin, technician_confirm=tech, notify_owner=bool(NOTIFY_RE.search(t)),
                        request_id=rid.group(0) if rid else None, source="deterministic")


def validate_llm_proposal(p: PlanProposal, text: str, open_reqs: list[dict], customers: list[dict], staff: list[dict]) -> tuple[PlanProposal, list[str]]:
    """Every flag a provider sets must be supported by the owner's own words and the company's data. Unsupported flags are dropped (and reported)."""
    t = norm(text)
    rejected, ids = [], {r["id"] for r in open_reqs}
    if p.request_id and p.request_id not in ids:
        rejected.append(f"request_id inexistente ({p.request_id[:20]})")
        p.request_id = None
    if p.request_id and p.request_id not in t:
        # A model must never settle an ambiguity on its own: with several open requests it would silently pick one. The request is fixed only by a literal
        # id in the owner's text or by the engine's own deterministic resolution (which asks when there is more than one candidate).
        rejected.append("request_id propuesto por el LLM sin referencia literal en el texto: se ignora (la solicitud la resuelven los datos, no el modelo)")
        p.request_id = None
    if p.admin_verify_materials and not wants_admin(t, staff):
        rejected.append("admin_verify_materials no respaldado por el texto del dueño")
        p.admin_verify_materials = False
    if p.technician_confirm and not wants_technician(t, staff):
        rejected.append("technician_confirm no respaldado por el texto del dueño")
        p.technician_confirm = False
    if p.review_pending and not wants_review(t):
        rejected.append("review_pending sin pedido de revisión")
        p.review_pending = False
    return p, rejected


class PlanEngine:
    def __init__(self, orch):
        self.o, self.eng = orch, orch.eng
        self._locks: dict[str, threading.Lock] = {}
        self._glock = threading.Lock()
        self._tl = threading.local()   # the lease epoch the CURRENT advance thread started in

    # ------------------------------------------------------------------ helpers
    def _lock(self, plan_id: str) -> threading.Lock:
        with self._glock:
            return self._locks.setdefault(plan_id, threading.Lock())

    def _check(self):
        self.o.check(getattr(self._tl, "epoch", self.o.epoch))   # stops a thread superseded by another instance (LeaseLost) before its next step

    def _pace(self):
        self._check()
        if not settings.sync_runs:
            time.sleep(settings.step_delay)

    def _ev(self, company_id, plan_id, type_, title, data=None, *, agent="coordinator", mode="real"):
        self._check()
        emit(self.eng, company_id, plan_id, type_, agent=agent, mode=mode, title=title, data=data or {})

    def _call(self, company_id, plan_id, tool, args):
        self._check()
        return invoke(self.eng, self.o.principal(company_id, "coordinator"), tool, args, run_id=plan_id, provider=self.o.provider, pace=self._pace)

    def _plan(self, company_id, plan_id):
        with self.eng.connect() as c:
            return one(c, "SELECT * FROM plans WHERE id=:i AND company_id=:c", i=plan_id, c=company_id)

    def _launch(self, plan_id: str) -> None:
        if not self.o.active:   # backstop (the HTTP gate refuses writes on a standby): do not start an unsupervised reconciler; the instance that holds the lease re-runs it on gain
            log.warning("plan %s no se lanza: esta instancia no tiene el lease", plan_id)
            return
        epoch = self.o.epoch
        if settings.sync_runs:
            self.advance(plan_id, epoch)
        else:
            threading.Thread(target=self.advance, args=(plan_id, epoch), daemon=True).start()

    # ------------------------------------------------------------------ creation / planning
    def create(self, user: dict, text: str, client_plan_id: str) -> dict:
        cid = user["company_id"]
        text = " ".join(text.split())
        key = f"plan:{client_plan_id}"
        with self.eng.begin() as c:
            ex = one(c, "SELECT id,status FROM plans WHERE company_id=:c AND idem_key=:k", c=cid, k=key)
            if ex:
                return {"plan_id": ex["id"], "deduplicated": True}
            same = next((r for r in all_(c, f"SELECT id,text FROM plans WHERE company_id=:c AND created_by=:u AND status IN ({','.join(repr(s) for s in ACTIVE)})", c=cid, u=user["id"])
                         if norm(r["text"]) == norm(text)), None)  # Python-side: SQLite lower() ignores accents/capital Ó
            if same:  # the same instruction, still open → no second plan (double submit with a new client id)
                return {"plan_id": same["id"], "deduplicated": True}
            if one(c, f"SELECT COUNT(*) AS n FROM plans WHERE company_id=:c AND status IN ({','.join(repr(s) for s in ACTIVE)})", c=cid)["n"] >= MAX_ACTIVE_PLANS:
                raise ToolError("hay demasiados planes activos; cierra o cancela alguno antes de crear otro")
            pid = uid("pln_")
            q(c, "INSERT INTO plans(id,company_id,created_by,text,status,rounds,idem_key,created_at,updated_at) VALUES(:i,:c,:u,:t,'draft',0,:k,:n,:n)", i=pid, c=cid, u=user["id"], t=text, k=key, n=now())
            audit(c, cid, "user", user["id"], "plans.create", pid, "ok", "instrucción recibida")
        self._ev(cid, pid, "plan.received", f"Instrucción del dueño ({user['name']}): «{text[:120]}»", {"text": text, "from": user["name"]}, agent="owner_assistant")
        self._plan_it(user, pid, {})
        return {"plan_id": pid, "deduplicated": False}

    def _plan_it(self, user: dict, pid: str, overrides: dict) -> None:
        cid = user["company_id"]
        plan = self._plan(cid, pid)
        text = plan["text"]
        self._ev(cid, pid, "agent.activated", "Coordinador: analiza la instrucción con datos reales", {"why": "planificar"})
        ctx = self._call(cid, pid, "plans.lookup", {})
        everyone = ctx["staff"] + ctx.get("unassigned", [])   # names the owner may use: eligible employees AND those without an active assistant (to explain why not)
        prov = self.o.provider
        prop, notes, source = None, [], "deterministic"
        fallback = None
        if prov.mode == "llm":
            raw, fallback, detail = self._ask_provider(prov, text, ctx["open_requests"])
            if raw is not None:
                self._ev(cid, pid, "plan.proposed", f"Propuesta del LLM ({raw.source}) — aún NO validada", {"proposal": raw.__dict__}, mode="llm")
                try:
                    prop, rej = validate_llm_proposal(raw, text, ctx["open_requests"], ctx["customers"], everyone)
                except Exception as e:                                    # una propuesta que rompe al validador es una propuesta inválida, no un error del servidor
                    prop, rej, fallback, detail = None, [], "invalida", f"la propuesta no se pudo validar ({type(e).__name__})"
                if rej:
                    self._ev(cid, pid, "plan.llm_rejected", f"Validación del motor descartó {len(rej)} elemento(s) de la propuesta del LLM", {"rejected": rej}, mode="deterministic")
                if prop is not None and not (prop.review_pending or prop.admin_verify_materials or prop.technician_confirm):
                    fallback, detail, prop = "sin_tareas", "tras validar, la propuesta del LLM no contenía tareas soportadas", None
                elif prop is not None:
                    source = raw.source  # its "unsupported" labels are advisory only: the deterministic scan in _resolve is authoritative
            if fallback:
                notes.append(f"{FALLBACK_LABEL[fallback]} ({detail}): se usan las reglas deterministas")
                self._ev(cid, pid, "plan.llm_rejected", "La propuesta del LLM no fue utilizable → reglas deterministas", {"reason": notes[-1], "code": fallback}, mode="llm")
        if prop is None:
            prop = deterministic_proposal(text, everyone)
        draft = self._resolve(prop, text, ctx, overrides)
        draft["notes"] = notes + draft.get("notes", [])
        draft["fallback"] = fallback
        self._persist(cid, pid, draft, source, overrides)
        self._ev(cid, pid, "agent.completed", "Coordinador: plan " + {"awaiting_user_confirmation": "listo para tu confirmación", "needs_clarification": "necesita una aclaración", "blocked": "BLOQUEADO (no se ejecuta nada)"}[draft["status"]], {"status": draft["status"]})
        if draft["status"] == "awaiting_user_confirmation" and draft["intent"] == "review_pending":
            self._auto_read_only(cid, pid)

    def _ask_provider(self, prov, text: str, open_requests: list[dict]):
        """Pide la propuesta a un modelo con un PLAZO DURO y sin dejar escapar ninguna excepción: devuelve (propuesta|None, código de respaldo|None, detalle).
        Un modelo caído, lento o que devuelve basura jamás rompe la creación del plan: se usan las reglas y se dice por qué."""
        out: dict = {}

        def run():
            try:
                out["v"] = prov.propose_plan(text, minimal_requests(open_requests))
            except Exception as e:                                        # noqa: BLE001
                out["e"] = f"{type(e).__name__}: {e}"[:200]
        th = threading.Thread(target=run, daemon=True, name="llm-plan")
        th.start()
        th.join(settings.llm_plan_timeout)
        if th.is_alive():
            return None, "lentitud", f"sin respuesta en {settings.llm_plan_timeout:g} s"
        if "e" in out:
            return None, "error", out["e"]
        raw = out.get("v")
        if raw is None:
            code = "presupuesto" if getattr(prov, "last_code", None) == "presupuesto" else "error"
            return None, code, str(getattr(prov, "last_error", None) or "sin respuesta")[:200]
        if not _well_formed(raw):
            return None, "invalida", "la respuesta no tiene la forma esperada"
        return raw, None, ""

    # ---- entity resolution + step construction (pure, from real data) ----
    def _resolve(self, prop: PlanProposal, text: str, ctx: dict, ov: dict) -> dict:
        excluded = scan_forbidden(text)
        draft = {"steps": [], "excluded": excluded, "clarification": None, "status": "awaiting_user_confirmation", "reason": None, "notes": [], "intent": None, "request_id": None}
        t = norm(text)
        if INJECTION.search(t):
            draft.update(status="blocked", reason="La instrucción intenta saltarse reglas o confirmaciones. El coordinador no las puede cambiar: no se crea ningún plan.")
            draft["excluded"].append({"code": "injection", "action": "Saltarse reglas, permisos o confirmaciones"})
            return draft
        tasks_wanted = prop.admin_verify_materials or prop.technician_confirm
        if not tasks_wanted and (not prop.review_pending or excluded):  # a read-only review + a forbidden action = the owner really asked for the forbidden action
            why = "Pides acciones que el coordinador no ejecuta: " + "; ".join(e["action"] for e in excluded) + ". Requieren una aprobación humana específica o un canal real que esta versión no tiene (no se hace nada)." if excluded \
                else "No reconozco una tarea segura en tu instrucción."
            draft.update(status="blocked", reason=why, clarification={"question": "¿Quieres pedir algo de este catálogo?", "options": [], "suggestions": [
                "Revisa las solicitudes pendientes", "Pide a administración que verifique los materiales de la visita de <cliente> y avísame si falta algo",
                "Pide a administración que verifique materiales para la visita de <cliente>; quiero confirmación del técnico"]})
            return draft
        staff, reqs, custs = ctx["staff"], ctx["open_requests"], ctx["customers"]
        everyone = staff + ctx.get("unassigned", [])
        # «Pide a Tomás…» where Tomás is nobody on the team: never silently hand the work to someone else
        mt = NAMED_TARGET.search(text)
        if mt:
            fn = norm(mt.group(1))
            if fn not in _first_names(everyone) and fn not in GENERIC_TARGET:
                role = "admin" if prop.admin_verify_materials else "technician"
                field = "admin_user_id" if role == "admin" else "technician_user_id"
                if not ov.get(field):
                    pool = [u for u in staff if u["role"] == role]
                    if not pool:
                        draft.update(status="blocked", reason="La empresa no tiene personal elegible con asistente activo para ese trabajo. Configúralo en «Empresa y equipo».")
                        return draft
                    draft.update(status="needs_clarification", reason=f"No encuentro a «{mt.group(1)}» entre administración ni los técnicos de tu empresa: no asigno el trabajo a otra persona sin que lo decidas.")
                    draft["clarification"] = {"question": "¿A quién se lo asigno?", "field": field, "options": [{"id": u["id"], "label": f"{u['name']} · {u.get('workload', 0)} tarea(s) abierta(s)"} for u in pool]}
                    return draft
        if "mañana" in text.lower() or "hoy" in text.lower():
            draft["notes"].append("La fecha («hoy»/«mañana») no se usa como filtro en esta versión: se revisan todas las solicitudes abiertas.")
        if not tasks_wanted:  # read-only plan
            draft["intent"] = "review_pending"
            draft["steps"] = [self._step(1, "review_pending", None, {}, None, "Revisar las solicitudes abiertas de la empresa"),
                              self._step(2, "report_to_owner", None, {}, 1, "Registrar el resultado y avisarte")]
            draft["summary"] = "Solo lectura: listaré las solicitudes abiertas con su visita y técnico."
            return draft
        # --- request ---
        req = None
        if ov.get("request_id"):
            req = next((r for r in reqs if r["id"] == ov["request_id"]), None)
        if req is None:
            m = re.search(r"req_[0-9a-f]{12}", text)
            rid = m.group(0) if m else None  # never prop.request_id: see validate_llm_proposal
            if rid and not any(r["id"] == rid for r in reqs):
                draft.update(status="needs_clarification", reason=f"La solicitud {rid[:24]} no existe o no está abierta en tu empresa.")
                draft["clarification"] = {"question": "¿A cuál solicitud abierta te refieres?", "field": "request_id", "options": [{"id": r["id"], "label": f"{r['customer']} · {r['status']} · {r['description'][:50]}"} for r in reqs[:8]]}
                return draft
            if rid:
                req = next(r for r in reqs if r["id"] == rid)
        if req is None:
            hits = _customers_in_text(text, custs)
            if ov.get("customer_id"):
                hits = [c for c in custs if c["id"] == ov["customer_id"]]
            if len(hits) > 1:
                draft.update(status="needs_clarification", reason="Hay más de un cliente que coincide con lo que escribiste.")
                draft["clarification"] = {"question": "¿De qué cliente hablas?", "field": "customer_id", "options": [{"id": c["id"], "label": c["name"]} for c in hits[:6]]}
                return draft
            cands = [r for r in reqs if (not hits or r["customer_id"] == hits[0]["id"])]
            if not hits and re.search(r"mantenci|mantencion", t):
                cands = [r for r in cands if r["category"] == "mantencion"] or cands
            if not cands:
                draft.update(status="needs_clarification", reason="No encuentro una solicitud abierta" + (f" de {hits[0]['name']}" if hits else "") + ". No invento una: indica otra o crea primero la solicitud.")
                draft["clarification"] = {"question": "¿Sobre qué solicitud abierta?", "field": "request_id", "options": []}
                return draft
            if len(cands) > 1:
                draft.update(status="needs_clarification", reason="Hay varias solicitudes abiertas posibles.")
                draft["clarification"] = {"question": "¿A cuál solicitud te refieres?", "field": "request_id", "options": [{"id": r["id"], "label": f"{r['customer']} · {r['status']} · {r['description'][:50]}"} for r in cands[:8]]}
                return draft
            req = cands[0]
        draft["request_id"] = req["id"]
        # --- people (validated against the company's real configuration, never a name typed by a model) ---
        named = _named(t, everyone)
        def label(u): return f"{u['name']} · {u.get('workload', 0)} tarea(s) abierta(s)"
        def options(pool): return [{"id": u["id"], "label": label(u)} for u in pool]
        def pick(role: str, field: str, what: str, preferred: str | None = None):
            """Choose the responsible among ELIGIBLE employees (active + personal assistant enabled). Never «the first»: several candidates → ask the owner."""
            pool = [u for u in staff if u["role"] == role]
            if ov.get(field):
                u = next((x for x in pool if x["id"] == ov[field]), None)
                if u:
                    return u, None
            if not pool:
                return None, {"status": "blocked", "reason": f"La empresa no tiene personal de {what} con un asistente personal activo, así que no puedo delegar. Configúralo en «Empresa y equipo» (alta del empleado + asignar su asistente).", "clarification": None}
            if role == "admin" and prop.admin_verify_materials and not [x for x in named if x["role"] == "admin"] and [x for x in named if x["role"] != "admin"]:
                other = [x for x in named if x["role"] != "admin"][0]
                return None, {"status": "needs_clarification", "reason": f"{other['name']} no es de administración: la verificación de materiales se asigna a administración.",
                              "clarification": {"question": f"¿A quién de {what} se lo asigno?", "field": field, "options": options(pool)}}
            mine = [x for x in named if x["role"] == role]
            if mine:
                ok = [x for x in mine if any(p["id"] == x["id"] for p in pool)]
                if len(ok) == 1:
                    return ok[0], None
                if len(ok) > 1:   # two people share the first name the owner used
                    return None, {"status": "needs_clarification", "reason": f"Hay {len(ok)} personas de {what} con ese nombre.", "clarification": {"question": "¿A cuál de ellas?", "field": field, "options": options(ok)}}
                why = mine[0].get("reason", "no puede recibir trabajo")
                return None, {"status": "needs_clarification", "reason": f"{mine[0]['name']} {why}: no se le puede delegar.", "clarification": {"question": f"¿A quién de {what} se lo asigno?", "field": field, "options": options(pool)}}
            if preferred:
                u = next((x for x in pool if x["id"] == preferred), None)
                if u:
                    return u, None
            if len(pool) == 1:
                return pool[0], None
            return None, {"status": "needs_clarification", "reason": f"Hay {len(pool)} personas elegibles en {what}: no elijo por ti.",
                          "clarification": {"question": f"¿A quién de {what} se lo asigno?", "field": field, "options": options(pool)}}
        admin_u = tech_u = None
        if prop.admin_verify_materials:
            admin_u, err = pick("admin", "admin_user_id", "administración")
            if err:
                draft.update(err); return draft
        if prop.technician_confirm:
            tech_u, err = pick("technician", "technician_user_id", "técnicos", preferred=req.get("technician_id"))
            if err:
                draft.update(err); return draft
        cust = req["customer"]
        steps = [self._step(1, "review_request", None, {"request_id": req["id"]}, None, f"Revisar la solicitud de {cust} ({req['id']}) y su stock real")]
        if admin_u:
            steps.append(self._step(len(steps) + 1, "ask_admin_verify_materials", admin_u, {"request_id": req["id"], "assignee_role": "admin"}, 1,
                                    f"Verificar los materiales de la visita de {cust} ({req['id']}): consulta el stock y registra evidencia"))
        if tech_u:
            dep = len(steps) if admin_u else 1
            steps.append(self._step(len(steps) + 1, "ask_technician_confirm", tech_u, {"request_id": req["id"], "assignee_role": "technician"}, dep,
                                    f"Confirmar con tu evidencia que la visita de {cust} ({req['id']}) puede realizarse con lo verificado"))
        steps.append(self._step(len(steps) + 1, "report_to_owner", None, {"request_id": req["id"]}, len(steps), "Verificar en el registro que todo esté cerrado con evidencia y avisarte (incluye faltantes)"))
        if len(steps) > MAX_STEPS:
            draft.update(status="blocked", reason="El plan supera el máximo de pasos permitido."); return draft
        draft["steps"] = steps
        draft["intent"] = "verify_materials" + ("+technician_confirm" if tech_u else "")
        if excluded:
            draft["notes"].append("Quedan FUERA del plan (no se ejecutan): " + "; ".join(e["action"] for e in excluded))
        draft["summary"] = f"Visita de {cust} ({req['id']}): " + " → ".join(ACTION_LABEL[s["action"]] for s in steps)
        return draft

    def _step(self, seq, action, user, args, dep, title):
        agent, access, perm = ACTIONS[action]
        return {"seq": seq, "action": action, "agent": agent, "assignee_user_id": user["id"] if user else None, "assignee_name": user["name"] if user else None,
                "args": args, "depends_on": dep, "access": access, "permission": perm, "title": title}

    def _persist(self, cid: str, pid: str, d: dict, source: str, overrides: dict) -> None:
        t = now()
        with self.eng.begin() as c:
            q(c, "DELETE FROM plan_steps WHERE plan_id=:p AND company_id=:c AND status='planned'", p=pid, c=cid)  # only never-executed steps are rebuilt
            for s in d["steps"]:
                q(c, "INSERT INTO plan_steps(id,company_id,plan_id,seq,action,agent,assignee_user_id,args,depends_on,access,permission,origin,title,status,created_at,updated_at) VALUES(:i,:c,:p,:sq,:a,:ag,:u,:ar,:d,:ac,:pe,:o,:ti,'planned',:t,:t)",
                  i=uid("stp_"), c=cid, p=pid, sq=s["seq"], a=s["action"], ag=s["agent"], u=s["assignee_user_id"], ar=jdump(s["args"]), d=s["depends_on"], ac=s["access"], pe=s["permission"],
                  o=source if source.startswith("llm") else "deterministic", ti=s["title"], t=t)
            q(c, "UPDATE plans SET status=:s,source=:so,intent=:in,request_id=:r,summary=:su,proposal=:pr,excluded=:ex,clarification=:cl,status_reason=:re,updated_at=:t WHERE id=:i AND company_id=:c",
              s=d["status"], so=source, **{"in": d["intent"]}, r=d["request_id"], su=d.get("summary"), pr=jdump({"overrides": overrides, "notes": d["notes"], "fallback": d.get("fallback")}), ex=jdump(d["excluded"]),
              cl=jdump(d["clarification"]) if d["clarification"] else None, re=d["reason"], t=t, i=pid, c=cid)
        ttl = {"awaiting_user_confirmation": "Plan validado (aún no ejecuta nada): espera tu confirmación", "needs_clarification": "Plan en pausa: " + (d["reason"] or ""), "blocked": "Plan bloqueado: " + (d["reason"] or "")}[d["status"]]
        self._ev(cid, pid, {"awaiting_user_confirmation": "plan.validated", "needs_clarification": "plan.needs_clarification", "blocked": "plan.blocked"}[d["status"]], ttl,
                 {"steps": [{k: s[k] for k in ("seq", "action", "agent", "access", "depends_on", "title")} | {"assignee": s["assignee_name"]} for s in d["steps"]], "excluded": d["excluded"],
                  "clarification": d["clarification"], "notes": d["notes"], "source": source}, mode="deterministic")

    def _auto_read_only(self, cid: str, pid: str) -> None:
        """Read-only plans change nothing, so they run without a confirmation click (reads are allowed by permission)."""
        with self.eng.begin() as c:
            n = q(c, "UPDATE plans SET status='running',confirmed_by='auto:solo-lectura',confirmed_at=:t,updated_at=:t WHERE id=:i AND company_id=:c AND status='awaiting_user_confirmation'", t=now(), i=pid, c=cid).rowcount
        if n == 1:
            self._ev(cid, pid, "plan.confirmed", "Plan de solo lectura: se ejecuta sin cambios en datos", {"by": "auto:solo-lectura"}, agent="coordinator")
            self._launch(pid)

    # ------------------------------------------------------------------ human decisions
    def clarify(self, user: dict, pid: str, choice: dict) -> dict:
        cid = user["company_id"]
        plan = self._plan(cid, pid)
        if not plan or plan["created_by"] != user["id"]:
            raise ToolError("plan no encontrado")
        if plan["status"] != "needs_clarification":
            raise ToolError(f"el plan está {plan['status']}: no admite aclaraciones")
        if plan["rounds"] >= MAX_ROUNDS:
            with self.eng.begin() as c:
                q(c, "UPDATE plans SET status='blocked',status_reason='Máximo de aclaraciones alcanzado; crea una instrucción nueva y más específica.',updated_at=:t WHERE id=:i AND company_id=:c AND status='needs_clarification'", t=now(), i=pid, c=cid)
            self._ev(cid, pid, "plan.blocked", "Máximo de rondas de aclaración alcanzado", {})
            raise ToolError("máximo de aclaraciones alcanzado: crea una instrucción nueva")
        cl = jload(plan["clarification"], {})
        field, opts = cl.get("field"), {o["id"] for o in cl.get("options", [])}
        val = choice.get(field) if field else None
        if not field or val not in opts:
            raise ToolError("la opción elegida no está entre las que propuso el coordinador")  # no free ids from the client either
        ov = jload(plan["proposal"], {}).get("overrides", {}) | {field: val}
        with self.eng.begin() as c:
            n = q(c, "UPDATE plans SET rounds=rounds+1,status='draft',updated_at=:t WHERE id=:i AND company_id=:c AND status='needs_clarification' AND rounds=:r", t=now(), i=pid, c=cid, r=plan["rounds"]).rowcount
        if n != 1:
            return {"plan_id": pid, "deduplicated": True}  # double click: the first one already applied the choice
        self._ev(cid, pid, "plan.clarified", f"Aclaración del dueño ({field}): {val}", {"field": field, "value": val}, agent="owner_assistant")
        self._plan_it(user, pid, ov)
        return {"plan_id": pid, "deduplicated": False}

    def confirm(self, user: dict, pid: str) -> dict:
        cid = user["company_id"]
        plan = self._plan(cid, pid)
        if not plan:
            raise ToolError("plan no encontrado")
        if plan["status"] in ("running", "waiting_human", "completed"):
            return {"plan_id": pid, "deduplicated": True, "status": plan["status"]}  # double click / second tab: nothing is created twice
        if plan["status"] != "awaiting_user_confirmation":
            raise ConflictError(f"el plan está {plan['status']}: no se puede confirmar")
        with self.eng.begin() as c:
            n = q(c, "UPDATE plans SET status='running',confirmed_by=:u,confirmed_at=:t,updated_at=:t WHERE id=:i AND company_id=:c AND status='awaiting_user_confirmation'", u=user["id"], t=now(), i=pid, c=cid).rowcount
            if n == 1:
                audit(c, cid, "user", user["id"], "plans.confirm", pid, "ok", "plan confirmado por una persona")
        if n != 1:
            return {"plan_id": pid, "deduplicated": True, "status": self._plan(cid, pid)["status"]}
        self._ev(cid, pid, "plan.confirmed", f"{user['name']} CONFIRMÓ el plan (acción humana)", {"by": user["name"]}, agent="owner_assistant")
        self._launch(pid)
        return {"plan_id": pid, "deduplicated": False, "status": "running"}

    def cancel(self, user: dict, pid: str) -> dict:
        cid = user["company_id"]
        plan = self._plan(cid, pid)
        if not plan:
            raise ToolError("plan no encontrado")
        if plan["status"] == "cancelled":
            return {"plan_id": pid, "deduplicated": True}
        if plan["status"] not in ACTIVE:
            raise ConflictError(f"el plan ya está {plan['status']}")
        with self.eng.begin() as c:
            n = q(c, f"UPDATE plans SET status='cancelled',updated_at=:t WHERE id=:i AND company_id=:c AND status IN ({','.join(repr(s) for s in ACTIVE)})", t=now(), i=pid, c=cid).rowcount
            if n != 1:
                return {"plan_id": pid, "deduplicated": True}
            tasks = q(c, "UPDATE tasks SET status='cancelada',evidence='plan cancelado por el dueño' WHERE company_id=:c AND run_id=:p AND status IN ('pendiente','en_espera')", c=cid, p=pid).rowcount
            steps = q(c, "UPDATE plan_steps SET status='cancelled',updated_at=:t WHERE plan_id=:p AND company_id=:c AND status NOT IN ('done')", t=now(), p=pid, c=cid).rowcount
            audit(c, cid, "user", user["id"], "plans.cancel", pid, "ok", f"{steps} paso(s) y {tasks} tarea(s) cancelados")
        self._ev(cid, pid, "plan.cancelled", f"{user['name']} canceló el plan: {steps} paso(s) sin ejecutar y {tasks} tarea(s) pendientes cancelados", {"steps": steps, "tasks": tasks}, agent="owner_assistant")
        return {"plan_id": pid, "deduplicated": False, "cancelled_steps": steps, "cancelled_tasks": tasks}

    # ------------------------------------------------------------------ execution (reconciler: safe to call any number of times)
    def on_task_event(self, plan_id: str | None) -> None:
        if plan_id and plan_id.startswith("pln_"):
            self._launch(plan_id)

    def recover(self) -> int:
        """After a restart: re-run the idempotent reconciler for plans that were mid-flight. Failed/blocked plans are NOT retried."""
        with self.eng.connect() as c:
            ids = [r["id"] for r in all_(c, "SELECT id FROM plans WHERE status IN ('running','waiting_human')")]
        for i in ids:
            self._launch(i)
        return len(ids)

    def advance(self, pid: str, epoch: int | None = None) -> None:
        self._tl.epoch = self.o.epoch if epoch is None else epoch
        with self._lock(pid):
            with self.eng.connect() as c:
                plan = one(c, "SELECT company_id,status FROM plans WHERE id=:i", i=pid)
            if not plan:
                return
            cid = plan["company_id"]
            try:
                self._advance(cid, pid)
            except LeaseLost as e:   # superseded by another instance: it owns the plan now; leave the state exactly as it is
                log.warning("plan %s detenido: %s", pid, e)
            except (ToolError, Denied) as e:
                self._stop(cid, pid, "blocked", f"El coordinador detuvo el plan: {e}")
            except Exception as e:  # unexpected → visible, safe summary only (traceback stays in the server log), NEVER auto-retried
                log.error("plan %s failed\n%s", pid, traceback.format_exc())
                self._stop(cid, pid, "failed", "Error interno durante la ejecución del plan. Requiere revisión; no se reintenta automáticamente.", error_class=type(e).__name__)

    def _stop(self, cid, pid, status, reason, error_class=None):
        with self.eng.begin() as c:
            n = q(c, "UPDATE plans SET status=:s,status_reason=:r,updated_at=:t WHERE id=:i AND company_id=:c AND status IN ('running','waiting_human')", s=status, r=reason, t=now(), i=pid, c=cid).rowcount
        if n:
            self._ev(cid, pid, "run.failed" if status == "failed" else "plan.blocked", reason, {"error_class": error_class, "summary": reason} if error_class else {"summary": reason})

    def _advance(self, cid: str, pid: str) -> None:
        for _ in range(14):
            plan = self._plan(cid, pid)
            if plan["status"] not in ("running", "waiting_human"):
                return
            with self.eng.connect() as c:
                steps = all_(c, "SELECT * FROM plan_steps WHERE plan_id=:p AND company_id=:c ORDER BY seq", p=pid, c=cid)
            by = {s["seq"]: s for s in steps}
            moved = False
            for s in steps:
                if s["status"] in ("done", "cancelled"):
                    continue
                dep_done = s["depends_on"] is None or by[s["depends_on"]]["status"] == "done"
                a, st = s["action"], s["status"]
                args = jload(s["args"], {})
                if a == "review_pending" and st == "planned":
                    out = self._call(cid, pid, "plans.lookup", {})
                    self._finish_read(cid, s, {"open_requests": out["open_requests"]})
                    moved = True
                elif a == "review_request" and st == "planned" and dep_done:
                    out = self._call(cid, pid, "plans.review", {"request_id": args["request_id"]})
                    self._finish_read(cid, s, {k: v for k, v in out.items() if not k.startswith("_")})
                    moved = True
                elif a in TASK_ROLE:
                    if st == "planned":
                        self._call(cid, pid, "plans.assign_task", {"plan_id": pid, "seq": s["seq"]}); moved = True
                    elif st == "waiting_dependency" and dep_done:
                        self._call(cid, pid, "plans.activate_step", {"plan_id": pid, "seq": s["seq"]}); moved = True
                    elif st == "waiting_human":
                        with self.eng.connect() as c:
                            t = one(c, "SELECT status FROM tasks WHERE id=:i AND company_id=:c", i=s["task_id"], c=cid)
                        if t and t["status"] == "hecha":  # the REGISTRY says it is done (not a chat claim) → verify evidence + real stock
                            self._call(cid, pid, "plans.verify_step", {"plan_id": pid, "seq": s["seq"]}); moved = True
                        elif t and t["status"] == "cancelada":
                            raise ToolError("una tarea del plan fue cancelada")
                elif a == "report_to_owner" and st == "planned" and all(x["status"] == "done" for x in steps if x["action"] != "report_to_owner"):
                    self._call(cid, pid, "plans.finish", {"plan_id": pid}); moved = True
            if not moved:
                break
        plan = self._plan(cid, pid)
        if plan["status"] in ("running", "waiting_human"):
            with self.eng.connect() as c:
                waiting = one(c, "SELECT 1 AS x FROM plan_steps WHERE plan_id=:p AND company_id=:c AND status='waiting_human'", p=pid, c=cid)
            want = "waiting_human" if waiting else "running"
            if want != plan["status"]:
                with self.eng.begin() as c:
                    q(c, "UPDATE plans SET status=:w,updated_at=:t WHERE id=:i AND company_id=:c AND status=:o", w=want, t=now(), i=pid, c=cid, o=plan["status"])

    def _finish_read(self, cid, s, result: dict) -> None:
        with self.eng.begin() as c:
            q(c, "UPDATE plan_steps SET status='done',result=:r,updated_at=:t WHERE id=:i AND company_id=:c AND status='planned'", r=jdump(result), t=now(), i=s["id"], c=cid)

    # ------------------------------------------------------------------ read model
    def view(self, cid: str, pid: str) -> dict | None:
        with self.eng.connect() as c:
            p = one(c, "SELECT * FROM plans WHERE id=:i AND company_id=:c", i=pid, c=cid)
            if not p:
                return None
            steps = all_(c, """SELECT s.seq,s.action,s.agent,s.access,s.permission,s.origin,s.title,s.status,s.depends_on,s.task_id,s.result,s.assignee_user_id,
                                      u.name AS assignee,u.role AS assignee_role,t.status AS task_status,t.evidence
                               FROM plan_steps s LEFT JOIN users u ON u.id=s.assignee_user_id AND u.company_id=s.company_id
                               LEFT JOIN tasks t ON t.id=s.task_id AND t.company_id=s.company_id
                               WHERE s.plan_id=:p AND s.company_id=:c ORDER BY s.seq""", p=pid, c=cid)
            by = one(c, "SELECT name FROM users WHERE id=:i AND company_id=:c", i=p["created_by"], c=cid)
        for s in steps:
            s["result"] = jload(s["result"], None)
            s["label"] = ACTION_LABEL.get(s["action"], s["action"])
        meta = jload(p["proposal"], {})
        return {"id": p["id"], "text": p["text"], "status": p["status"], "source": p["source"], "intent": p["intent"], "request_id": p["request_id"], "summary": p["summary"],
                "status_reason": p["status_reason"], "result_summary": p["result_summary"], "excluded": jload(p["excluded"], []), "clarification": jload(p["clarification"], None),
                "notes": meta.get("notes", []), "fallback": meta.get("fallback"), "fallback_label": FALLBACK_LABEL.get(meta.get("fallback") or ""), "rounds": p["rounds"], "max_rounds": MAX_ROUNDS, "created_by": by["name"] if by else None, "created_by_id": p["created_by"],
                "confirmed_at": p["confirmed_at"], "created_at": p["created_at"], "steps": steps}


class ConflictError(Exception):
    pass
