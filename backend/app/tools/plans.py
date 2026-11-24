"""V0.4 tools for the conversational coordinator (WorkOrderPlan).

Design rules (Issue #2):
- The LLM never reaches these tools. Only the engine's coordinator principal does, and every write tool takes ONLY ids
  (plan_id + step seq): the effect is read back from the persisted, validated and human-CONFIRMED plan, never from free args.
- Each write is idempotent (unique idempotency key / compare-and-set), so a double click, a retry or a restart never duplicates work.
- The coordinator holds `plans.assign/verify/finish` as an AGENT permission; it does not inherit the owner's permissions
  (it cannot approve spending, complete anyone's task or decide anything human-only).
"""
from __future__ import annotations

from pydantic import Field

from ..db import all_, jdump, jload, now, one, q, uid
from .registry import Strict, ToolCtx, ToolError, tool

CLOSED_REQUEST = ("cerrada", "ejecucion_fallida", "presupuesto_rechazado")
TASK_ACTIONS = {"ask_admin_verify_materials": ("admin", "admin_assistant"), "ask_technician_confirm": ("technician", "tech_assistant")}


def _plan(ctx: ToolCtx, plan_id: str) -> dict:
    p = one(ctx.conn, "SELECT * FROM plans WHERE id=:i AND company_id=:c", i=plan_id, c=ctx.company_id)
    if not p:
        raise ToolError("plan no encontrado")  # same answer for 'missing' and 'other tenant'
    return p


def _step(ctx: ToolCtx, plan_id: str, seq: int) -> dict:
    s = one(ctx.conn, "SELECT * FROM plan_steps WHERE plan_id=:p AND seq=:s AND company_id=:c", p=plan_id, s=seq, c=ctx.company_id)
    if not s:
        raise ToolError("paso no encontrado")
    return s


def materials_snapshot(conn, company_id: str, request_id: str) -> dict:
    """Real stock picture for ONE request: parts in its quotes + material requests, compared against current stock."""
    need: dict[str, int] = {}
    for qt in all_(conn, "SELECT lines FROM quotes WHERE company_id=:c AND request_id=:r", c=company_id, r=request_id):
        for ln in jload(qt["lines"], []):
            if ln.get("type") == "repuesto":
                need[ln["code"]] = max(need.get(ln["code"], 0), int(ln.get("qty", 1)))
    for m in all_(conn, "SELECT sku,qty FROM material_requests WHERE company_id=:c AND request_id=:r AND status IN ('solicitado','aprobado','sin_stock')", c=company_id, r=request_id):
        need[m["sku"]] = max(need.get(m["sku"], 0), int(m["qty"]))
    rows, short = [], []
    for sku, qty in sorted(need.items()):
        p = one(conn, "SELECT name,stock FROM parts WHERE company_id=:c AND sku=:s", c=company_id, s=sku)
        stock = p["stock"] if p else 0
        rows.append({"sku": sku, "name": p["name"] if p else sku, "needed": qty, "stock": stock})
        if stock < qty:
            short.append({"sku": sku, "needed": qty, "stock": stock})
    return {"parts": rows, "shortages": short}


class Lookup(Strict):
    pass


