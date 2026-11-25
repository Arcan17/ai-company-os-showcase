"""Coordination engine. Business flow is explicit code (deterministic state machine); an LLM provider may only
PROPOSE a classification. Every step is a persisted event, every action goes through the permissioned tool layer.

Phases:  intake  →  (waiting_approval | blocked)  →  decided  →  completed
         blocked --resolve--> recover → waiting_approval → decided → completed
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
import traceback
from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from ..config import settings
from ..db import all_, jdump, jload, now, one, q, uid
from ..directory import ROLE_BY_TEMPLATE, pick_assignee
from ..events import audit, emit, emit_in
from ..lease import LeaseLost
from ..permissions import effective_permissions
from ..providers.factory import get_provider
from ..tools import Denied, Principal, ToolError, invoke
from ..tools.registry import REGISTRY


log = logging.getLogger("aicos.engine")


def pick_assignee_eng(eng, company_id: str, role: str):
    with eng.connect() as c:
        return pick_assignee(c, company_id, role)


class Conflict(Exception):
    pass


@dataclass
class ChatInput:
    message: str
    name: str | None
    phone: str | None
    email: str | None
    client_msg_id: str
    retry_secret: str | None = None      # lo genera el cliente (CSPRNG) y prueba que quien reintenta ES quien envió el mensaje; NUNCA se guarda (solo su huella dentro de la clave de idempotencia)


class Orchestrator:
    def __init__(self, eng: Engine):
        self.eng = eng
        self.provider = get_provider(eng)
        self.plans = None  # PlanEngine (V0.4), attached by create_app
        self.keeper = None  # lease.LeaseKeeper (V0.6), attached by create_app; None = no lease logic (single-process tests)

    # ---------- instance role (V0.6) ----------
    @property
    def active(self) -> bool:
        return self.keeper.held if self.keeper else True

    @property
    def epoch(self) -> int:
        return self.keeper.epoch if self.keeper else 0

    def check(self, epoch: int) -> None:
        """Called by every run/plan thread before each step: stops it (LeaseLost) once this instance lost the lease since the thread started."""
        if self.keeper:
            self.keeper.check(epoch)

    # ---------- principals ----------
    def principal(self, company_id: str, agent_role: str, user_id: str | None = None) -> Principal:
        """Principal of an agent. SHARED agents (reception, coordinator) belong to the company. PERSONAL assistants (owner/admin/tech) are one row PER EMPLOYEE
        created from a template: pass `user_id` to act as that employee's assistant; without it the least-loaded ELIGIBLE employee of the role is used
        (legacy scenarios A–D) — never «the first». Effective permissions of a personal assistant = agent template ∩ the employee's role."""
        personal = agent_role in ROLE_BY_TEMPLATE
        with self.eng.connect() as c:
            if personal:
                if user_id is None:
                    pick = pick_assignee(c, company_id, ROLE_BY_TEMPLATE[agent_role])
                    if not pick:
                        raise ToolError(f"no hay un empleado elegible con asistente activo para {ROLE_BY_TEMPLATE[agent_role]}")
                    user_id = pick["id"]
                a = one(c, "SELECT * FROM agents WHERE company_id=:c AND role=:r AND user_id=:u AND enabled=1", c=company_id, r=agent_role, u=user_id)
                u = one(c, "SELECT id,role,COALESCE(active,1) AS active FROM users WHERE id=:i AND company_id=:c", i=user_id, c=company_id)
                if not a or not u or not u["active"]:
                    raise ToolError(f"asistente no disponible: {agent_role} (sin asignación activa para ese empleado)")
            else:
                a = one(c, "SELECT * FROM agents WHERE company_id=:c AND role=:r AND user_id IS NULL AND enabled=1", c=company_id, r=agent_role)
                if not a:
                    raise ToolError(f"agente no disponible: {agent_role}")
                u = None
        perms = effective_permissions(set(jload(a["permissions"], [])), u["role"] if u else None)
        return Principal("agent", a["id"], a["name"], agent_role, company_id, perms, user_id=u["id"] if u else None, user_role=u["role"] if u else None)

    # ---------- run lifecycle ----------
    def start_chat(self, slug: str, inp: ChatInput) -> dict:
        try:
            return self._start_chat(slug, inp)
        except (IntegrityError, OperationalError):
            return self._start_chat(slug, inp)   # una ráfaga idéntica: otra petición insertó primero (clave única); esta vuelve a LEER y la encuentra, sin duplicar nada

    def _start_chat(self, slug: str, inp: ChatInput) -> dict:
        """CONTRATO DE IDEMPOTENCIA DEL CHAT PÚBLICO (Issue #20, docs/v07/I20_IDEMPOTENCIA_CHAT_PUBLICO.md). El id del mensaje (`client_msg_id`) NO es una credencial: puede conocerse o adivinarse.
        Un reintento solo encuentra su conversación si presenta además el `retry_secret` que generó el cliente: la clave es (id, huella del secreto). Sin el secreto correcto no hay nada que
        repetir: la petición es una conversación NUEVA y suya, nunca la de otra persona. Sin secreto no hay idempotencia por id (cada petición es una conversación)."""
        with self.eng.begin() as c:
            co = one(c, "SELECT * FROM companies WHERE slug=:s", s=slug)
            if not co:
                raise ToolError("empresa no encontrada")
            scope = hashlib.sha256(inp.retry_secret.encode()).hexdigest()[:32] if inp.retry_secret else secrets.token_hex(16)
            key = f"chat:{inp.client_msg_id}:{scope}"
            ex = one(c, "SELECT * FROM runs WHERE company_id=:c AND idem_key=:k", c=co["id"], k=key)
            if ex:  # retry of the same client message (same id AND same secret) → same run, nothing duplicated
                conv = one(c, "SELECT token FROM conversations WHERE id=:i AND company_id=:c", i=jload(ex["state"], {}).get("conversation_id"), c=co["id"])
                return {"run_id": ex["id"], "deduplicated": True, "conversation_token": conv["token"] if conv else None,
                        "request_id": ex["request_id"] if ex["request_id"] != "-" else None}
            conv_id, token, run_id = uid("cnv_"), uid("tok_") + uid(), uid("run_")
            q(c, "INSERT INTO conversations(id,company_id,channel,token,created_at) VALUES(:i,:c,'chat_web',:t,:n)", i=conv_id, c=co["id"], t=token, n=now())
            q(c, "INSERT INTO messages(id,company_id,conversation_id,sender,body,status,idem_key,created_at) VALUES(:i,:c,:cv,'customer',:b,'recibido',:k,:n)",
              i=uid("msg_"), c=co["id"], cv=conv_id, b=inp.message, k=f"in:{key}", n=now())
            state = {"conversation_id": conv_id, "input": {**{k: v for k, v in inp.__dict__.items() if k != "retry_secret"}, "chat_key": key}}   # el secreto NO se persiste
            q(c, "INSERT INTO runs(id,company_id,request_id,scenario,status,phase,state,provider,created_by,idem_key,created_at,updated_at) VALUES(:i,:c,'-','chat',:s,'intake',:st,:p,'customer',:k,:t,:t)",
              i=run_id, c=co["id"], s="running", st=jdump(state), p=self.provider.name, k=key, t=now())
        self._launch(run_id)
        return {"run_id": run_id, "deduplicated": False, "conversation_token": token, "request_id": None}

    def _launch(self, run_id: str) -> None:
        if not self.active:   # backstop: the HTTP gate already refuses writes on a standby; this covers losing the lease in the instant between that check and here
            self._fail_run(run_id, reason="lease_lost", audit_action="runs.interrupted", title="Ejecución interrumpida: esta instancia ya no es la activa",
                           summary="Esta instancia perdió el lease de la base de datos antes de iniciar el trabajo. No se ejecutó nada; requiere revisión de un administrador.")
            return
        epoch = self.epoch
        if settings.sync_runs:
            self._execute(run_id, epoch)
        else:
            threading.Thread(target=self._execute, args=(run_id, epoch), daemon=True, name=f"run-{run_id}").start()

    def _set(self, run_id: str, **kw) -> None:
        sets = ",".join(f"{k}=:{k}" for k in kw)
        guard = " AND status='running'" if "status" in kw else ""   # a status change only ever comes from the thread that is running the run …
        with self.eng.begin() as c:
            n = q(c, f"UPDATE runs SET {sets},updated_at=:_t WHERE id=:_i{guard}", _t=now(), _i=run_id, **kw).rowcount
        if guard and n != 1:   # … so if the run is no longer `running`, another instance already declared it interrupted: never overwrite that verdict
            raise LeaseLost("la ejecución ya no está en curso: otra instancia la declaró interrumpida")

    def _execute(self, run_id: str, epoch: int | None = None) -> None:
        epoch = self.epoch if epoch is None else epoch
        with self.eng.connect() as c:
            run = one(c, "SELECT * FROM runs WHERE id=:i", i=run_id)
        f = Flow(self, run, epoch)
        try:
            getattr(f, "phase_" + run["phase"])()
        except LeaseLost as e:   # not a failure of the run: this thread was superseded. The instance that holds the lease owns the verdict.
            log.warning("run %s detenido en fase %s: %s", run_id, run["phase"], e)
        except Exception as e:  # any unexpected failure is visible and recoverable, never silent
            tb = traceback.format_exc()
            log.error("run %s failed in phase %s\n%s", run_id, run["phase"], tb)
            self._fail_run(run_id, reason="error", title=f"Error: {type(e).__name__}", error_class=type(e).__name__, phase=run["phase"],  # safe summary only; full traceback stays in the server log
                           summary="Error interno durante la ejecución. Requiere revisión de un administrador.")

    def _fail_run(self, run_id: str, *, reason: str, title: str, summary: str, error_class: str | None = None, phase: str | None = None, audit_action: str | None = None) -> bool:
        """The ONE place a running run becomes failed. Status + audit + `run.failed` event + the request's «ejecucion_fallida» are ONE transaction: a process killed at any
        point leaves either nothing or everything (compare-and-set on status='running' also makes it idempotent and safe against two instances)."""
        with self.eng.begin() as c:
            run = one(c, "SELECT * FROM runs WHERE id=:i", i=run_id)
            if not run or q(c, "UPDATE runs SET status='failed',updated_at=:t WHERE id=:i AND status='running'", t=now(), i=run_id).rowcount != 1:
                return False
            cid, ph = run["company_id"], phase or run["phase"]
            if audit_action:
                audit(c, cid, "system", run_id, audit_action, run_id, "ok", f"fase={ph}")
            emit_in(c, cid, run_id, "run.failed", agent="coordinator", title=title,
                    data={"error_class": error_class or "Interrupted", "reason": reason, "phase": ph, "summary": summary})
            if run["request_id"] != "-":
                q(c, "UPDATE requests SET status='ejecucion_fallida',updated_at=:t WHERE id=:i AND company_id=:c", t=now(), i=run["request_id"], c=cid)
                audit(c, cid, "system", run_id, "requests.mark_failed", run["request_id"], "ok", "run fallido")
        return True

    def recover_runs(self) -> int:
        """Called ONLY when this instance gains the lease: every run still `running` lost its thread with the previous owner. Each is declared failed/interrupted by `_fail_run`
        (atomic and idempotent) and a human decides. Nothing is retried or replayed: a phase is a chain of effects outside one transaction, and re-running one without
        reconciling them could duplicate a movement. Runs waiting for a person (`waiting_*`, `blocked`) are NOT interrupted: they keep waiting, as their state says."""
        with self.eng.connect() as c:
            rows = all_(c, "SELECT id,phase FROM runs WHERE status='running'")
        n = 0
        for r in rows:
            n += self._fail_run(r["id"], reason="interrupted", audit_action="runs.interrupted", title="Ejecución interrumpida por un reinicio", phase=r["phase"],
                                summary="El servidor se detuvo mientras esta ejecución estaba en curso. No se reintentó nada automáticamente; requiere revisión de un administrador.")
        if n:
            log.warning("recuperación: %d ejecución(es) interrumpida(s) marcadas para revisión", n)
        return n

    # ---------- parking a run on an approval (closes the lost wake-up) ----------
    def park_for_approval(self, company_id: str, run_id: str, approval_id: str) -> bool:
        """`running` → `waiting_approval` Y comprobación de la decisión en UNA transacción. La aprobación se hace visible (approvals.request) varios pasos ANTES de que la ejecución espere; si alguien decide
        en esa ventana, el CAS de `decide()` (`WHERE status='waiting_approval'`) no encuentra nada y nadie relanzaría la ejecución (lost wake-up: quedaba esperando una aprobación ya decidida).
        Protocolo (como Dekker): `decide` ESCRIBE la aprobación y luego intenta el CAS sobre la ejecución; aquí se ESCRIBE la espera y luego se RELEE la aprobación. Al menos uno de los dos ve al otro y
        quien gana el CAS `waiting_approval → running/decided` es el ÚNICO que reanuda. Conserva el fencing: solo transiciona desde `running` (si otra instancia declaró la ejecución interrumpida/fallida,
        ese veredicto es final y se lanza LeaseLost) y el relanzamiento pasa por `_launch` (lease activo). → True si esta llamada reanudó la ejecución."""
        resume = False
        with self.eng.begin() as c:
            if q(c, "UPDATE runs SET status='waiting_approval',updated_at=:t WHERE id=:r AND company_id=:c AND status='running'", t=now(), r=run_id, c=company_id).rowcount != 1:
                raise LeaseLost("la ejecución ya no está en curso: otra instancia la declaró interrumpida")
            ap = one(c, "SELECT status FROM approvals WHERE id=:a AND run_id=:r AND company_id=:c", a=approval_id, r=run_id, c=company_id)
            if ap and ap["status"] != "pendiente":     # decidida mientras la ejecución aún no esperaba: se reanuda AHORA (si decide() ya ganó el CAS, rowcount=0 y no se duplica)
                resume = q(c, "UPDATE runs SET status='running',phase='decided',updated_at=:t WHERE id=:r AND company_id=:c AND status='waiting_approval'", t=now(), r=run_id, c=company_id).rowcount == 1
        if resume:
            self._launch(run_id)
        return resume

    # ---------- human decision on an approval ----------
    def decide(self, company_id: str, approval_id: str, user: dict, decision: str, comment: str | None) -> dict:
        assert decision in ("approved", "rejected")
        with self.eng.begin() as c:
            ap = one(c, "SELECT * FROM approvals WHERE id=:i AND company_id=:c", i=approval_id, c=company_id)
            if not ap:
                raise ToolError("aprobación no encontrada")
            if ap["status"] != "pendiente":
                if ap["status"] == decision:
                    return {"approval_id": ap["id"], "status": ap["status"], "deduplicated": True}
                raise Conflict(f"la aprobación ya fue {ap['status']}")
            n = q(c, "UPDATE approvals SET status=:s,decided_by_user_id=:u,decided_at=:t,comment=:cm WHERE id=:i AND company_id=:c AND status='pendiente'",
                  s=decision, u=user["id"], t=now(), cm=comment, i=ap["id"], c=company_id).rowcount
            if n != 1:
                raise Conflict("la aprobación fue decidida concurrentemente")
            audit(c, company_id, "user", user["id"], f"approvals.decide:{decision}", ap["id"], "ok", comment or "")
        emit(self.eng, company_id, ap["run_id"], "approval.decided", agent="owner_assistant", mode="real",
             title=f"{user['name']} {'APROBÓ' if decision == 'approved' else 'RECHAZÓ'}: {ap['subject']}",
             data={"approval_id": ap["id"], "decision": decision, "by": user["name"], "comment": comment, "amount": ap["amount"]})
        with self.eng.begin() as c:  # compare-and-set: only one resume per decision
            n = q(c, "UPDATE runs SET status='running',phase='decided',updated_at=:t WHERE id=:r AND company_id=:c AND status='waiting_approval'",
                  t=now(), r=ap["run_id"], c=company_id).rowcount
        if n == 1:
            self._launch(ap["run_id"])
        return {"approval_id": ap["id"], "status": decision, "deduplicated": False}

    def resolve(self, company_id: str, run_id: str, user: dict, action: str) -> dict:
        with self.eng.begin() as c:
            run = one(c, "SELECT * FROM runs WHERE id=:i AND company_id=:c", i=run_id, c=company_id)
            if not run:
                raise ToolError("ejecución no encontrada")
            st = jload(run["state"], {})
            if action not in [a["id"] for a in st.get("alternatives", [])]:
                raise ToolError("alternativa no válida para esta ejecución")
            n = q(c, "UPDATE runs SET status='running',phase='recover',state=:s,updated_at=:t WHERE id=:i AND company_id=:c AND status='blocked'",
                  s=jdump({**st, "choice": action}), t=now(), i=run_id, c=company_id).rowcount
            if n != 1:
                raise Conflict("la ejecución ya no está bloqueada")
            audit(c, company_id, "user", user["id"], f"runs.resolve:{action}", run_id, "ok")
        emit(self.eng, company_id, run_id, "run.resolved", agent="coordinator", mode="real", title=f"{user['name']} eligió: {action}", data={"action": action, "by": user["name"]})
        self._launch(run_id)
        return {"run_id": run_id, "action": action}

    # ---------- scenario D entry points (human-driven) ----------
    def run_for_request(self, company_id: str, request_id: str) -> dict | None:
        with self.eng.connect() as c:
            return one(c, "SELECT * FROM runs WHERE company_id=:c AND request_id=:r ORDER BY created_at DESC LIMIT 1", c=company_id, r=request_id)

    def continue_collab(self, company_id: str, run_id: str) -> bool:
        """Technician's material request is persisted → administration phase starts (compare-and-set: once)."""
        with self.eng.begin() as c:
            n = q(c, "UPDATE runs SET status='running',phase='collab_admin',updated_at=:t WHERE id=:r AND company_id=:c AND status='waiting_technician'", t=now(), r=run_id, c=company_id).rowcount
        if n == 1:
            self._launch(run_id)
        return n == 1

    def complete_task(self, user: dict, task_id: str, evidence: str, assistant_role: str) -> dict:
        """A person completes THEIR task through their assistant (same permissions). Effects are applied once; the run then continues."""
        with self.eng.connect() as c:
            t = one(c, "SELECT * FROM tasks WHERE id=:i AND company_id=:c", i=task_id, c=user["company_id"])
        if not t:
            raise ToolError("tarea no encontrada")
        pr = self.principal(user["company_id"], assistant_role, user["id"])
        out = invoke(self.eng, pr, "tasks.complete", {"task_id": task_id, "evidence": evidence}, run_id=t["run_id"], provider=self.provider)
        if t["kind"] == "plan_step" and self.plans:  # V0.4: the coordinator re-reads the REGISTRY (also when the call was a duplicate: it reconciles)
            self.plans.on_task_event(t["run_id"])
            return out
        if out["deduplicated"] or not t["run_id"] or t["kind"] not in ("material", "recepcion"):
            return out
        phase = "after_material" if t["kind"] == "material" else "collab_close"
        with self.eng.begin() as c:
            n = q(c, "UPDATE runs SET status='running',phase=:p,updated_at=:t WHERE id=:r AND company_id=:c AND status='waiting_task'", p=phase, t=now(), r=t["run_id"], c=user["company_id"]).rowcount
        if n == 1:
            self._launch(t["run_id"])
        return out

    def process_due(self, user: dict, as_of: str | None) -> dict:
        from ..tools.registry import Principal
        from ..permissions import ROLE_PERMISSIONS
        p = Principal("user", user["id"], user["name"], user["role"], user["company_id"], set(ROLE_PERMISSIONS[user["role"]]), user_id=user["id"], user_role=user["role"])
        return invoke(self.eng, p, "commitments.process_due", {"as_of": as_of}, provider=self.provider)

    # ---------- customer-side confirmation (only real confirmation closes a visit) ----------
    def confirm_visit(self, slug: str, request_id: str, code: str) -> dict:
        with self.eng.begin() as c:
            co = one(c, "SELECT id FROM companies WHERE slug=:s", s=slug)
            v = one(c, "SELECT * FROM visits WHERE company_id=:c AND request_id=:r", c=co["id"], r=request_id) if co else None
            if not v or v["confirm_code"] != code.strip().upper():
                raise ToolError("código de confirmación inválido")
            if v["customer_confirmed"]:
                return {"status": "confirmada", "deduplicated": True}
            q(c, "UPDATE visits SET status='confirmada',customer_confirmed=1,confirmed_at=:t WHERE id=:i AND company_id=:c", t=now(), i=v["id"], c=co["id"])
            q(c, "UPDATE requests SET status='visita_confirmada',updated_at=:t WHERE id=:r AND company_id=:c", t=now(), r=request_id, c=co["id"])
            q(c, "UPDATE work_orders SET status='programada',updated_at=:t WHERE request_id=:r AND company_id=:c", t=now(), r=request_id, c=co["id"])
            q(c, "UPDATE tasks SET status='hecha' WHERE company_id=:c AND request_id=:r AND idem_key LIKE :pat", c=co["id"], r=request_id, pat="%:await-customer")
            for m in all_(c, "SELECT id FROM commitments WHERE company_id=:c AND request_id=:r AND customer_facing=1 AND status IN ('abierto','vencido')", c=co["id"], r=request_id):
                q(c, "UPDATE commitments SET status='cumplido',last_evidence='El cliente confirmó la visita',updated_at=:t WHERE id=:i AND company_id=:c", t=now(), i=m["id"], c=co["id"])
                q(c, "UPDATE tasks SET status='cancelada' WHERE company_id=:c AND commitment_id=:k AND kind='recordatorio' AND status='pendiente'", c=co["id"], k=m["id"])
                q(c, "UPDATE messages SET status='cancelado' WHERE company_id=:c AND commitment_id=:k AND status IN ('preparado_no_enviado','requiere_aprobacion')", c=co["id"], k=m["id"])
            audit(c, co["id"], "customer", request_id, "visits.customer_confirm", v["id"], "ok")
            run = one(c, "SELECT id FROM runs WHERE company_id=:c AND request_id=:r", c=co["id"], r=request_id)
        if run:
            emit(self.eng, co["id"], run["id"], "customer.confirmed", agent="reception", mode="real",
                 title="El CLIENTE confirmó la visita", data={"request_id": request_id, "visit_id": v["id"], "slot": v["slot_start"]})
        return {"status": "confirmada", "deduplicated": False}


