"""V0.3 tools: commitments, reminders/follow-up drafts, material collaboration and evidence-gated task completion.
Every effect is a persisted row guarded by a unique key or a compare-and-set, so repeated calls never duplicate or re-apply."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from ..db import all_, jload, now, one, q, uid
from .registry import Strict, ToolCtx, ToolError, tool
from .definitions import NoArgs, RequestId, _req


def _iso(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _user(ctx: ToolCtx, uid_: str, role: str | None = None) -> dict:
    sql, p = "SELECT id,name,role FROM users WHERE id=:i AND company_id=:c", {"i": uid_, "c": ctx.company_id}
    u = one(ctx.conn, sql, **p)
    if not u or (role and u["role"] != role):
        raise ToolError("usuario no encontrado en esta empresa")
    return u


def _cancel_side_effects(ctx: ToolCtx, commitment_id: str, *, drafts: bool) -> dict:
    """Closing/reassigning a commitment cancels reminders that no longer apply (and, on close, customer drafts)."""
    r = q(ctx.conn, "UPDATE tasks SET status='cancelada' WHERE company_id=:c AND commitment_id=:k AND kind='recordatorio' AND status='pendiente'", c=ctx.company_id, k=commitment_id).rowcount
    d = 0
    if drafts:
        d = q(ctx.conn, "UPDATE messages SET status='cancelado' WHERE company_id=:c AND commitment_id=:k AND status IN ('preparado_no_enviado','requiere_aprobacion')", c=ctx.company_id, k=commitment_id).rowcount
    return {"reminders_cancelled": r, "drafts_cancelled": d}


class CreateCommitment(Strict):
    request_id: str
    owner_user_id: str
    title: str = Field(min_length=3, max_length=200)
    due_at: str = Field(max_length=40)
    origin: str = Field(default="visita", max_length=40)
    customer_facing: bool = False
    idem_key: str = Field(min_length=3, max_length=120)


@tool("commitments.create", "commitments.write", CreateCommitment, access="write", description="Registra un compromiso con responsable y plazo (idempotente).")
def commitments_create(ctx, a):
    _req(ctx, a.request_id)
    owner = _user(ctx, a.owner_user_id)
    try:
        _iso(a.due_at)
    except ValueError:
        raise ToolError("fecha límite inválida")
    ex = one(ctx.conn, "SELECT id FROM commitments WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=a.idem_key)
    if ex:
        return {"commitment_id": ex["id"], "deduplicated": True, "_summary": "compromiso ya existía", "_resource": ex["id"]}
    cid, t = uid("cmt_"), now()
    q(ctx.conn, "INSERT INTO commitments(id,company_id,request_id,run_id,owner_user_id,title,due_at,status,origin,customer_facing,reminder_sent,idem_key,created_at,updated_at) VALUES(:i,:c,:r,:ru,:o,:ti,:d,'abierto',:og,:cf,0,:k,:t,:t)",
      i=cid, c=ctx.company_id, r=a.request_id, ru=ctx.run_id, o=owner["id"], ti=a.title, d=a.due_at, og=a.origin, cf=int(a.customer_facing), k=a.idem_key, t=t)
    ctx.event("commitment.created", f"Compromiso: {a.title} → {owner['name']} (plazo {a.due_at[:16]})", {"commitment_id": cid, "owner": owner["name"], "due_at": a.due_at, "origin": a.origin})
    return {"commitment_id": cid, "owner": owner["name"], "deduplicated": False, "_summary": f"compromiso «{a.title}» → {owner['name']}", "_resource": cid}


class ListCommitments(Strict):
    status: str | None = None


@tool("commitments.list", "commitments.read", ListCommitments, description="Compromisos de la empresa (el técnico solo ve los suyos).")
def commitments_list(ctx, a):
    sql = """SELECT m.id,m.request_id,m.title,m.due_at,m.status,m.origin,m.last_evidence,m.customer_facing,u.name AS owner,m.owner_user_id
             FROM commitments m JOIN users u ON u.id=m.owner_user_id WHERE m.company_id=:c"""
    p = {"c": ctx.company_id}
    if ctx.principal.user_role == "technician":
        sql += " AND m.owner_user_id=:u"
        p["u"] = ctx.principal.user_id
    if a.status:
        sql += " AND m.status=:s"
        p["s"] = a.status
    rows = all_(ctx.conn, sql + " ORDER BY m.due_at", **p)
    n = now()
    for r in rows:
        r["overdue"] = r["status"] in ("abierto", "vencido") and _iso(r["due_at"]) <= _iso(n)
    return {"commitments": rows, "_summary": f"{len(rows)} compromiso(s)"}


class CommitmentStatus(Strict):
    commitment_id: str
    status: Literal["cumplido", "cancelado"]
    evidence: str = Field(default="", max_length=500)


@tool("commitments.update_status", "commitments.write", CommitmentStatus, access="write", description="Cierra un compromiso y cancela recordatorios/borradores inaplicables.")
def commitments_update_status(ctx, a):
    m = one(ctx.conn, "SELECT * FROM commitments WHERE id=:i AND company_id=:c", i=a.commitment_id, c=ctx.company_id)
    if not m:
        raise ToolError("compromiso no encontrado")
    if m["status"] == a.status:
        return {"commitment_id": m["id"], "deduplicated": True, "_summary": f"ya estaba {a.status}", "_resource": m["id"]}
    if m["status"] in ("cumplido", "cancelado"):
        raise ToolError(f"el compromiso ya está {m['status']}")
    q(ctx.conn, "UPDATE commitments SET status=:s,last_evidence=:e,updated_at=:t WHERE id=:i AND company_id=:c", s=a.status, e=a.evidence or None, t=now(), i=m["id"], c=ctx.company_id)
    eff = _cancel_side_effects(ctx, m["id"], drafts=True)
    ctx.event("commitment.closed", f"Compromiso {a.status}: {m['title']}", {"commitment_id": m["id"], "status": a.status, "evidence": a.evidence, **eff})
    return {"commitment_id": m["id"], "deduplicated": False, **eff, "_summary": f"compromiso {a.status}; {eff['reminders_cancelled']} recordatorio(s) y {eff['drafts_cancelled']} borrador(es) cancelados", "_resource": m["id"]}


class Reassign(Strict):
    commitment_id: str
    new_owner_user_id: str


@tool("commitments.reassign", "commitments.write", Reassign, access="write", description="Reasigna un compromiso; cancela los recordatorios pendientes del responsable anterior.")
def commitments_reassign(ctx, a):
    m = one(ctx.conn, "SELECT * FROM commitments WHERE id=:i AND company_id=:c", i=a.commitment_id, c=ctx.company_id)
    if not m:
        raise ToolError("compromiso no encontrado")
    new = _user(ctx, a.new_owner_user_id)
    if m["status"] in ("cumplido", "cancelado"):
        raise ToolError(f"el compromiso ya está {m['status']}")
    if m["owner_user_id"] == new["id"]:
        return {"commitment_id": m["id"], "deduplicated": True, "_summary": "ya pertenece a ese responsable", "_resource": m["id"]}
    q(ctx.conn, "UPDATE commitments SET owner_user_id=:o,reminder_sent=0,status='abierto',updated_at=:t WHERE id=:i AND company_id=:c", o=new["id"], t=now(), i=m["id"], c=ctx.company_id)
    eff = _cancel_side_effects(ctx, m["id"], drafts=False)
    ctx.event("commitment.reassigned", f"Compromiso reasignado a {new['name']}: {m['title']}", {"commitment_id": m["id"], "new_owner": new["name"], **eff})
    return {"commitment_id": m["id"], "deduplicated": False, **eff, "_summary": f"reasignado a {new['name']}; {eff['reminders_cancelled']} recordatorio(s) cancelados", "_resource": m["id"]}


class ProcessDue(Strict):
    as_of: str | None = Field(default=None, max_length=40)


@tool("commitments.process_due", "commitments.process", ProcessDue, access="write", mode="deterministic",
      description="Procesa compromisos vencidos: recordatorio interno + borrador de seguimiento (NO se envía). Reentrante e idempotente.")
def commitments_process_due(ctx, a):
    as_of = _iso(a.as_of) if a.as_of else _iso(now())
    due = [m for m in all_(ctx.conn, "SELECT * FROM commitments WHERE company_id=:c AND status IN ('abierto','vencido') AND reminder_sent=0", c=ctx.company_id) if _iso(m["due_at"]) <= as_of]
    done = []
    for m in due:
        if q(ctx.conn, "UPDATE commitments SET status='vencido',reminder_sent=1,updated_at=:t WHERE id=:i AND company_id=:c AND reminder_sent=0", t=now(), i=m["id"], c=ctx.company_id).rowcount != 1:
            continue  # someone else processed it concurrently
        tid = uid("tsk_")
        nth = one(ctx.conn, "SELECT COUNT(*) AS n FROM tasks WHERE company_id=:c AND commitment_id=:k AND kind='recordatorio'", c=ctx.company_id, k=m["id"])["n"] + 1  # reassignments regenerate with a fresh key
        q(ctx.conn, "INSERT INTO tasks(id,company_id,request_id,run_id,title,assignee_user_id,status,idem_key,kind,commitment_id,created_at) VALUES(:i,:c,:r,:ru,:ti,:u,'pendiente',:k,'recordatorio',:cm,:t)",
          i=tid, c=ctx.company_id, r=m["request_id"], ru=m["run_id"], ti=f"Recordatorio: venció «{m['title']}»", u=m["owner_user_id"], k=f"rem:{m['id']}:{nth}", cm=m["id"], t=now())
        ctx.event("reminder.created", f"Recordatorio interno por vencimiento: {m['title']}", {"commitment_id": m["id"], "task_id": tid}, run_id=m["run_id"], agent="coordinator", mode="deterministic")
        draft = None
        if m["customer_facing"]:
            v = one(ctx.conn, "SELECT customer_confirmed FROM visits WHERE company_id=:c AND request_id=:r", c=ctx.company_id, r=m["request_id"])
            req = one(ctx.conn, "SELECT conversation_id,customer_id FROM requests WHERE id=:i AND company_id=:c", i=m["request_id"], c=ctx.company_id)
            already = one(ctx.conn, "SELECT 1 AS x FROM messages WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=f"fu:{m['id']}")  # never a second follow-up for the same commitment
            if v and not v["customer_confirmed"] and req and req["conversation_id"] and not already:
                cu = one(ctx.conn, "SELECT name FROM customers WHERE id=:i AND company_id=:c", i=req["customer_id"], c=ctx.company_id)
                draft = uid("msg_")
                q(ctx.conn, "INSERT INTO messages(id,company_id,conversation_id,sender,body,status,idem_key,commitment_id,created_at) VALUES(:i,:c,:cv,'agent:reception',:b,'preparado_no_enviado',:k,:cm,:t)",
                  i=draft, c=ctx.company_id, cv=req["conversation_id"], k=f"fu:{m['id']}", cm=m["id"], t=now(),
                  b=f"Hola {cu['name'].split(' ')[0]}, te recordamos que tu visita está propuesta pero aún no confirmada. Responde CONFIRMO con tu código para reservarla.")
                ctx.event("followup.drafted", "Seguimiento al cliente PREPARADO (no enviado)", {"message_id": draft, "status": "preparado_no_enviado"}, run_id=m["run_id"], agent="reception", mode="simulated")
        done.append({"commitment_id": m["id"], "reminder_task": tid, "followup_draft": draft})
    return {"processed": done, "as_of": as_of.isoformat(), "_summary": f"{len(done)} compromiso(s) vencido(s) procesado(s)"}


# ---------- material collaboration ----------
class RequestMaterial(Strict):
    request_id: str
    sku: str = Field(max_length=40)
    qty: int = Field(default=1, ge=1, le=10)
    reason: str = Field(default="", max_length=200)


@tool("collab.request_material", "materials.request", RequestMaterial, access="write",
      description="El técnico pide material para una visita: crea solicitud, delegación técnico→administración y tarea asignada a una persona de administración.")
def collab_request_material(ctx, a):
    if not ctx.run_id:
        raise ToolError("la solicitud no tiene una ejecución asociada")
    key = f"collab:{a.request_id}:{a.sku}"
    ex = one(ctx.conn, "SELECT id FROM material_requests WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=key)
    if ex:
        return {"material_request_id": ex["id"], "deduplicated": True, "_summary": "solicitud de material ya registrada", "_resource": ex["id"]}
    run = one(ctx.conn, "SELECT status FROM runs WHERE id=:i AND company_id=:c", i=ctx.run_id, c=ctx.company_id)
    if not run or run["status"] != "waiting_technician":
        raise ToolError("esta ejecución no está esperando material del técnico")
    r = _req(ctx, a.request_id)
    if not one(ctx.conn, "SELECT 1 AS x FROM work_orders WHERE company_id=:c AND request_id=:r AND technician_id=:u", c=ctx.company_id, r=r["id"], u=ctx.principal.user_id):
        raise ToolError("orden no asignada a este técnico")
    part = one(ctx.conn, "SELECT sku,name,unit_price FROM parts WHERE company_id=:c AND sku=:s", c=ctx.company_id, s=a.sku)
    if not part:
        raise ToolError("repuesto inexistente en el catálogo")
    from ..directory import is_eligible, pick_assignee
    st = jload(one(ctx.conn, "SELECT state FROM runs WHERE id=:i AND company_id=:c", i=ctx.run_id, c=ctx.company_id)["state"], {})
    admin = one(ctx.conn, "SELECT id,name FROM users WHERE id=:i AND company_id=:c", i=st.get("admin_id"), c=ctx.company_id) if st.get("admin_id") and is_eligible(ctx.conn, ctx.company_id, st["admin_id"], "admin") else pick_assignee(ctx.conn, ctx.company_id, "admin")
    if not admin:
        raise ToolError("la empresa no tiene personal de administración con asistente activo")
    cust = one(ctx.conn, "SELECT name FROM customers WHERE id=:i AND company_id=:c", i=r["customer_id"], c=ctx.company_id)
    mid, tid, did, t = uid("mat_"), uid("tsk_"), uid("del_"), now()
    cost = part["unit_price"] * a.qty
    q(ctx.conn, "INSERT INTO material_requests(id,company_id,request_id,sku,qty,status,requested_by,idem_key,created_at,run_id,cost,reason) VALUES(:i,:c,:r,:s,:q,'solicitado',:b,:k,:t,:ru,:co,:re)",
      i=mid, c=ctx.company_id, r=r["id"], s=a.sku, q=a.qty, b=ctx.principal.user_id, k=key, t=t, ru=ctx.run_id, co=cost, re=a.reason)
    q(ctx.conn, "INSERT INTO delegations(id,company_id,run_id,from_agent,to_agent,task,status,idem_key,created_at) VALUES(:i,:c,:ru,'tech_assistant','admin_assistant',:ta,'enviada',:k,:t)",
      i=did, c=ctx.company_id, ru=ctx.run_id, ta=f"Material {a.sku} x{a.qty} para {cust['name']}", k=f"{key}:deleg", t=t)
    q(ctx.conn, "INSERT INTO tasks(id,company_id,request_id,run_id,title,assignee_user_id,assignee_agent,status,idem_key,kind,created_at) VALUES(:i,:c,:r,:ru,:ti,:u,'admin_assistant','pendiente',:k,'material',:t)",
      i=tid, c=ctx.company_id, r=r["id"], ru=ctx.run_id, ti=f"Preparar y entregar {part['name']} x{a.qty} al técnico (visita de {cust['name']})", u=admin["id"], k=f"{key}:task", t=t)
    ctx.event("agent.delegated", f"tech_assistant → admin_assistant: material {a.sku} x{a.qty}", {"to": "admin_assistant", "task": f"Material {a.sku} x{a.qty}", "delegation_id": did, "reason": a.reason}, agent="tech_assistant")
    ctx.event("task.assigned", f"Tarea asignada a {admin['name']}: preparar {a.sku} x{a.qty}", {"task_id": tid, "assignee": admin["name"], "assignee_role": "admin"}, agent="tech_assistant")
    return {"material_request_id": mid, "task_id": tid, "assignee": admin["name"], "cost": cost, "deduplicated": False,
            "_summary": f"material {a.sku} x{a.qty} solicitado; tarea asignada a {admin['name']}", "_resource": mid}


class MatStatus(Strict):
    material_request_id: str
    status: Literal["sin_stock", "aprobado", "rechazado", "cancelado"]


@tool("materials.update_status", "materials.manage", MatStatus, access="write", description="Transición de una solicitud de material (aprobación/sin stock/rechazo). Idempotente.")
def materials_update_status(ctx, a):
    m = one(ctx.conn, "SELECT * FROM material_requests WHERE id=:i AND company_id=:c", i=a.material_request_id, c=ctx.company_id)
    if not m:
        raise ToolError("solicitud de material no encontrada")
    if m["status"] == a.status:
        return {"status": a.status, "deduplicated": True, "_summary": f"ya estaba {a.status}", "_resource": m["id"]}
    if m["status"] in ("entregado", "recibido", "rechazado", "cancelado"):
        raise ToolError(f"no se puede pasar de {m['status']} a {a.status}")
    q(ctx.conn, "UPDATE material_requests SET status=:s WHERE id=:i AND company_id=:c", s=a.status, i=m["id"], c=ctx.company_id)
    return {"status": a.status, "deduplicated": False, "_summary": f"material → {a.status}", "_resource": m["id"]}


class Annotate(Strict):
    request_id: str
    status: Literal["material_pendiente_entrega", "material_listo", "programada"]
    note: str = Field(max_length=300)


@tool("workorders.annotate", "workorders.write", Annotate, access="write", description="Actualiza estado y nota de la orden de trabajo.")
def workorders_annotate(ctx, a):
    w = one(ctx.conn, "SELECT id FROM work_orders WHERE company_id=:c AND request_id=:r", c=ctx.company_id, r=a.request_id)
    if not w:
        raise ToolError("orden no encontrada")
    q(ctx.conn, "UPDATE work_orders SET status=:s,notes=:n,updated_at=:t WHERE id=:i AND company_id=:c", s=a.status, n=a.note, t=now(), i=w["id"], c=ctx.company_id)
    return {"status": a.status, "_summary": f"orden → {a.status}", "_resource": w["id"]}


class CompleteTask(Strict):
    task_id: str
    evidence: str = Field(min_length=10, max_length=500)


@tool("tasks.complete", "tasks.complete", CompleteTask, access="write",
      description="Marca una tarea PROPIA como hecha. Exige evidencia y, para tareas de material, aprobación económica vigente; aplica el efecto real (stock/estado) una sola vez.")
def tasks_complete(ctx, a):
    t = one(ctx.conn, "SELECT * FROM tasks WHERE id=:i AND company_id=:c", i=a.task_id, c=ctx.company_id)
    if not t:
        raise ToolError("tarea no encontrada")
    if t["assignee_user_id"] != ctx.principal.user_id:
        raise ToolError("la tarea no está asignada a este usuario")
    if t["status"] == "hecha":
        return {"task_id": t["id"], "kind": t["kind"], "run_id": t["run_id"], "deduplicated": True, "_summary": "tarea ya estaba hecha (sin cambios)", "_resource": t["id"]}
    if t["status"] != "pendiente":
        raise ToolError(f"la tarea está {t['status']}")
    if (t["idem_key"] or "").endswith(":await-customer"):
        raise ToolError("esta tarea espera la confirmación del cliente: no se cierra desde el equipo")
    if (t["idem_key"] or "").endswith(":prep-visit"):
        v = one(ctx.conn, "SELECT customer_confirmed FROM visits WHERE company_id=:c AND request_id=:r", c=ctx.company_id, r=t["request_id"])
        if not v or not v["customer_confirmed"]:
            raise ToolError("la visita aún no fue confirmada por el cliente: no se puede dar por preparada")
    mr = one(ctx.conn, "SELECT * FROM material_requests WHERE company_id=:c AND request_id=:r AND run_id=:ru ORDER BY created_at DESC LIMIT 1", c=ctx.company_id, r=t["request_id"], ru=t["run_id"]) if t["kind"] in ("material", "recepcion") else None
    effect = None
    if t["kind"] == "material":
        if not mr or mr["status"] != "aprobado":
            raise ToolError("no hay aprobación económica vigente del material: no se puede marcar como resuelta")
        p = one(ctx.conn, "SELECT stock FROM parts WHERE company_id=:c AND sku=:s", c=ctx.company_id, s=mr["sku"])
        if not p or p["stock"] < mr["qty"]:
            raise ToolError("stock insuficiente para entregar: no se puede marcar como resuelta")
    if t["kind"] == "recepcion" and (not mr or mr["status"] != "entregado"):
        raise ToolError("el material aún no fue entregado por administración")
    if q(ctx.conn, "UPDATE tasks SET status='hecha',evidence=:e,completed_by=:u,completed_at=:n WHERE id=:i AND company_id=:c AND status='pendiente'",
         e=a.evidence, u=ctx.principal.user_id, n=now(), i=t["id"], c=ctx.company_id).rowcount != 1:
        return {"task_id": t["id"], "kind": t["kind"], "run_id": t["run_id"], "deduplicated": True, "_summary": "tarea ya completada por otra operación", "_resource": t["id"]}
    if t["kind"] == "material":
        q(ctx.conn, "UPDATE parts SET stock=stock-:n WHERE company_id=:c AND sku=:s", n=mr["qty"], c=ctx.company_id, s=mr["sku"])
        q(ctx.conn, "UPDATE material_requests SET status='entregado' WHERE id=:i AND company_id=:c", i=mr["id"], c=ctx.company_id)
        effect = f"stock {mr['sku']} −{mr['qty']}; material entregado"
    elif t["kind"] == "recepcion":
        q(ctx.conn, "UPDATE material_requests SET status='recibido' WHERE id=:i AND company_id=:c", i=mr["id"], c=ctx.company_id)
        effect = "material recibido por el técnico"
    if t["commitment_id"]:
        q(ctx.conn, "UPDATE commitments SET status='cumplido',last_evidence=:e,updated_at=:n WHERE id=:i AND company_id=:c AND status IN ('abierto','vencido')", e=a.evidence, n=now(), i=t["commitment_id"], c=ctx.company_id)
        _cancel_side_effects(ctx, t["commitment_id"], drafts=True)
    ctx.event("task.completed", f"Tarea completada con evidencia: {t['title']}", {"task_id": t["id"], "evidence": a.evidence, "effect": effect, "by": ctx.principal.name})
    return {"task_id": t["id"], "kind": t["kind"], "run_id": t["run_id"], "effect": effect, "deduplicated": False, "_summary": f"tarea hecha{' · ' + effect if effect else ''}", "_resource": t["id"]}


class CancelTask(Strict):
    task_id: str
    reason: str = Field(max_length=200)


@tool("tasks.cancel", "tasks.write", CancelTask, access="write", description="Cancela una tarea pendiente (p. ej. material rechazado).")
def tasks_cancel(ctx, a):
    t = one(ctx.conn, "SELECT status FROM tasks WHERE id=:i AND company_id=:c", i=a.task_id, c=ctx.company_id)
    if not t:
        raise ToolError("tarea no encontrada")
    if t["status"] == "cancelada":
        return {"deduplicated": True, "_summary": "tarea ya cancelada"}
    if t["status"] != "pendiente":
        raise ToolError(f"la tarea está {t['status']}")
    q(ctx.conn, "UPDATE tasks SET status='cancelada',evidence=:e WHERE id=:i AND company_id=:c AND status='pendiente'", e=a.reason, i=a.task_id, c=ctx.company_id)
    return {"deduplicated": False, "_summary": f"tarea cancelada: {a.reason}", "_resource": a.task_id}