@tool("plans.lookup", "plans.read", Lookup, description="Datos reales de la empresa para planificar: clientes, solicitudes abiertas (con técnico y visita) y personal asignable.")
def plans_lookup(ctx, a):
    customers = all_(ctx.conn, "SELECT id,name FROM customers WHERE company_id=:c ORDER BY name LIMIT 100", c=ctx.company_id)
    marks = ",".join(f"'{s}'" for s in CLOSED_REQUEST)
    reqs = all_(ctx.conn, f"""SELECT r.id,r.customer_id,cu.name AS customer,r.category,r.status,substr(r.description,1,80) AS description,
                               w.technician_id,v.status AS visit_status,v.slot_start
                               FROM requests r JOIN customers cu ON cu.id=r.customer_id AND cu.company_id=r.company_id
                               LEFT JOIN work_orders w ON w.request_id=r.id AND w.company_id=r.company_id
                               LEFT JOIN visits v ON v.request_id=r.id AND v.company_id=r.company_id
                               WHERE r.company_id=:c AND r.status NOT IN ({marks}) ORDER BY r.created_at DESC LIMIT 30""", c=ctx.company_id)
    from ..directory import eligible_staff, ineligible_staff
    staff = [{"id": u["id"], "name": u["name"], "role": u["role"], "workload": u["workload"]} for r in ("admin", "technician") for u in eligible_staff(ctx.conn, ctx.company_id, r)]   # ELIGIBLE only
    unassigned = [{"id": u["id"], "name": u["name"], "role": r, "reason": u["reason"]} for r in ("admin", "technician") for u in ineligible_staff(ctx.conn, ctx.company_id, r)]
    return {"customers": customers, "open_requests": reqs, "staff": staff, "unassigned": unassigned,
            "_summary": f"{len(reqs)} solicitud(es) abierta(s), {len(staff)} persona(s) asignable(s)" + (f", {len(unassigned)} sin asistente activo" if unassigned else "")}


class Review(Strict):
    request_id: str


@tool("plans.review", "plans.read", Review, description="Revisa una solicitud: cliente, visita, orden, técnico, materiales y stock actual (solo lectura).")
def plans_review(ctx, a):
    r = one(ctx.conn, "SELECT * FROM requests WHERE id=:i AND company_id=:c", i=a.request_id, c=ctx.company_id)
    if not r:
        raise ToolError("solicitud no encontrada")
    cu = one(ctx.conn, "SELECT name FROM customers WHERE id=:i AND company_id=:c", i=r["customer_id"], c=ctx.company_id)
    v = one(ctx.conn, "SELECT status,slot_start,customer_confirmed FROM visits WHERE company_id=:c AND request_id=:r", c=ctx.company_id, r=r["id"])
    w = one(ctx.conn, "SELECT w.status,u.name AS technician FROM work_orders w LEFT JOIN users u ON u.id=w.technician_id WHERE w.company_id=:c AND w.request_id=:r", c=ctx.company_id, r=r["id"])
    snap = materials_snapshot(ctx.conn, ctx.company_id, r["id"])
    return {"request_id": r["id"], "customer": cu["name"] if cu else "?", "category": r["category"], "status": r["status"], "visit": v, "work_order": w, **snap,
            "_summary": f"{cu['name'] if cu else '?'} · {r['status']} · {len(snap['shortages'])} faltante(s)"}


class PlanStep(Strict):
    plan_id: str
    seq: int = Field(ge=1, le=6)


def _require_confirmed(p: dict) -> None:
    if p["status"] not in ("running", "waiting_human") or not p["confirmed_by"]:
        raise ToolError("el plan no está confirmado por una persona o ya terminó: no se ejecuta nada")


@tool("plans.assign_task", "plans.assign", PlanStep, access="write",
      description="Crea la tarea interna + delegación de UN paso de un plan YA confirmado (solo ids; idempotente). Si depende de otro paso, queda en espera.")