class Flow:
    """One run's execution context: emits events and calls tools as the right agent."""

    def __init__(self, o: Orchestrator, run: dict, epoch: int = 0):
        self.o, self.eng, self.run, self.epoch = o, o.eng, run, epoch
        self.cid, self.rid = run["company_id"], run["id"]
        self.st = jload(run["state"], {})
        self._pr: dict[str, Principal] = {}

    # --- primitives ---
    def alive(self) -> None:
        """Fencing before every step: this thread may act only while the lease epoch is the one it started in AND the run row still says `running`
        (someone — e.g. the instance that took the lease — may have declared it interrupted in the meantime; that verdict is final)."""
        self.o.check(self.epoch)
        with self.eng.connect() as c:
            st = one(c, "SELECT status FROM runs WHERE id=:i", i=self.rid)
        if not st or st["status"] != "running":
            raise LeaseLost("la ejecución dejó de estar en curso (otra instancia la declaró interrumpida)")

    def pace(self):
        self.alive()
        if settings.step_delay:
            time.sleep(settings.step_delay)

    def park_for_approval(self, approval_id: str) -> None:
        """Ver Orchestrator.park_for_approval: esperar la decisión SIN perder una que ya llegó."""
        self.alive()
        self.o.park_for_approval(self.cid, self.rid, approval_id)

    def emit(self, type_, agent=None, title="", mode="real", **data):
        self.alive()
        emit(self.eng, self.cid, self.rid, type_, agent=agent, mode=mode, title=title, data=data)

    def save(self, **kw):
        self.alive()
        self.st.update(kw)
        self.o._set(self.rid, state=jdump(self.st))

    def _user_for(self, role: str) -> str | None:
        """Which employee a personal assistant acts for in THIS run: the one the run already involves (stable across phases), else the least-loaded eligible one."""
        if role == "admin_assistant":
            if not self.st.get("admin_id"):
                p = pick_assignee_eng(self.eng, self.cid, "admin")
                if p:
                    self.save(admin_id=p["id"], admin=p["name"])
            return self.st.get("admin_id")
        if role == "tech_assistant":
            return self.st.get("technician_id")   # None → least-loaded eligible technician
        return None                               # owner_assistant: the company's owner (single, resolved by the directory)

    def call(self, role: str, tool_name: str, /, **args) -> dict:
        self.alive()
        if role not in self._pr:
            self._pr[role] = self.o.principal(self.cid, role, self._user_for(role) if role in ROLE_BY_TEMPLATE else None)
        return invoke(self.eng, self._pr[role], tool_name, args, run_id=self.rid, provider=self.o.provider, pace=self.pace)

    def activate(self, role, why):
        self.emit("agent.activated", role, why)
        self.pace()

    def finish(self, role, summary):
        self.emit("agent.completed", role, summary)

    def delegate(self, frm, to, task, key):
        self.call(frm, "delegations.create", from_agent=frm, to_agent=to, task=task, idem_key=f"{self.rid}:{key}")
        self.emit("agent.delegated", frm, f"{frm} → {to}: {task}", to=to, task=task)
        self.activate(to, task)

    def _db(self, sql, **p):
        with self.eng.connect() as c:
            return all_(c, sql, c=self.cid, **p)

    # --- phase 1: reception → coordinator → admin → approval request ---
    def phase_intake(self):
        inp = self.st["input"]
        self.emit("run.started", "customer", "Mensaje recibido por el chat de atención", message=inp["message"], channel="chat web (prueba)")
        self.activate("reception", "Atender mensaje del cliente")
        cust = None
        for query in [inp.get("phone"), inp.get("email")]:
            if query and not cust:
                m = self.call("reception", "customers.find", query=query)["matches"]
                cust = m[0] if m else None
        if not cust and inp.get("name") and not (inp.get("phone") or inp.get("email")):
            m = self.call("reception", "customers.find", query=inp["name"])["matches"]
            cust = m[0] if len(m) == 1 else None
        if not cust:
            if not inp.get("name"):
                self.emit("agent.blocked", "reception", "No se pudo identificar al cliente y falta su nombre")
                self.emit("run.blocked", "reception", "Se necesita el nombre del cliente", alternatives=[])
                return self.o._set(self.rid, status="blocked", state=jdump({**self.st, "alternatives": []}))
            cust = self.call("reception", "customers.create", name=inp["name"], phone=inp.get("phone"), email=inp.get("email"))["customer"]
        hist = self.call("reception", "customers.history", customer_id=cust["id"])["requests"]
        req = self.call("reception", "requests.create", customer_id=cust["id"], description=inp["message"],
                        idem_key=inp.get("chat_key") or f"chat:{inp['client_msg_id']}", conversation_id=self.st["conversation_id"])
        with self.eng.begin() as c:
            q(c, "UPDATE requests SET conversation_id=:cv WHERE id=:i AND company_id=:c AND conversation_id IS NULL", cv=self.st["conversation_id"], i=req["request_id"], c=self.cid)
            q(c, "UPDATE conversations SET customer_id=:k WHERE id=:i AND company_id=:c", k=cust["id"], i=self.st["conversation_id"], c=self.cid)
            q(c, "INSERT INTO messages(id,company_id,conversation_id,sender,body,status,idem_key,created_at) VALUES(:i,:c,:cv,'agent:reception',:b,'enviado_chat',:k,:t) ON CONFLICT DO NOTHING",
              i=uid("msg_"), c=self.cid, cv=self.st["conversation_id"], k=f"ack:{req['request_id']}", t=now(),
              b=f"Hola {cust['name'].split(' ')[0]}, recibimos tu solicitud ({req['request_id']}). Un coordinador la está revisando y te enviaremos una propuesta.")
        self.o._set(self.rid, request_id=req["request_id"])
        self.save(request_id=req["request_id"], customer=cust, history_count=len(hist))
        self.delegate("reception", "coordinator", f"Nuevo caso {req['request_id']} de {cust['name']} ({len(hist)} solicitud(es) previas)", "to-coordinator")
        self.finish("reception", "Solicitud registrada y entregada al coordinador")
        cl = self.call("coordinator", "requests.classify", request_id=req["request_id"])
        av = self.call("coordinator", "availability.technicians", limit=5)
        if not av["slots"]:
            return self.block("coordinator", "Sin disponibilidad de técnicos", [{"id": "diagnostic_only", "label": "Reintentar cuando haya disponibilidad"}], None)
        self.save(category=cl["category"], urgency=cl["urgency"], parts=cl["parts"], slot=av["slots"][0])
        if cl["category"] == "mantencion":  # maintenance visits follow the collaborative flow (scenario D)
            self.o._set(self.rid, scenario="D")
        self.delegate("coordinator", "admin_assistant", "Preparar presupuesto y verificar repuestos", "to-admin")
        pr = self.call("admin_assistant", "pricing.lookup", category=cl["category"])
        inv = self.call("admin_assistant", "inventory.check", skus=cl["parts"]) if cl["parts"] else {"missing": [], "parts": []}
        if inv["missing"]:
            return self.block_for_part(inv)
        codes = [s["code"] for s in pr["services"]]
        self.save(service_codes=codes)
        quote = self.call("admin_assistant", "quotes.prepare", request_id=req["request_id"], kind="principal", service_codes=codes, part_skus=cl["parts"])
        self.finish("admin_assistant", f"Presupuesto borrador ${quote['total']:,.0f} listo")
        self.ask_approval(quote, "Presupuesto principal")

    def block(self, role, reason, alternatives, inv):
        self.emit("agent.blocked", role, reason)
        self.emit("run.blocked", "coordinator", f"Bloqueado: {reason}", reason=reason, alternatives=alternatives)
        self.o._set(self.rid, status="blocked", state=jdump({**self.st, "alternatives": alternatives}))

    def block_for_part(self, inv):
        miss = inv["missing"][0]
        part = next(p for p in inv["parts"] if p["sku"] == miss)
        rid = self.st["request_id"]
        self.emit("agent.blocked", "admin_assistant", f"Sin stock de {miss} ({part.get('name', miss)})")
        self.call("admin_assistant", "materials.request", sku=miss, qty=1, request_id=rid)
        self.call("admin_assistant", "tasks.create", request_id=rid, title=f"Reponer repuesto {miss} y avisar al cliente", assignee_agent="admin_assistant", idem_key=f"{self.rid}:restock-{miss}")
        self.delegate("admin_assistant", "coordinator", f"Bloqueo: sin stock de {miss}", "block-report")
        self.call("coordinator", "requests.update_status", request_id=rid, status="bloqueada_repuesto")
        alt = part.get("alternative")
        alts = []
        if alt and alt["stock"] > 0:
            alts.append({"id": "use_alternative_part", "label": f"Usar {alt['name']} ({alt['sku']}, stock {alt['stock']}) — nuevo presupuesto con aprobación", "sku": alt["sku"]})
        alts.append({"id": "diagnostic_only", "label": "Ofrecer solo visita de diagnóstico (sin repuesto) — nuevo presupuesto con aprobación"})
        self.call("owner_assistant", "company.summary")
        self.finish("coordinator", "Caso bloqueado por falta de repuesto; alternativas propuestas")
        self.block("coordinator", f"Falta repuesto {miss}", alts, inv)

    def ask_approval(self, quote, subject):
        rid = self.st["request_id"]
        self.delegate("admin_assistant", "coordinator", f"Presupuesto listo (${quote['total']:,.0f})", "quote-ready")
        self.call("coordinator", "requests.update_status", request_id=rid, status="presupuesto_en_revision")
        ap = self.call("coordinator", "approvals.request", request_id=rid, subject=subject,
                       summary=f"{self.st['customer']['name']} · {self.st.get('category')} · total ${quote['total']:,.0f}", amount=quote["total"])
        self.delegate("coordinator", "owner_assistant", "Presentar el presupuesto al dueño para aprobación", "to-owner")
        self.call("owner_assistant", "approvals.pending")
        self.emit("approval.requested", "owner_assistant", f"Esperando decisión humana: {subject} (${quote['total']:,.0f})",
                  approval_id=ap["approval_id"], amount=quote["total"], subject=subject)
        self.emit("agent.waiting", "owner_assistant", "Esperando aprobación del dueño")
        self.emit("run.waiting_approval", "coordinator", "Ejecución en pausa hasta que un humano decida")
        self.save(quote_total=quote["total"], approval_subject=subject)
        self.park_for_approval(ap["approval_id"])

    # --- phase 2: blocked → alternative chosen ---
    def phase_recover(self):
        choice, rid = self.st["choice"], self.st["request_id"]
        if choice in ("reschedule_after_restock", "proceed_without_material"):
            return self.collab_resolve(choice)
        self.activate("coordinator", f"Retomar con alternativa: {choice}")
        self.delegate("coordinator", "admin_assistant", "Preparar presupuesto alternativo", "to-admin-recover")
        if choice == "use_alternative_part":
            sku = next(a["sku"] for a in self.st["alternatives"] if a["id"] == choice)
            inv = self.call("admin_assistant", "inventory.check", skus=[sku])
            if inv["missing"]:
                return self.block("admin_assistant", f"La alternativa {sku} tampoco tiene stock", [{"id": "diagnostic_only", "label": "Solo diagnóstico"}], inv)
            pr = self.call("admin_assistant", "pricing.lookup", category=self.st["category"])
            codes, parts = [s["code"] for s in pr["services"]], [sku]
        else:
            codes, parts = ["DIAG"], []
        self.save(service_codes=codes, parts=parts)
        quote = self.call("admin_assistant", "quotes.prepare", request_id=rid, kind="principal", service_codes=codes, part_skus=parts)
        self.finish("admin_assistant", f"Presupuesto alternativo ${quote['total']:,.0f}")
        self.ask_approval(quote, "Presupuesto alternativo")

    # --- phase 3: after the human decision ---
    def phase_decided(self):
        rid = self.st["request_id"]
        ap = self._db("SELECT * FROM approvals WHERE company_id=:c AND run_id=:r ORDER BY created_at DESC LIMIT 1", r=self.rid)[0]
        self.activate("coordinator", f"Decisión humana recibida: {ap['status']}")
        if ap["subject"].startswith("Material"):
            return self.phase_material_decided(ap)
        if ap["status"] == "approved":
            self.delegate("coordinator", "admin_assistant", "Preparar propuesta de visita", "to-admin-visit")
            self.call("admin_assistant", "quotes.set_status", request_id=rid, kind="principal", status="aprobado")
            slot = self.st["slot"]
            try:
                visit = self.call("admin_assistant", "visits.propose", request_id=rid, availability_id=slot["id"])
            except ToolError:  # slot taken meanwhile → recover with the next free one
                self.emit("agent.recovering", "admin_assistant", "La franja ya no está libre; buscando otra")
                slots = self.call("admin_assistant", "availability.technicians", limit=1)["slots"]
                if not slots:
                    return self.block("admin_assistant", "Sin franjas libres", [{"id": "diagnostic_only", "label": "Reintentar más tarde"}], None)
                slot = slots[0]
                visit = self.call("admin_assistant", "visits.propose", request_id=rid, availability_id=slot["id"])
            self.call("admin_assistant", "workorders.create", request_id=rid, technician_id=slot["technician_id"])
            self.delegate("admin_assistant", "tech_assistant", "Registrar orden pendiente de confirmación del cliente", "to-tech")
            self.save(technician_id=slot["technician_id"])
            self.call("tech_assistant", "workorders.acknowledge", request_id=rid)
            self.call("admin_assistant", "tasks.create", request_id=rid, title="Esperar confirmación del cliente para la visita", assignee_agent="reception", idem_key=f"{self.rid}:await-customer")
            self.call("admin_assistant", "tasks.create", request_id=rid, title=f"Preparar visita ({slot['slot_start']}) — pendiente de confirmación", assignee_user_id=slot["technician_id"], idem_key=f"{self.rid}:prep-visit")
            self.finish("tech_assistant", "Orden registrada, técnico notificado")
            self.finish("admin_assistant", "Propuesta de visita preparada (NO confirmada)")
            self.delegate("coordinator", "reception", "Preparar la comunicación al cliente", "to-reception-msg")
            code = self._db("SELECT confirm_code FROM visits WHERE company_id=:c AND request_id=:r", r=rid)[0]["confirm_code"]
            cust = self.st["customer"]
            body = self.o.provider.draft_customer_message("visit_proposal", {"customer": cust["name"], "slot": slot["slot_start"].replace("T", " "), "total": self.st["quote_total"]})
            body += f" Código de confirmación: {code}."
            self.call("reception", "messages.prepare_customer", request_id=rid, kind="visit_proposal", body=body)
            self.finish("reception", "Mensaje preparado (no enviado: canal simulado)")
            self.call("coordinator", "requests.update_status", request_id=rid, status="propuesta_preparada")
            self.call("coordinator", "results.record", request_id=rid, kind="propuesta_preparada",
                      summary=f"Propuesta de visita {slot['slot_start']} preparada; pendiente de confirmación del cliente",
                      data={"visit_id": visit["visit_id"], "total": self.st["quote_total"], "technician_id": slot["technician_id"], "customer_confirmed": False})
            self.finish("coordinator", "Caso con propuesta preparada; falta la confirmación del cliente")
            if self.st.get("category") == "mantencion":
                return self.start_collab(slot, rid)
            self.emit("run.completed", "coordinator", "Propuesta preparada — la visita NO está confirmada hasta que el cliente la confirme", outcome="propuesta_preparada", customer_confirmed=False)
            self.o._set(self.rid, status="completed")
        else:
            self.delegate("coordinator", "admin_assistant", "El dueño rechazó el presupuesto: preparar alternativa", "to-admin-alt")
            self.call("admin_assistant", "quotes.set_status", request_id=rid, kind="principal", status="rechazado")
            alt = self.call("admin_assistant", "quotes.prepare", request_id=rid, kind="alternativa", service_codes=["DIAG"], part_skus=[])
            self.call("admin_assistant", "tasks.create", request_id=rid, title="Seguimiento: el cliente puede aceptar la alternativa de diagnóstico", assignee_agent="reception", idem_key=f"{self.rid}:followup-alt")
            self.finish("admin_assistant", f"Alternativa (solo diagnóstico) ${alt['total']:,.0f}")
            self.delegate("coordinator", "reception", "Comunicar la alternativa al cliente", "to-reception-alt")
            body = self.o.provider.draft_customer_message("alternative", {"customer": self.st["customer"]["name"], "alt": "visita de diagnóstico", "total": alt["total"]})
            self.call("reception", "messages.prepare_customer", request_id=rid, kind="alternative", body=body)
            self.finish("reception", "Alternativa preparada (no enviada)")
            self.call("coordinator", "requests.update_status", request_id=rid, status="alternativa_preparada")
            self.call("coordinator", "results.record", request_id=rid, kind="rechazado_con_alternativa",
                      summary="Presupuesto rechazado por el dueño; alternativa de diagnóstico preparada", data={"alt_total": alt["total"]})
            self.finish("coordinator", "Presupuesto rechazado; alternativa ofrecida")
            self.emit("run.completed", "coordinator", "Presupuesto rechazado — alternativa preparada para el cliente", outcome="rechazado_con_alternativa")
            self.o._set(self.rid, status="completed")

    # ================= scenario D: technician ↔ administration ↔ coordinator ↔ owner =================
    def _admin_id(self):
        """The administration employee of this run: stable once chosen; chosen among ELIGIBLE employees by workload, never «the first» (V0.5)."""
        if self.st.get("admin_id"):
            return {"id": self.st["admin_id"], "name": self.st["admin"]}
        p = pick_assignee_eng(self.eng, self.cid, "admin")
        if not p:
            raise ToolError("la empresa no tiene personal de administración con asistente activo")
        return {"id": p["id"], "name": p["name"]}

    def _mr(self):
        rows = self._db("SELECT * FROM material_requests WHERE company_id=:c AND run_id=:r ORDER BY created_at DESC LIMIT 1", r=self.rid)
        return rows[0] if rows else None

    def start_collab(self, slot, rid):
        from datetime import datetime, timedelta
        start = datetime.fromisoformat(slot["slot_start"])
        admin = self._admin_id()
        tech = self._db("SELECT id,name FROM users WHERE company_id=:c AND id=:i", i=slot["technician_id"])[0]
        c1 = self.call("coordinator", "commitments.create", request_id=rid, owner_user_id=tech["id"], title="Informar el material necesario antes de la visita",
                       due_at=(start - timedelta(hours=12)).isoformat(), origin="visita", idem_key=f"{self.rid}:c-material")
        c2 = self.call("coordinator", "commitments.create", request_id=rid, owner_user_id=admin["id"], title="Confirmar la visita con el cliente",
                       due_at=(start - timedelta(hours=24)).isoformat(), origin="visita", customer_facing=True, idem_key=f"{self.rid}:c-confirm")
        self.save(technician_id=tech["id"], technician=tech["name"], admin_id=admin["id"], admin=admin["name"], c_material=c1["commitment_id"], c_confirm=c2["commitment_id"])
        self.emit("agent.waiting", "tech_assistant", f"Esperando que {tech['name']} informe el material que necesita")
        self.emit("run.waiting_human", "coordinator", f"En pausa: {tech['name']} debe informar materiales desde «Mi asistente»", who=tech["name"], role="technician", reason="material_del_tecnico")
        self.o._set(self.rid, status="waiting_technician")

    def phase_collab_admin(self):
        rid, mr = self.st["request_id"], self._mr()
        self.activate("admin_assistant", f"Atender el pedido de material del técnico ({mr['sku']} x{mr['qty']})")
        inv = self.call("admin_assistant", "inventory.check", skus=[mr["sku"]])
        part = inv["parts"][0]
        if not part.get("exists") or part["stock"] < mr["qty"]:
            self.emit("agent.blocked", "admin_assistant", f"Stock insuficiente de {mr['sku']} (hay {part.get('stock', 0)}, se piden {mr['qty']})")
            self.call("admin_assistant", "materials.update_status", material_request_id=mr["id"], status="sin_stock")
            self.call("admin_assistant", "tasks.create", request_id=rid, title=f"Reponer {mr['sku']} (faltan {mr['qty'] - part.get('stock', 0)})", assignee_user_id=self.st["admin_id"],
                      assignee_agent="admin_assistant", kind="reposicion", idem_key=f"{self.rid}:restock-{mr['sku']}")
            self.emit("task.assigned", "admin_assistant", f"Tarea asignada a {self.st['admin']}: reponer {mr['sku']}", assignee=self.st["admin"], assignee_role="admin")
            self.delegate("admin_assistant", "coordinator", f"Sin stock de {mr['sku']} para la visita", "collab-shortage")
            alts = [{"id": "reschedule_after_restock", "label": "Reprogramar la visita hasta reponer el material"},
                    {"id": "proceed_without_material", "label": "Hacer la visita sin ese material"}]
            self.finish("coordinator", "Sin stock: alternativas propuestas")
            return self.block("coordinator", f"Sin stock de {mr['sku']} para la visita", alts, inv)
        self.delegate("admin_assistant", "coordinator", f"Material disponible; costo adicional ${mr['cost']:,.0f} requiere aprobación del dueño", "collab-cost")
        c3 = self.call("coordinator", "commitments.create", request_id=rid, owner_user_id=self.st["admin_id"], title=f"Entregar {mr['sku']} x{mr['qty']} al técnico antes de la visita",
                       due_at=self._due(hours=-6), origin="material", idem_key=f"{self.rid}:c-deliver")
        with self.eng.begin() as c:  # engine-internal link (system operation)
            q(c, "UPDATE tasks SET commitment_id=:k WHERE company_id=:c AND run_id=:r AND kind='material' AND commitment_id IS NULL", k=c3["commitment_id"], c=self.cid, r=self.rid)
        self.save(c_deliver=c3["commitment_id"], material_request_id=mr["id"])
        self.finish("admin_assistant", "Inventario verificado; solicitud de aprobación enviada")
        subject = f"Material para la visita ({mr['sku']} x{mr['qty']})"
        ap = self.call("coordinator", "approvals.request", request_id=rid, subject=subject,
                       summary=f"{self.st['customer']['name']} · material {mr['sku']} x{mr['qty']} pedido por {self.st['technician']} · motivo: {mr['reason'] or 'visita'}", amount=mr["cost"])
        self.delegate("coordinator", "owner_assistant", "Presentar el costo del material al dueño para aprobación", "to-owner-material")
        self.call("owner_assistant", "approvals.pending")
        self.emit("approval.requested", "owner_assistant", f"Esperando decisión humana: {subject} (${mr['cost']:,.0f})", approval_id=ap["approval_id"], amount=mr["cost"], subject=subject)
        self.emit("agent.waiting", "owner_assistant", "Esperando aprobación del dueño")
        self.emit("run.waiting_approval", "coordinator", "Ejecución en pausa hasta que un humano decida el gasto del material")
        self.park_for_approval(ap["approval_id"])

    def _due(self, hours):
        from datetime import datetime, timedelta
        return (datetime.fromisoformat(self.st["slot"]["slot_start"]) + timedelta(hours=hours)).isoformat()

    def phase_material_decided(self, ap):
        rid, mr = self.st["request_id"], self._mr()
        task = self._db("SELECT id FROM tasks WHERE company_id=:c AND run_id=:r AND kind='material' ORDER BY created_at DESC LIMIT 1", r=self.rid)[0]
        if ap["status"] == "approved":
            self.delegate("coordinator", "admin_assistant", "Gasto aprobado: preparar y entregar el material", "to-admin-material-ok")
            self.call("admin_assistant", "workorders.annotate", request_id=rid, status="material_pendiente_entrega", note=f"Material {mr['sku']} x{mr['qty']} aprobado; pendiente de entrega por {self.st['admin']}")
            self.emit("task.assigned", "admin_assistant", f"Tarea autorizada para {self.st['admin']}: entregar {mr['sku']} x{mr['qty']}", assignee=self.st["admin"], assignee_role="admin", task_id=task["id"])
            self.finish("admin_assistant", "Orden actualizada; entrega pendiente de evidencia")
            self.delegate("coordinator", "reception", "Preparar aviso al cliente sobre el material adicional", "to-reception-material")
            body = f"Hola {self.st['customer']['name'].split(' ')[0]}, para tu visita se incluirá material adicional ({mr['sku']} x{mr['qty']}, estimado ${mr['cost']:,.0f}). Tu visita sigue propuesta hasta que la confirmes.".replace(",", ".")
            self.call("reception", "messages.prepare_customer", request_id=rid, kind="material_update", body=body)
            self.finish("reception", "Aviso preparado (no enviado: canal simulado)")
            self.emit("agent.waiting", "admin_assistant", f"Esperando que {self.st['admin']} entregue el material y aporte evidencia")
            self.emit("run.waiting_human", "coordinator", f"En pausa: {self.st['admin']} debe entregar el material y marcar la tarea con evidencia", who=self.st["admin"], role="admin", reason="entrega_de_material")
            self.finish("coordinator", "Compromiso de entrega registrado")
            # the approval becomes effective only now, so the task cannot be completed before the run is waiting for it
            self.call("admin_assistant", "materials.update_status", material_request_id=mr["id"], status="aprobado")
            self.o._set(self.rid, status="waiting_task")
        else:
            self.call("admin_assistant", "materials.update_status", material_request_id=mr["id"], status="rechazado")
            self.call("coordinator", "tasks.cancel", task_id=task["id"], reason="Gasto de material rechazado por el dueño")
            self.call("coordinator", "commitments.update_status", commitment_id=self.st["c_deliver"], status="cancelado", evidence="gasto rechazado")
            self.call("coordinator", "tasks.create", request_id=rid, title="El dueño rechazó el material: informa alternativa o procede sin él", assignee_user_id=self.st["technician_id"],
                      assignee_agent="tech_assistant", kind="aviso", idem_key=f"{self.rid}:material-rejected")
            self.emit("task.assigned", "coordinator", f"Aviso para {self.st['technician']}: material rechazado", assignee=self.st["technician"], assignee_role="technician")
            self.call("coordinator", "results.record", request_id=rid, kind="material_rechazado", summary="Gasto de material rechazado por el dueño; técnico avisado", data={"sku": mr["sku"]})
            self.finish("coordinator", "Material rechazado; sin cambios en inventario")
            self.emit("run.completed", "coordinator", "Material rechazado — no se movió inventario ni se envió nada", outcome="material_rechazado")
            self.o._set(self.rid, status="completed")

    def collab_resolve(self, choice):
        rid = self.st["request_id"]
        self.activate("coordinator", f"Decisión humana sobre el faltante: {choice}")
        mr = self._mr()
        if choice == "proceed_without_material":
            self.call("coordinator", "materials.update_status", material_request_id=mr["id"], status="cancelado")
            self.call("coordinator", "commitments.update_status", commitment_id=self.st["c_material"], status="cumplido", evidence="Se acordó realizar la visita sin material adicional")
            kind, summary = "visita_sin_material", "Se acordó hacer la visita sin el material pedido"
        else:
            self.call("coordinator", "materials.update_status", material_request_id=mr["id"], status="cancelado")
            self.call("coordinator", "commitments.update_status", commitment_id=self.st["c_material"], status="cancelado", evidence="Visita a reprogramar")
            self.call("coordinator", "commitments.update_status", commitment_id=self.st["c_confirm"], status="cancelado", evidence="Visita a reprogramar")
            self.call("coordinator", "tasks.create", request_id=rid, title="Reprogramar la visita con el cliente tras reponer stock", assignee_user_id=self.st["admin_id"], assignee_agent="admin_assistant",
                      kind="reprogramar", idem_key=f"{self.rid}:reschedule")
            self.emit("task.assigned", "coordinator", f"Tarea para {self.st['admin']}: reprogramar la visita", assignee=self.st["admin"], assignee_role="admin")
            kind, summary = "reprogramacion_pendiente", "Visita por reprogramar hasta reponer stock"
        self.call("coordinator", "results.record", request_id=rid, kind=kind, summary=summary, data={"sku": mr["sku"]})
        self.finish("coordinator", summary)
        self.emit("run.completed", "coordinator", summary, outcome=kind)
        self.o._set(self.rid, status="completed")

    def phase_after_material(self):
        """Admin delivered with evidence → the technician must confirm reception (person-to-person handoff)."""
        rid, mr = self.st["request_id"], self._mr()
        self.activate("admin_assistant", "Entrega registrada con evidencia; avisar al técnico")
        self.call("admin_assistant", "workorders.annotate", request_id=rid, status="material_listo", note=f"Material entregado por {self.st['admin']}; falta confirmación del técnico")
        self.delegate("admin_assistant", "tech_assistant", "Material entregado: confirmar recepción", "to-tech-recv")
        self.emit("agent.waiting", "tech_assistant", f"Esperando que {self.st['technician']} confirme la recepción")
        self.emit("run.waiting_human", "coordinator", f"En pausa: {self.st['technician']} debe confirmar la recepción", who=self.st["technician"], role="technician", reason="recepcion_de_material")
        t = self.call("admin_assistant", "tasks.create", request_id=rid, title=f"Confirmar recepción del material {mr['sku']} x{mr['qty']}", assignee_user_id=self.st["technician_id"],
                      assignee_agent="tech_assistant", kind="recepcion", commitment_id=self.st["c_material"], idem_key=f"{self.rid}:recv")
        self.emit("task.assigned", "admin_assistant", f"Tarea asignada a {self.st['technician']}: confirmar recepción", assignee=self.st["technician"], assignee_role="technician", task_id=t["task_id"])
        self.o._set(self.rid, status="waiting_task")

    def phase_collab_close(self):
        rid, mr = self.st["request_id"], self._mr()
        self.activate("coordinator", "Recepción confirmada por el técnico: cerrar el caso")
        self.call("coordinator", "results.record", request_id=rid, kind="material_entregado_y_recibido",
                  summary=f"Material {mr['sku']} x{mr['qty']} aprobado, entregado por {self.st['admin']} y recibido por {self.st['technician']}", data={"sku": mr["sku"], "qty": mr["qty"], "cost": mr["cost"]})
        self.finish("tech_assistant", "Recepción confirmada")
        self.finish("coordinator", "Colaboración cerrada con evidencia en cada paso")
        self.emit("run.completed", "coordinator", "Material entregado y recibido — cada paso con evidencia; la visita sigue sin confirmar hasta que el cliente la confirme",
                  outcome="material_entregado_y_recibido", customer_confirmed=False)
        self.o._set(self.rid, status="completed")
