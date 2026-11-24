"""Business tools. Each handler runs in one transaction and EVERY query is filtered by ctx.company_id.
`mode` tells the truth about what the tool is: real DB logic, deterministic rule, LLM proposal, or simulated external action.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Literal

from pydantic import Field

from ..db import all_, jdump, jload, now, one, q, uid
from .registry import Strict, ToolCtx, ToolError, tool


def _own_refs(ctx: ToolCtx, *, request_id: str | None = None, user_id: str | None = None, commitment_id: str | None = None) -> None:
    """Las referencias OPCIONALES que una herramienta guarda (solicitud, empleado, compromiso) deben existir EN ESTA empresa. Misma respuesta para «ausente» y «de otra empresa»
    (sin canal de existencia); sin esto una fila de B podía apuntar a un objeto de A y el panel de B mostraba el nombre del empleado de A (matriz de V0.7 P1)."""
    if request_id is not None:
        _req(ctx, request_id)
    if user_id is not None and not one(ctx.conn, "SELECT 1 AS x FROM users WHERE id=:i AND company_id=:c", i=user_id, c=ctx.company_id):
        raise ToolError("empleado no encontrado")
    if commitment_id is not None and not one(ctx.conn, "SELECT 1 AS x FROM commitments WHERE id=:i AND company_id=:c", i=commitment_id, c=ctx.company_id):
        raise ToolError("compromiso no encontrado")


def _req(ctx: ToolCtx, rid: str) -> dict:
    r = one(ctx.conn, "SELECT * FROM requests WHERE id=:i AND company_id=:c", i=rid, c=ctx.company_id)
    if not r:
        raise ToolError("solicitud no encontrada")  # same answer for 'missing' and 'other tenant' (no existence leak)
    return r


# ---------- customers / requests (reception) ----------
class FindCustomer(Strict):
    query: str = Field(min_length=2, max_length=120)


@tool("customers.find", "customers.read", FindCustomer, description="Busca clientes por teléfono, email o nombre.")
def customers_find(ctx, a):
    digits = re.sub(r"\D", "", a.query)
    q_ = a.query.strip().lower()
    rows = all_(ctx.conn, "SELECT id,name,phone,email,address FROM customers WHERE company_id=:c", c=ctx.company_id)
    hits = [r for r in rows if (digits and len(digits) >= 7 and digits in re.sub(r"\D", "", r["phone"] or ""))
            or (r["email"] and r["email"].lower() == q_) or q_ in r["name"].lower()]
    return {"matches": hits[:3], "found": bool(hits), "_summary": f"{len(hits)} coincidencia(s)"}


class CreateCustomer(Strict):
    name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=120)


@tool("customers.create", "customers.write", CreateCustomer, access="write", description="Crea un cliente nuevo.")
def customers_create(ctx, a):
    digits = re.sub(r"\D", "", a.phone or "")
    for r in all_(ctx.conn, "SELECT * FROM customers WHERE company_id=:c", c=ctx.company_id):  # dedupe by phone/email
        if (digits and digits == re.sub(r"\D", "", r["phone"] or "")) or (a.email and r["email"] == a.email):
            return {"customer": r, "created": False, "_summary": "cliente ya existía", "_resource": r["id"]}
    cid = uid("cus_")
    q(ctx.conn, "INSERT INTO customers(id,company_id,name,phone,email,created_at) VALUES(:i,:c,:n,:p,:e,:t)",
      i=cid, c=ctx.company_id, n=a.name, p=a.phone, e=a.email, t=now())
    return {"customer": {"id": cid, "name": a.name, "phone": a.phone, "email": a.email}, "created": True,
            "_summary": f"cliente creado {cid}", "_resource": cid}


class CustomerId(Strict):
    customer_id: str


@tool("customers.history", "customers.read", CustomerId, description="Historial de solicitudes y visitas de un cliente.")
def customers_history(ctx, a):
    rows = all_(ctx.conn, "SELECT id,description,category,status,created_at FROM requests WHERE company_id=:c AND customer_id=:k ORDER BY created_at DESC LIMIT 10",
                c=ctx.company_id, k=a.customer_id)
    return {"requests": rows, "_summary": f"{len(rows)} solicitud(es) previas"}


class CreateRequest(Strict):
    customer_id: str
    description: str = Field(min_length=3, max_length=2000)
    idem_key: str = Field(min_length=3, max_length=120)
    conversation_id: str | None = None


@tool("requests.create", "requests.create", CreateRequest, access="write", description="Registra una solicitud (idempotente por idem_key).")
def requests_create(ctx, a):
    if not one(ctx.conn, "SELECT 1 AS x FROM customers WHERE id=:i AND company_id=:c", i=a.customer_id, c=ctx.company_id):
        raise ToolError("cliente no encontrado")
    ex = one(ctx.conn, "SELECT * FROM requests WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=a.idem_key)
    if ex:
        return {"request_id": ex["id"], "deduplicated": True, "_summary": f"solicitud {ex['id']} ya existía (idempotente)", "_resource": ex["id"]}
    rid = uid("req_")
    t = now()
    q(ctx.conn, "INSERT INTO requests(id,company_id,customer_id,conversation_id,description,status,idem_key,created_at,updated_at) VALUES(:i,:c,:k,:cv,:d,'nueva',:ik,:t,:t)",
      i=rid, c=ctx.company_id, k=a.customer_id, cv=a.conversation_id, d=a.description, ik=a.idem_key, t=t)
    return {"request_id": rid, "deduplicated": False, "_summary": f"solicitud {rid} registrada", "_resource": rid}


class RequestId(Strict):
    request_id: str


@tool("requests.get", "requests.read", RequestId, description="Detalle de una solicitud con cliente, presupuesto y visita.")
def requests_get(ctx, a):
    r = _req(ctx, a.request_id)
    cust = one(ctx.conn, "SELECT id,name,phone,email,address FROM customers WHERE id=:i AND company_id=:c", i=r["customer_id"], c=ctx.company_id)
    quotes = all_(ctx.conn, "SELECT kind,total,status,lines FROM quotes WHERE request_id=:r AND company_id=:c", r=r["id"], c=ctx.company_id)
    visit = one(ctx.conn, "SELECT status,slot_start,customer_confirmed FROM visits WHERE request_id=:r AND company_id=:c", r=r["id"], c=ctx.company_id)
    return {"request": r, "customer": cust, "quotes": quotes, "visit": visit, "_summary": f"{r['status']}"}


class ListRequests(Strict):
    status: str | None = None


@tool("requests.list", "requests.read", ListRequests, description="Lista solicitudes de la empresa.")
def requests_list(ctx, a):
    sql = "SELECT r.id,r.description,r.category,r.urgency,r.status,r.created_at,c.name AS customer FROM requests r JOIN customers c ON c.id=r.customer_id WHERE r.company_id=:c"
    p = {"c": ctx.company_id}
    if a.status:
        sql += " AND r.status=:s"
        p["s"] = a.status
    rows = all_(ctx.conn, sql + " ORDER BY r.created_at DESC LIMIT 50", **p)
    return {"requests": rows, "_summary": f"{len(rows)} solicitud(es)"}


class Classify(Strict):
    request_id: str


@tool("requests.classify", "classify.run", Classify, mode="deterministic", access="write",
      description="Clasifica la solicitud (categoría, urgencia, repuestos probables). Proveedor intercambiable: reglas o LLM local.")
def requests_classify(ctx, a):
    r = _req(ctx, a.request_id)
    skus = [p["sku"] for p in all_(ctx.conn, "SELECT sku FROM parts WHERE company_id=:c", c=ctx.company_id)]
    cl = ctx.provider.classify(r["description"], skus)
    q(ctx.conn, "UPDATE requests SET category=:k,urgency=:u,status='clasificada',updated_at=:t WHERE id=:i AND company_id=:c",
      k=cl.category, u=cl.urgency, t=now(), i=r["id"], c=ctx.company_id)
    return {"category": cl.category, "urgency": cl.urgency, "parts": cl.parts, "rationale": cl.rationale, "source": cl.source,
            "_mode": "llm" if cl.source.startswith("llm") else "deterministic", "_summary": f"{cl.category} · urgencia {cl.urgency} ({cl.source})", "_resource": r["id"]}


class UpdateStatus(Strict):
    request_id: str
    status: Literal["nueva", "clasificada", "presupuesto_en_revision", "bloqueada_repuesto", "propuesta_preparada", "visita_confirmada",
                    "presupuesto_rechazado", "alternativa_preparada", "cerrada"]


@tool("requests.update_status", "requests.update", UpdateStatus, access="write", description="Cambia el estado de una solicitud.")
def requests_update_status(ctx, a):
    r = _req(ctx, a.request_id)
    q(ctx.conn, "UPDATE requests SET status=:s,updated_at=:t WHERE id=:i AND company_id=:c", s=a.status, t=now(), i=r["id"], c=ctx.company_id)
    return {"status": a.status, "_summary": f"estado → {a.status}", "_resource": r["id"]}


# ---------- availability / pricing / inventory / quotes / visits (coordinator + admin) ----------
class Avail(Strict):
    limit: int = Field(default=5, ge=1, le=20)


@tool("availability.technicians", "availability.read", Avail, description="Franjas libres de técnicos.")
def availability_technicians(ctx, a):
    # V0.5: only ELIGIBLE technicians (active + enabled personal assistant); at the same hour the LEAST-booked technician comes first (not «the first» by id)
    rows = all_(ctx.conn, """SELECT a.id,a.technician_id,u.name AS technician,a.slot_start,a.slot_end FROM availability a
                             JOIN users u ON u.id=a.technician_id AND u.company_id=a.company_id
                             WHERE a.company_id=:c AND a.status='free' AND COALESCE(u.active,1)=1 AND u.role='technician'
                               AND EXISTS(SELECT 1 FROM agents g WHERE g.company_id=a.company_id AND g.user_id=u.id AND g.role='tech_assistant' AND g.enabled=1)
                             ORDER BY a.slot_start,
                                      (SELECT COUNT(*) FROM availability h WHERE h.company_id=a.company_id AND h.technician_id=a.technician_id AND h.status<>'free'), u.name LIMIT :l""",
                c=ctx.company_id, l=a.limit)
    return {"slots": rows, "_summary": f"{len(rows)} franja(s) libre(s)"}


class Pricing(Strict):
    category: str = Field(max_length=60)


@tool("pricing.lookup", "pricing.read", Pricing, description="Servicios y tarifas de la lista de precios para una categoría.")
def pricing_lookup(ctx, a):
    rows = all_(ctx.conn, "SELECT code,name,price FROM services WHERE company_id=:c AND category=:k", c=ctx.company_id, k=a.category)
    if not rows:
        rows = all_(ctx.conn, "SELECT code,name,price FROM services WHERE company_id=:c AND category='diagnostico'", c=ctx.company_id)
    return {"services": rows, "_summary": f"{len(rows)} servicio(s): {', '.join(r['code'] for r in rows)}"}


class Inv(Strict):
    skus: list[str] = Field(max_length=10)


@tool("inventory.check", "inventory.read", Inv, description="Stock y alternativa de repuestos.")
def inventory_check(ctx, a):
    out = []
    for sku in a.skus:
        p = one(ctx.conn, "SELECT sku,name,stock,unit_price,alt_sku FROM parts WHERE company_id=:c AND sku=:s", c=ctx.company_id, s=sku)
        if not p:
            out.append({"sku": sku, "exists": False, "available": False})
            continue
        alt = one(ctx.conn, "SELECT sku,name,stock,unit_price FROM parts WHERE company_id=:c AND sku=:s", c=ctx.company_id, s=p["alt_sku"]) if p["alt_sku"] else None
        out.append({**p, "exists": True, "available": p["stock"] > 0, "alternative": alt})
    miss = [o["sku"] for o in out if not o["available"]]
    return {"parts": out, "missing": miss, "_summary": "todo disponible" if not miss else f"SIN STOCK: {', '.join(miss)}"}


class PrepareQuote(Strict):
    request_id: str
    kind: Literal["principal", "alternativa"] = "principal"
    service_codes: list[str] = Field(max_length=6)
    part_skus: list[str] = Field(default_factory=list, max_length=6)


@tool("quotes.prepare", "quotes.write", PrepareQuote, access="write", description="Prepara un presupuesto BORRADOR con precios de la base (idempotente por solicitud+tipo).")
def quotes_prepare(ctx, a):
    r = _req(ctx, a.request_id)
    ex = one(ctx.conn, "SELECT * FROM quotes WHERE company_id=:c AND request_id=:r AND kind=:k", c=ctx.company_id, r=r["id"], k=a.kind)
    if ex:
        return {"quote_id": ex["id"], "total": ex["total"], "lines": jload(ex["lines"]), "deduplicated": True,
                "_summary": f"presupuesto ya existía (${ex['total']:,.0f})", "_resource": ex["id"]}
    lines = []
    for code in a.service_codes:
        s = one(ctx.conn, "SELECT code,name,price FROM services WHERE company_id=:c AND code=:s", c=ctx.company_id, s=code)
        if not s:
            raise ToolError(f"servicio inexistente: {code}")
        lines.append({"type": "servicio", "code": s["code"], "name": s["name"], "qty": 1, "unit": s["price"]})
    for sku in a.part_skus:
        p = one(ctx.conn, "SELECT sku,name,unit_price,stock FROM parts WHERE company_id=:c AND sku=:s", c=ctx.company_id, s=sku)
        if not p:
            raise ToolError(f"repuesto inexistente: {sku}")
        lines.append({"type": "repuesto", "code": p["sku"], "name": p["name"], "qty": 1, "unit": p["unit_price"]})
    total = sum(l["qty"] * l["unit"] for l in lines)
    qid = uid("quo_")
    q(ctx.conn, "INSERT INTO quotes(id,company_id,request_id,kind,lines,total,status,created_at) VALUES(:i,:c,:r,:k,:l,:t,'borrador',:n)",
      i=qid, c=ctx.company_id, r=r["id"], k=a.kind, l=jdump(lines), t=total, n=now())
    return {"quote_id": qid, "total": total, "lines": lines, "deduplicated": False, "_summary": f"presupuesto borrador ${total:,.0f}", "_resource": qid}


class QuoteStatus(Strict):
    request_id: str
    kind: Literal["principal", "alternativa"] = "principal"
    status: Literal["aprobado", "rechazado"]


@tool("quotes.set_status", "quotes.write", QuoteStatus, access="write", description="Marca el presupuesto como aprobado/rechazado (refleja la decisión humana).")
def quotes_set_status(ctx, a):
    r = _req(ctx, a.request_id)
    q(ctx.conn, "UPDATE quotes SET status=:s WHERE company_id=:c AND request_id=:r AND kind=:k", s=a.status, c=ctx.company_id, r=r["id"], k=a.kind)
    return {"status": a.status, "_summary": f"presupuesto {a.status}", "_resource": r["id"]}


class ProposeVisit(Strict):
    request_id: str
    availability_id: str


@tool("visits.propose", "visits.propose", ProposeVisit, access="write",
      description="PROPONE una visita (estado 'propuesta', no confirmada). Retiene la franja; solo el cliente puede confirmarla.")
def visits_propose(ctx, a):
    r = _req(ctx, a.request_id)
    ex = one(ctx.conn, "SELECT * FROM visits WHERE company_id=:c AND request_id=:r", c=ctx.company_id, r=r["id"])
    if ex:
        return {"visit_id": ex["id"], "slot_start": ex["slot_start"], "status": ex["status"], "deduplicated": True,
                "_summary": "visita ya propuesta (idempotente)", "_resource": ex["id"]}
    s = one(ctx.conn, """SELECT av.* FROM availability av JOIN users u ON u.id=av.technician_id AND u.company_id=av.company_id
                         WHERE av.id=:i AND av.company_id=:c AND av.status='free' AND u.role='technician' AND COALESCE(u.active,1)=1""", i=a.availability_id, c=ctx.company_id)   # a slot of someone who is no longer a technician is not bookable
    if not s:
        raise ToolError("franja no disponible")
    vid = uid("vis_")
    q(ctx.conn, "UPDATE availability SET status='held',request_id=:r WHERE id=:i AND company_id=:c", r=r["id"], i=s["id"], c=ctx.company_id)
    q(ctx.conn, "INSERT INTO visits(id,company_id,request_id,technician_id,slot_start,slot_end,status,customer_confirmed,confirm_code,created_at) VALUES(:i,:c,:r,:t,:s,:e,'propuesta',0,:cc,:n)",
      i=vid, c=ctx.company_id, r=r["id"], t=s["technician_id"], s=s["slot_start"], e=s["slot_end"], cc=secrets.token_hex(3).upper(), n=now())
    return {"visit_id": vid, "slot_start": s["slot_start"], "technician_id": s["technician_id"], "status": "propuesta", "customer_confirmed": False,
            "deduplicated": False, "_summary": f"visita PROPUESTA {s['slot_start']} (sin confirmar)", "_resource": vid}


# ---------- work orders / technician ----------
class CreateWO(Strict):
    request_id: str
    technician_id: str


@tool("workorders.create", "workorders.write", CreateWO, access="write", description="Crea la orden de trabajo (idempotente).")
def workorders_create(ctx, a):
    r = _req(ctx, a.request_id)
    if not one(ctx.conn, "SELECT 1 AS x FROM users WHERE id=:i AND company_id=:c AND role='technician'", i=a.technician_id, c=ctx.company_id):
        raise ToolError("técnico no encontrado")
    ex = one(ctx.conn, "SELECT id FROM work_orders WHERE company_id=:c AND request_id=:r", c=ctx.company_id, r=r["id"])
    if ex:
        return {"work_order_id": ex["id"], "deduplicated": True, "_summary": "orden ya existía", "_resource": ex["id"]}
    wid, t = uid("wo_"), now()
    q(ctx.conn, "INSERT INTO work_orders(id,company_id,request_id,technician_id,status,created_at,updated_at) VALUES(:i,:c,:r,:t,'pendiente_confirmacion_cliente',:n,:n)",
      i=wid, c=ctx.company_id, r=r["id"], t=a.technician_id, n=t)
    q(ctx.conn, "UPDATE requests SET assigned_user_id=:u WHERE id=:r AND company_id=:c", u=a.technician_id, r=r["id"], c=ctx.company_id)
    return {"work_order_id": wid, "deduplicated": False, "_summary": f"orden {wid} creada", "_resource": wid}


@tool("workorders.acknowledge", "workorders.update", RequestId, access="write", description="El asistente del técnico acusa recibo de la orden asignada a su usuario.")
def workorders_ack(ctx, a):
    w = one(ctx.conn, "SELECT * FROM work_orders WHERE company_id=:c AND request_id=:r", c=ctx.company_id, r=a.request_id)
    if not w or w["technician_id"] != ctx.principal.user_id:
        raise ToolError("orden no asignada a este técnico")
    q(ctx.conn, "UPDATE work_orders SET notes=:n,updated_at=:t WHERE id=:i AND company_id=:c", n="técnico notificado", t=now(), i=w["id"], c=ctx.company_id)
    return {"work_order_id": w["id"], "_summary": "técnico notificado de la orden pendiente", "_resource": w["id"]}


class NoArgs(Strict):
    pass


@tool("inventory.list", "inventory.read", NoArgs, description="Catálogo completo de repuestos con stock actual (solo lectura).")
def inventory_list(ctx, a):
    rows = all_(ctx.conn, "SELECT sku,name,stock,unit_price,alt_sku FROM parts WHERE company_id=:c ORDER BY sku LIMIT 100", c=ctx.company_id)
    miss = [r["sku"] for r in rows if r["stock"] <= 0]
    return {"parts": rows, "missing": miss, "_summary": f"{len(rows)} repuesto(s)" + (f"; SIN STOCK: {', '.join(miss)}" if miss else "")}


@tool("workorders.list_mine", "workorders.read_assigned", NoArgs, description="Trabajos asignados al técnico.")
def workorders_list_mine(ctx, a):
    rows = all_(ctx.conn, """SELECT w.request_id,w.status,r.description,r.category,c.name AS customer,c.address,v.slot_start,v.status AS visit_status
                             FROM work_orders w JOIN requests r ON r.id=w.request_id JOIN customers c ON c.id=r.customer_id
                             LEFT JOIN visits v ON v.request_id=w.request_id AND v.company_id=w.company_id
                             WHERE w.company_id=:c AND w.technician_id=:u ORDER BY w.created_at DESC""", c=ctx.company_id, u=ctx.principal.user_id)
    return {"work_orders": rows, "_summary": f"{len(rows)} trabajo(s) asignado(s)"}


class WOStatus(Strict):
    request_id: str
    status: Literal["en_camino", "en_curso", "bloqueada", "completada"]
    note: str = Field(default="", max_length=500)


@tool("workorders.update_status", "workorders.update", WOStatus, access="write", description="El técnico actualiza el estado de su orden.")
def workorders_update(ctx, a):
    w = one(ctx.conn, "SELECT * FROM work_orders WHERE company_id=:c AND request_id=:r AND technician_id=:u", c=ctx.company_id, r=a.request_id, u=ctx.principal.user_id)
    if not w:
        raise ToolError("orden no asignada a este técnico")
    q(ctx.conn, "UPDATE work_orders SET status=:s,notes=:n,updated_at=:t WHERE id=:i AND company_id=:c", s=a.status, n=a.note, t=now(), i=w["id"], c=ctx.company_id)
    return {"status": a.status, "_summary": f"orden → {a.status}", "_resource": w["id"]}


class MatReq(Strict):
    sku: str
    qty: int = Field(default=1, ge=1, le=50)
    request_id: str | None = None


@tool("materials.request", "materials.request", MatReq, access="write", description="Solicita reposición de material (idempotente por solicitud+sku).")
def materials_request(ctx, a):
    if not one(ctx.conn, "SELECT 1 AS x FROM parts WHERE company_id=:c AND sku=:s", c=ctx.company_id, s=a.sku):
        raise ToolError("repuesto inexistente")
    key = f"{a.request_id or ctx.principal.user_id or 'co'}:{a.sku}"
    ex = one(ctx.conn, "SELECT id FROM material_requests WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=key)
    if ex:
        return {"material_request_id": ex["id"], "deduplicated": True, "_summary": "solicitud de material ya existía", "_resource": ex["id"]}
    _own_refs(ctx, request_id=a.request_id)
    mid = uid("mat_")
    q(ctx.conn, "INSERT INTO material_requests(id,company_id,request_id,sku,qty,status,requested_by,idem_key,created_at) VALUES(:i,:c,:r,:s,:q,'solicitado',:b,:k,:t)",
      i=mid, c=ctx.company_id, r=a.request_id, s=a.sku, q=a.qty, b=ctx.principal.role, k=key, t=now())
    return {"material_request_id": mid, "deduplicated": False, "_summary": f"material {a.sku} x{a.qty} solicitado", "_resource": mid}


class Issue(Strict):
    request_id: str
    description: str = Field(min_length=3, max_length=500)


@tool("issues.report", "issues.report", Issue, access="write", description="El técnico reporta un problema; se crea una tarea para el coordinador.")
def issues_report(ctx, a):
    _req(ctx, a.request_id)
    key = "issue:" + hashlib.sha1(f"{a.request_id}{a.description}".encode()).hexdigest()[:12]
    ex = one(ctx.conn, "SELECT id FROM tasks WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=key)
    if ex:
        return {"task_id": ex["id"], "deduplicated": True, "_summary": "problema ya reportado", "_resource": ex["id"]}
    tid = uid("tsk_")
    q(ctx.conn, "INSERT INTO tasks(id,company_id,request_id,title,assignee_agent,status,idem_key,created_at) VALUES(:i,:c,:r,:ti,'coordinator','pendiente',:k,:t)",
      i=tid, c=ctx.company_id, r=a.request_id, ti=f"Problema reportado: {a.description}", k=key, t=now())
    return {"task_id": tid, "deduplicated": False, "_summary": "problema reportado al coordinador", "_resource": tid}


# ---------- tasks / delegations / approvals / messages / results ----------
class CreateTask(Strict):
    request_id: str | None = None
    title: str = Field(min_length=3, max_length=200)
    assignee_agent: str | None = None
    assignee_user_id: str | None = None
    idem_key: str = Field(min_length=3, max_length=120)
    kind: str | None = Field(default=None, max_length=30)
    due: str | None = Field(default=None, max_length=40)
    commitment_id: str | None = None


@tool("tasks.create", "tasks.write", CreateTask, access="write", description="Crea una tarea (idempotente por idem_key).")
def tasks_create(ctx, a):
    ex = one(ctx.conn, "SELECT id FROM tasks WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=a.idem_key)
    if ex:
        return {"task_id": ex["id"], "deduplicated": True, "_summary": "tarea ya existía", "_resource": ex["id"]}
    _own_refs(ctx, request_id=a.request_id, user_id=a.assignee_user_id, commitment_id=a.commitment_id)   # V0.7 P1: cada referencia debe ser de ESTA empresa (la matriz de aislamiento lo encontró)
    tid = uid("tsk_")
    q(ctx.conn, "INSERT INTO tasks(id,company_id,request_id,run_id,title,assignee_user_id,assignee_agent,status,idem_key,created_at,kind,due,commitment_id) VALUES(:i,:c,:r,:ru,:ti,:u,:a,'pendiente',:k,:t,:kd,:du,:cm)",
      i=tid, c=ctx.company_id, r=a.request_id, ru=ctx.run_id, ti=a.title, u=a.assignee_user_id, a=a.assignee_agent, k=a.idem_key, t=now(), kd=a.kind, du=a.due, cm=a.commitment_id)
    return {"task_id": tid, "deduplicated": False, "_summary": f"tarea: {a.title}", "_resource": tid}


@tool("tasks.list", "tasks.read", NoArgs, description="Tareas pendientes (el técnico solo ve las suyas).")
def tasks_list(ctx, a):
    sql = """SELECT t.id,t.title,t.status,t.kind,t.due,t.assignee_agent,t.assignee_user_id,u.name AS assignee,t.request_id,t.run_id,t.commitment_id,t.created_at
             FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id AND u.company_id=t.company_id WHERE t.company_id=:c AND t.status='pendiente'"""
    p = {"c": ctx.company_id}
    if ctx.principal.user_role in ("technician", "admin"):  # personal view: your own tasks (admin also sees agent-level ones via the dashboard)
        sql += " AND t.assignee_user_id=:u"
        p["u"] = ctx.principal.user_id
    rows = all_(ctx.conn, sql + " ORDER BY t.created_at DESC LIMIT 50", **p)
    return {"tasks": rows, "_summary": f"{len(rows)} tarea(s) pendiente(s)"}


class Delegate(Strict):
    from_agent: str
    to_agent: str
    task: str = Field(max_length=300)
    idem_key: str


@tool("delegations.create", "delegations.write", Delegate, access="write", description="Registra una delegación entre agentes.")
def delegations_create(ctx, a):
    if a.from_agent != ctx.principal.role:
        raise ToolError("un agente solo puede delegar en su propio nombre")
    ex = one(ctx.conn, "SELECT id FROM delegations WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=a.idem_key)
    if ex:
        return {"delegation_id": ex["id"], "deduplicated": True, "_summary": "delegación ya registrada", "_resource": ex["id"]}
    did = uid("del_")
    q(ctx.conn, "INSERT INTO delegations(id,company_id,run_id,from_agent,to_agent,task,status,idem_key,created_at) VALUES(:i,:c,:r,:f,:to,:ta,'enviada',:k,:t)",
      i=did, c=ctx.company_id, r=ctx.run_id or "-", f=a.from_agent, to=a.to_agent, ta=a.task, k=a.idem_key, t=now())
    return {"delegation_id": did, "from": a.from_agent, "to": a.to_agent, "deduplicated": False, "_summary": f"{a.from_agent} → {a.to_agent}: {a.task}", "_resource": did}


class RequestApproval(Strict):
    request_id: str
    subject: str = Field(max_length=120)
    summary: str = Field(max_length=500)
    amount: float = Field(ge=0)


@tool("approvals.request", "approvals.request", RequestApproval, access="write", description="Solicita aprobación HUMANA (no la decide).")
def approvals_request(ctx, a):
    _req(ctx, a.request_id)
    ex = one(ctx.conn, "SELECT id,status FROM approvals WHERE company_id=:c AND run_id=:r AND subject=:s", c=ctx.company_id, r=ctx.run_id, s=a.subject)
    if ex:
        return {"approval_id": ex["id"], "status": ex["status"], "deduplicated": True, "_summary": "aprobación ya solicitada (idempotente)", "_resource": ex["id"]}
    aid = uid("apr_")
    q(ctx.conn, "INSERT INTO approvals(id,company_id,run_id,request_id,subject,summary,amount,status,requested_by_agent,created_at) VALUES(:i,:c,:r,:rq,:s,:su,:am,'pendiente',:by,:t)",
      i=aid, c=ctx.company_id, r=ctx.run_id, rq=a.request_id, s=a.subject, su=a.summary, am=a.amount, by=ctx.principal.role, t=now())
    return {"approval_id": aid, "status": "pendiente", "deduplicated": False, "_summary": f"aprobación solicitada al dueño (${a.amount:,.0f})", "_resource": aid}


@tool("approvals.pending", "approvals.read", NoArgs, description="Aprobaciones pendientes de la empresa.")
def approvals_pending(ctx, a):
    rows = all_(ctx.conn, "SELECT id,request_id,subject,summary,amount,created_at FROM approvals WHERE company_id=:c AND status='pendiente' ORDER BY created_at", c=ctx.company_id)
    return {"approvals": rows, "_summary": f"{len(rows)} aprobación(es) pendiente(s)"}


class PrepMsg(Strict):
    request_id: str
    kind: Literal["visit_proposal", "alternative", "ack", "material_update"]
    body: str = Field(min_length=3, max_length=1500)


@tool("messages.prepare_customer", "messages.prepare", PrepMsg, mode="simulated", access="write",
      description="Prepara (NO envía) un mensaje al cliente. Canal externo simulado: queda en estado preparado_no_enviado.")
def messages_prepare(ctx, a):
    r = _req(ctx, a.request_id)
    key = f"{r['id']}:{a.kind}"
    ex = one(ctx.conn, "SELECT id FROM messages WHERE company_id=:c AND idem_key=:k", c=ctx.company_id, k=key)
    if ex:
        return {"message_id": ex["id"], "deduplicated": True, "status": "preparado_no_enviado", "_summary": "mensaje ya preparado", "_resource": ex["id"]}
    conv = r["conversation_id"] or uid("cnv_")
    if not r["conversation_id"]:
        q(ctx.conn, "INSERT INTO conversations(id,company_id,customer_id,channel,created_at) VALUES(:i,:c,:k,'web',:t)", i=conv, c=ctx.company_id, k=r["customer_id"], t=now())
    mid = uid("msg_")
    q(ctx.conn, "INSERT INTO messages(id,company_id,conversation_id,sender,body,status,idem_key,created_at) VALUES(:i,:c,:cv,'agent:reception',:b,'preparado_no_enviado',:k,:t)",
      i=mid, c=ctx.company_id, cv=conv, b=a.body, k=key, t=now())
    return {"message_id": mid, "deduplicated": False, "status": "preparado_no_enviado", "body": a.body, "channel": "SIMULADO (WhatsApp/email no conectados)",
            "_summary": "mensaje al cliente PREPARADO, no enviado", "_resource": mid}


class RecordResult(Strict):
    request_id: str
    kind: str = Field(max_length=60)
    summary: str = Field(max_length=500)
    data: dict = Field(default_factory=dict)


@tool("results.record", "results.write", RecordResult, access="write", description="Registra el resultado final de una ejecución.")
def results_record(ctx, a):
    _req(ctx, a.request_id)
    ex = one(ctx.conn, "SELECT id FROM results WHERE company_id=:c AND run_id=:r AND kind=:k", c=ctx.company_id, r=ctx.run_id, k=a.kind)
    if ex:
        return {"result_id": ex["id"], "deduplicated": True, "_summary": "resultado ya registrado", "_resource": ex["id"]}
    rid = uid("res_")
    q(ctx.conn, "INSERT INTO results(id,company_id,run_id,request_id,kind,summary,data,created_at) VALUES(:i,:c,:r,:rq,:k,:s,:d,:t)",
      i=rid, c=ctx.company_id, r=ctx.run_id, rq=a.request_id, k=a.kind, s=a.summary, d=jdump(a.data), t=now())
    return {"result_id": rid, "deduplicated": False, "_summary": a.summary, "_resource": rid}


@tool("company.summary", "company.summary", NoArgs, description="Estado general de la empresa.")
def company_summary(ctx, a):
    c = ctx.company_id
    st = {r["status"]: r["n"] for r in all_(ctx.conn, "SELECT status, COUNT(*) AS n FROM requests WHERE company_id=:c GROUP BY status", c=c)}
    pend = one(ctx.conn, "SELECT COUNT(*) AS n FROM approvals WHERE company_id=:c AND status='pendiente'", c=c)["n"]
    tk = one(ctx.conn, "SELECT COUNT(*) AS n FROM tasks WHERE company_id=:c AND status='pendiente'", c=c)["n"]
    return {"requests_by_status": st, "pending_approvals": pend, "pending_tasks": tk,
            "_summary": f"{sum(st.values())} solicitudes · {pend} aprobación(es) pendiente(s) · {tk} tarea(s)"}


@tool("notes.read_own", "notes.read_own", NoArgs, description="Notas PRIVADAS del propio usuario (nunca las de otros).")
def notes_read_own(ctx, a):
    rows = all_(ctx.conn, "SELECT id,body,created_at FROM private_notes WHERE company_id=:c AND user_id=:u ORDER BY created_at", c=ctx.company_id, u=ctx.principal.user_id)
    return {"notes": rows, "_summary": f"{len(rows)} nota(s) propia(s)"}


class OtherNotes(Strict):
    user_id: str


@tool("notes.read_others", "notes.read_others", OtherNotes, description="Leer notas privadas de OTRO empleado. Ningún rol ni agente tiene este permiso.")
def notes_read_others(ctx, a):
    rows = all_(ctx.conn, "SELECT id,body FROM private_notes WHERE company_id=:c AND user_id=:u", c=ctx.company_id, u=a.user_id)
    return {"notes": rows}