def plans_assign_task(ctx, a):
    p = _plan(ctx, a.plan_id)
    _require_confirmed(p)
    s = _step(ctx, a.plan_id, a.seq)
    if s["action"] not in TASK_ACTIONS:
        raise ToolError("el paso no es una tarea asignable")
    role, assistant = TASK_ACTIONS[s["action"]]
    key = f"plan:{p['id']}:{s['seq']}"
    ex = one(ctx.conn, "SELECT id,status FROM tasks WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=key)
    if ex:  # crash recovery: the task exists but the step may not know it yet → link it (never create a second one)
        q(ctx.conn, "UPDATE plan_steps SET status=:st,task_id=:t,updated_at=:n WHERE id=:i AND company_id=:c AND status='planned'",
          st="waiting_dependency" if ex["status"] == "en_espera" else "waiting_human", t=ex["id"], n=now(), i=s["id"], c=ctx.company_id)
        return {"task_id": ex["id"], "deduplicated": True, "queued": ex["status"] == "en_espera", "assignee_role": role, "_summary": "tarea del plan ya existía (sin duplicar)", "_resource": ex["id"]}
    u = one(ctx.conn, "SELECT id,name,role FROM users WHERE id=:i AND company_id=:c", i=s["assignee_user_id"], c=ctx.company_id)
    from ..directory import is_eligible
    if not u or u["role"] != role or not is_eligible(ctx.conn, ctx.company_id, u["id"], role):
        raise ToolError("el responsable validado ya no es elegible (desactivado, sin asistente activo o con otro rol): no se crea la tarea")
    dep = one(ctx.conn, "SELECT status FROM plan_steps WHERE plan_id=:p AND seq=:s", p=p["id"], s=s["depends_on"]) if s["depends_on"] else None
    queued = bool(dep and dep["status"] != "done")
    args = jload(s["args"], {})
    tid, did, t = uid("tsk_"), uid("del_"), now()
    q(ctx.conn, "INSERT INTO tasks(id,company_id,request_id,run_id,title,assignee_user_id,assignee_agent,status,idem_key,kind,created_at) VALUES(:i,:c,:r,:ru,:ti,:u,:ag,:st,:k,'plan_step',:t)",
      i=tid, c=ctx.company_id, r=p["request_id"], ru=p["id"], ti=s["title"], u=u["id"], ag=assistant, st="en_espera" if queued else "pendiente", k=key, t=t)
    q(ctx.conn, "INSERT INTO delegations(id,company_id,run_id,from_agent,to_agent,task,status,idem_key,created_at) VALUES(:i,:c,:ru,'coordinator',:to,:ta,'enviada',:k,:t)",
      i=did, c=ctx.company_id, ru=p["id"], to=assistant, ta=s["title"][:300], k=f"{key}:deleg", t=t)
    n = q(ctx.conn, "UPDATE plan_steps SET status=:st,task_id=:t,updated_at=:n WHERE id=:i AND company_id=:c AND status='planned'",
          st="waiting_dependency" if queued else "waiting_human", t=tid, n=t, i=s["id"], c=ctx.company_id).rowcount
    if n != 1:
        raise ToolError("el paso ya no estaba pendiente de asignación")
    ctx.event("agent.delegated", f"coordinator → {assistant}: {s['title'][:80]}", {"to": assistant, "task": s["title"], "delegation_id": did, "plan_step": s["seq"]}, agent="coordinator")
    if queued:
        ctx.event("task.queued", f"Tarea creada para {u['name']}, en espera de que termine el paso {s['depends_on']}", {"task_id": tid, "assignee": u["name"], "assignee_role": role, "depends_on": s["depends_on"]}, agent="coordinator")
    else:
        ctx.event("task.assigned", f"Tarea asignada a {u['name']}: {s['title'][:80]}", {"task_id": tid, "assignee": u["name"], "assignee_role": role, "plan_step": s["seq"]}, agent="coordinator")
        ctx.event("run.waiting_human", f"Esperando a {u['name']} ({role})", {"role": role, "task_id": tid}, agent="coordinator")
    return {"task_id": tid, "deduplicated": False, "queued": queued, "assignee": u["name"], "assignee_role": role,
            "_summary": f"tarea → {u['name']}{' (en espera)' if queued else ''}", "_resource": tid}


@tool("plans.activate_step", "plans.assign", PlanStep, access="write",
      description="Libera la tarea de un paso cuando su dependencia ya está cerrada con evidencia (compare-and-set; idempotente).")
def plans_activate_step(ctx, a):
    p = _plan(ctx, a.plan_id)
    _require_confirmed(p)
    s = _step(ctx, a.plan_id, a.seq)
    if not s["task_id"]:
        raise ToolError("el paso no tiene tarea creada")
    dep = one(ctx.conn, "SELECT status FROM plan_steps WHERE plan_id=:p AND seq=:s", p=p["id"], s=s["depends_on"]) if s["depends_on"] else None
    if dep and dep["status"] != "done":
        raise ToolError("la dependencia aún no está cerrada con evidencia")
    role = TASK_ACTIONS[s["action"]][0]
    n = q(ctx.conn, "UPDATE tasks SET status='pendiente' WHERE id=:i AND company_id=:c AND status='en_espera'", i=s["task_id"], c=ctx.company_id).rowcount
    if n != 1:
        return {"task_id": s["task_id"], "deduplicated": True, "_summary": "tarea ya liberada", "_resource": s["task_id"]}
    q(ctx.conn, "UPDATE plan_steps SET status='waiting_human',updated_at=:n WHERE id=:i AND company_id=:c AND status='waiting_dependency'", n=now(), i=s["id"], c=ctx.company_id)
    u = one(ctx.conn, "SELECT name FROM users WHERE id=:i AND company_id=:c", i=s["assignee_user_id"], c=ctx.company_id)
    ctx.event("task.assigned", f"Dependencia cumplida: tarea liberada para {u['name']}", {"task_id": s["task_id"], "assignee": u["name"], "assignee_role": role, "plan_step": s["seq"]}, agent="coordinator")
    ctx.event("run.waiting_human", f"Esperando a {u['name']} ({role})", {"role": role, "task_id": s["task_id"]}, agent="coordinator")
    return {"task_id": s["task_id"], "deduplicated": False, "assignee_role": role, "_summary": f"tarea liberada para {u['name']}", "_resource": s["task_id"]}


@tool("plans.verify_step", "plans.verify", PlanStep, access="write",
      description="El coordinador verifica un paso mirando el REGISTRO (tarea hecha por su responsable, con evidencia), no una afirmación; añade la foto real de stock.")
def plans_verify_step(ctx, a):
    p = _plan(ctx, a.plan_id)
    _require_confirmed(p)
    s = _step(ctx, a.plan_id, a.seq)
    if s["status"] == "done":
        return {"deduplicated": True, "result": jload(s["result"], {}), "_summary": "paso ya verificado", "_resource": s["id"]}
    if s["status"] != "waiting_human" or not s["task_id"]:
        raise ToolError("el paso no está esperando evidencia de una persona")
    t = one(ctx.conn, "SELECT status,evidence,completed_by,assignee_user_id,run_id,title FROM tasks WHERE id=:i AND company_id=:c", i=s["task_id"], c=ctx.company_id)
    if not t or t["status"] != "hecha":
        raise ToolError("la tarea aún no está cerrada en el registro")
    if t["run_id"] != p["id"] or t["completed_by"] != s["assignee_user_id"] or t["assignee_user_id"] != s["assignee_user_id"]:
        raise ToolError("la tarea no fue cerrada por su responsable validado")
    if len((t["evidence"] or "").strip()) < 10:
        raise ToolError("la tarea no tiene evidencia suficiente")
    result = {"verified": True, "task_id": s["task_id"], "evidence": t["evidence"], "completed_by": t["completed_by"]}
    if s["action"] == "ask_admin_verify_materials":
        result.update(materials_snapshot(ctx.conn, ctx.company_id, p["request_id"]))
    n = q(ctx.conn, "UPDATE plan_steps SET status='done',result=:r,updated_at=:t WHERE id=:i AND company_id=:c AND status='waiting_human'", r=jdump(result), t=now(), i=s["id"], c=ctx.company_id).rowcount
    if n != 1:
        return {"deduplicated": True, "result": result, "_summary": "paso ya verificado por otra operación", "_resource": s["id"]}
    sh = result.get("shortages") or []
    ctx.event("plan.step_verified", f"Verificado en el registro: «{t['title'][:70]}»" + (f" · FALTAN {len(sh)} material(es)" if sh else ""),
              {"step": s["seq"], "task_id": s["task_id"], "evidence": t["evidence"], "shortages": sh, "parts": result.get("parts")}, agent="coordinator")
    return {"deduplicated": False, "result": result, "_summary": "paso verificado con evidencia" + (f"; faltantes: {', '.join(x['sku'] for x in sh)}" if sh else ""), "_resource": s["id"]}


class FinishIn(Strict):
    plan_id: str


@tool("plans.finish", "plans.finish", FinishIn, access="write",
      description="Cierra el plan SOLO si todos sus pasos están hechos y verificados; registra el resumen final (idempotente).")
def plans_finish(ctx, a):
    p = _plan(ctx, a.plan_id)
    if p["status"] == "completed":
        return {"deduplicated": True, "summary": p["result_summary"], "_summary": "plan ya estaba completado", "_resource": p["id"]}
    _require_confirmed(p)
    steps = all_(ctx.conn, "SELECT * FROM plan_steps WHERE plan_id=:p AND company_id=:c ORDER BY seq", p=p["id"], c=ctx.company_id)
    pend = [s for s in steps if s["action"] != "report_to_owner" and s["status"] != "done"]
    if pend:
        raise ToolError("quedan pasos sin cerrar con evidencia: no se puede completar el plan")
    lines, shortages = [], []
    for s in steps:
        r = jload(s["result"], {})
        if s["action"] == "ask_admin_verify_materials":
            lines.append(f"Administración verificó materiales: {r.get('evidence', '')[:140]}")
            shortages = r.get("shortages") or []
            for x in r.get("parts") or []:
                lines.append(f"  · {x['sku']} {x['name']}: necesita {x['needed']}, stock {x['stock']}")
        elif s["action"] == "ask_technician_confirm":
            lines.append(f"Técnico confirmó: {r.get('evidence', '')[:140]}")
        elif s["action"] == "review_pending":
            for x in (r.get("open_requests") or [])[:10]:
                lines.append(f"  · {x['id']} · {x['customer']} · {x['status']} · visita {x.get('visit_status') or 'sin visita'}")
            lines.insert(0, f"{len(r.get('open_requests') or [])} solicitud(es) abierta(s):")
    has_admin = any(s["action"] == "ask_admin_verify_materials" for s in steps)
    if not has_admin:
        head = "Revisión completada (solo lectura)."
    elif shortages:
        head = "FALTAN MATERIALES: " + ", ".join(f"{x['sku']} (necesita {x['needed']}, hay {x['stock']})" for x in shortages) + ". No agendes hasta resolverlo."
    else:
        head = "No faltan materiales según el stock real."
    summary = head + "\n" + "\n".join(lines)
    n = q(ctx.conn, "UPDATE plans SET status='completed',result_summary=:s,updated_at=:t WHERE id=:i AND company_id=:c AND status IN ('running','waiting_human')", s=summary, t=now(), i=p["id"], c=ctx.company_id).rowcount
    if n != 1:
        return {"deduplicated": True, "_summary": "plan ya cerrado por otra operación", "_resource": p["id"]}
    q(ctx.conn, "UPDATE plan_steps SET status='done',result=:r,updated_at=:t WHERE plan_id=:p AND company_id=:c AND action='report_to_owner'", r=jdump({"reported": True, "shortages": shortages}), t=now(), p=p["id"], c=ctx.company_id)
    q(ctx.conn, "INSERT INTO results(id,company_id,run_id,request_id,kind,summary,data,created_at) VALUES(:i,:c,:ru,:r,'plan_resultado',:s,:d,:t)",
      i=uid("res_"), c=ctx.company_id, ru=p["id"], r=p["request_id"], s=summary[:500], d=jdump({"shortages": shortages}), t=now())
    ctx.event("run.completed", "Plan completado: " + head, {"summary": summary, "shortages": shortages}, agent="coordinator")
    return {"deduplicated": False, "summary": summary, "shortages": shortages, "_summary": "plan completado" + (" con faltantes" if shortages else ""), "_resource": p["id"]}
