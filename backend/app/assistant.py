"""Personal assistant for an employee: deterministic intent routing → permissioned tools.
It acts with the employee's EFFECTIVE permissions (agent ∩ user). It cannot read other people's private notes.
"""
from __future__ import annotations

import re

from .db import jdump, now, one, q, uid
from .providers.base import validate_material_need
from .tools import Denied, ToolError, invoke
from .db import all_

ASSISTANT_BY_ROLE = {"owner": "owner_assistant", "admin": "admin_assistant", "technician": "tech_assistant"}


def _fmt(tool: str, out: dict) -> str:
    if tool in ("inventory.check", "inventory.list"):
        lines = []
        for x in out["parts"]:
            if not x.get("exists", True):
                lines.append(f"• {x['sku']}: no existe en el catálogo")
                continue
            alt = f" (alternativa {x['alternative']['sku']}: {x['alternative']['stock']} u.)" if x.get("alternative") else ""
            lines.append(f"• {x['sku']} · {x['name']}: {x['stock']} u." + (" — SIN STOCK" if x["stock"] <= 0 else "") + alt)
        return "Inventario (stock real de la base):\n" + ("\n".join(lines) or "sin repuestos")
    if tool == "company.summary":
        s = ", ".join(f"{k}: {v}" for k, v in out["requests_by_status"].items()) or "sin solicitudes"
        return f"Estado de la empresa — solicitudes ({s}). Aprobaciones pendientes: {out['pending_approvals']}. Tareas pendientes: {out['pending_tasks']}."
    if tool == "approvals.pending":
        return "Aprobaciones pendientes:\n" + ("\n".join(f"• {a['summary']} — ${a['amount']:,.0f}" for a in out["approvals"]) or "ninguna")
    if tool == "tasks.list":
        return "Tareas pendientes:\n" + ("\n".join(f"• {t['title']}  [{t['id']}{' · ' + t['kind'] if t.get('kind') else ''}{' · ' + t['assignee'] if t.get('assignee') else ''}]" for t in out["tasks"]) or "ninguna")
    if tool == "commitments.list":
        return "Compromisos:\n" + ("\n".join(f"• {c['title']} — {c['owner']} — plazo {c['due_at'][:16].replace('T', ' ')} — {c['status']}{' (VENCIDO)' if c.get('overdue') else ''}" for c in out["commitments"]) or "ninguno")
    if tool == "collab.request_material":
        return f"Listo: pedí el material a administración. Se creó la solicitud, la delegación y la tarea asignada a {out['assignee']} (costo estimado ${out['cost']:,.0f}; el dueño debe aprobar el gasto).".replace(",", ".")
    if tool == "tasks.complete":
        return "Tarea completada con tu evidencia." + (f" Efecto real: {out['effect']}." if out.get("effect") else "")
    if tool == "workorders.list_mine":
        return "Tus trabajos:\n" + ("\n".join(f"• {w['request_id']} · {w['customer']} · {w['status']} · {w.get('slot_start') or 'sin fecha'}" for w in out["work_orders"]) or "ninguno asignado")
    if tool == "requests.list":
        return "Solicitudes:\n" + ("\n".join(f"• {r['id']} · {r['customer']} · {r['status']}" for r in out["requests"][:8]) or "ninguna")
    if tool == "notes.read_own":
        return "Tus notas privadas:\n" + ("\n".join(f"• {n['body']}" for n in out["notes"]) or "ninguna")
    return out.get("_summary", "listo")


INSTRUCTION_RE = re.compile(r"^\s*(coordinador|dale trabajo)\b|\b(pide|p[ií]dele|pidele|asigna|encarga|solicita)\b.*(administraci|admin\b|t[eé]cnic|javiera|mat[ií]as|material|verific)", re.I)


def _plan_reply(v: dict) -> str:
    link = f"#/coordinator/{v['id']}"
    if v["status"] == "awaiting_user_confirmation":
        steps = "\n".join(f"  {s['seq']}. {s['label']}" + (f" → {s['assignee']}" if s["assignee"] else "") + (" (lectura)" if s["access"] == "read" else "") for s in v["steps"])
        return f"Plan {v['id']} PROPUESTO (aún no se creó ninguna tarea):\n{steps}\nRevísalo y confírmalo o cancélalo con un clic en la pantalla «Coordinador» ({link}). La confirmación la das tú, no yo."
    if v["status"] == "needs_clarification":
        return f"Necesito una aclaración antes de planificar: {v['status_reason']} {((v['clarification'] or {}).get('question') or '')} Respóndela en «Coordinador» ({link})."
    if v["status"] == "blocked":
        return f"No ejecuto esto. {v['status_reason']} (Plan bloqueado {v['id']}; no se hizo nada.)"
    return f"Plan {v['id']}: {v['status']}. {v.get('result_summary') or ''}"


def _owner_instruction(eng, orch, user: dict, text: str) -> dict:
    from .tools import ToolError as _TE
    try:
        out = orch.plans.create(user, text, "asst-" + uid())
        v = orch.plans.view(user["company_id"], out["plan_id"])
        body, calls = _plan_reply(v) + ("\n(Ya existía un plan abierto con esa misma instrucción; no se duplicó.)" if out["deduplicated"] else ""), [{"tool": "plans.propose", "ok": v["status"] != "blocked", "summary": f"{v['status']} · origen {v['source']}"}]
    except _TE as e:
        body, calls = f"No pude crear el plan: {e}", [{"tool": "plans.propose", "ok": False, "summary": str(e)}]
    _log(eng, user, "owner_assistant", text, body, calls)
    return {"reply": body, "tool_calls": calls, "assistant": "owner_assistant"}


def handle(eng, orch, user: dict, text: str) -> dict:
    role = ASSISTANT_BY_ROLE[user["role"]]
    try:
        pr = orch.principal(user["company_id"], role, user["id"])
    except ToolError as e:   # employee without an active personal assistant (V0.5: assistants are assigned from templates)
        body = f"No tienes un asistente personal activo ({e}). Pídele al dueño que te asigne uno en «Empresa y equipo»."
        _log(eng, user, role, text, body, [])
        return {"reply": body, "tool_calls": [{"tool": "assistant", "ok": False, "summary": str(e)}], "assistant": role}
    t = text.lower()
    if user["role"] == "owner" and orch.plans:
        from .engine.planner import scan_forbidden
        if re.search(r"confirm\w*\s+(el\s+)?plan", t):
            body = "La confirmación de un plan es una acción humana: ábrelo en «Coordinador» y pulsa Confirmar. Yo no puedo confirmar por ti."
            _log(eng, user, role, text, body, [])
            return {"reply": body, "tool_calls": [], "assistant": role}
        if INSTRUCTION_RE.search(text) or scan_forbidden(text):
            return _owner_instruction(eng, orch, user, text)
    rid = (re.search(r"req_[0-9a-f]{12}", text) or [None])[0] if re.search(r"req_[0-9a-f]{12}", text) else None
    sku = (re.search(r"\b[A-Z]{2,}[A-Z0-9]*-[A-Z0-9]+\b", text) or [None])[0] if re.search(r"\b[A-Z]{2,}[A-Z0-9]*-[A-Z0-9]+\b", text) else None
    plan: list[tuple[str, dict]] = []
    reply_override = None
    if re.search(r"notas? de (otro|javiera|carolina|mat[ií]as|ricardo|daniela|sergio|\w+)", t) and "mis notas" not in t:
        other = _other(eng, user)
        plan = [("notes.read_others", {"user_id": other["id"] if other else "x"})]
    elif "nota" in t:
        plan = [("notes.read_own", {})]
    elif re.search(r"aprob", t):
        plan = [("approvals.pending", {})]
    elif re.search(r"estado|resumen|empresa|c[oó]mo vamos", t):
        plan = [("company.summary", {})]
    elif user["role"] == "technician" and re.search(r"necesito|requiero|me falta|hace falta", t) and _collab_candidates(eng, user):
        return _collab_request(eng, orch, user, pr, role, text)
    elif user["role"] == "technician" and re.search(r"necesito|requiero|me falta|hace falta", t) and _already_requested(eng, user):
        plan = []
        reply_override = "Ya registraste un pedido de material para esa visita; administración y el dueño lo están gestionando. No se duplicó nada."
    elif re.search(r"(complet|termin|list[oa]|marc)\w*\s+(la\s+)?tarea\s+(tsk_[0-9a-f]{12})", t):
        tid = re.search(r"tsk_[0-9a-f]{12}", t).group(0)
        ev = re.split(r"evidencia\s*[:\-]?", text, maxsplit=1, flags=re.I)
        evidence = (ev[1] if len(ev) > 1 else text.split(tid, 1)[-1]).strip(" :.-")
        plan = [("tasks.complete", {"task_id": tid, "evidence": evidence})]
    elif re.search(r"\bstock\b|inventario|existencias?|cu[aá]nt\w+\s+(hay|queda)", t):
        skus = list(dict.fromkeys(re.findall(r"\b[A-Z]{2,}[A-Z0-9]*-[A-Z0-9]+\b", text)))[:10]
        plan = [("inventory.check", {"skus": skus})] if skus else [("inventory.list", {})]
    elif re.search(r"compromiso|vencid|plazo", t):
        plan = [("commitments.list", {})]
    elif re.search(r"atenci[oó]n|prioridad|urgente|qu[eé] debo hacer", t):
        plan = [("approvals.pending", {}), ("tasks.list", {}), ("commitments.list", {})]
    elif re.search(r"material|repuesto", t) and sku:
        plan = [("materials.request", {"sku": sku, "qty": 1, "request_id": rid})]
    elif re.search(r"problema|falla|reporta", t) and rid:
        plan = [("issues.report", {"request_id": rid, "description": text[:300]})]
    elif re.search(r"en camino|en curso|completad|terminad|marcar", t) and rid:
        st = "en_camino" if "camino" in t else "completada" if re.search(r"complet|termin", t) else "en_curso"
        plan = [("workorders.update_status", {"request_id": rid, "status": st, "note": "actualizado desde el asistente"})]
    elif re.search(r"tarea|pendiente|recordatorio", t):
        plan = [("tasks.list", {})]
    elif re.search(r"trabajo|asignad|orden", t):
        plan = [("workorders.list_mine", {})] if user["role"] == "technician" else [("requests.list", {})]
    elif re.search(r"solicitud|caso|cliente", t):
        plan = [("requests.list", {})]
    calls, reply = [], []
    if reply_override:
        reply.append(reply_override)
    elif not plan:
        reply.append("Puedo ayudarte con: «qué requiere mi atención», estado, aprobaciones, tareas, compromisos, mis trabajos, solicitudes y mis notas. "
                     "Técnico: «necesito dos cilindros de gas para la visita de Marta», «marcar req_… en camino». "
                     "Completar una tarea: «completar tarea tsk_… evidencia: entregué el material al técnico».")
    for name, args in plan:
        try:
            if name == "tasks.complete":  # goes through the orchestrator so the run continues after the evidence is accepted
                out = orch.complete_task(user, args["task_id"], args["evidence"], role)
            else:
                out = invoke(eng, pr, name, args)
            calls.append({"tool": name, "ok": True, "summary": out.get("_summary", "")})
            reply.append(_fmt(name, out))
        except Denied as e:
            calls.append({"tool": name, "ok": False, "summary": f"DENEGADO: {e}"})
            reply.append(f"No puedo hacerlo: {e}. Solo accedo a lo que tu rol permite.")
        except ToolError as e:
            calls.append({"tool": name, "ok": False, "summary": str(e)})
            reply.append(f"No pude completar la acción: {e}")
    body = "\n".join(reply)
    _log(eng, user, role, text, body, calls)
    return {"reply": body, "tool_calls": calls, "assistant": role}


def _other(eng, user):
    with eng.connect() as c:
        return one(c, "SELECT id FROM users WHERE company_id=:c AND id<>:u LIMIT 1", c=user["company_id"], u=user["id"])


def _collab_candidates(eng, user) -> list[dict]:
    """Technician's own orders whose run is waiting for the technician's material report (scenario D)."""
    with eng.connect() as c:
        return all_(c, """SELECT r.id,cu.name AS customer FROM work_orders w JOIN requests r ON r.id=w.request_id JOIN customers cu ON cu.id=r.customer_id
                          JOIN runs ru ON ru.request_id=r.id AND ru.company_id=w.company_id
                          WHERE w.company_id=:c AND w.technician_id=:u AND ru.status='waiting_technician'""", c=user["company_id"], u=user["id"])


def _collab_request(eng, orch, user, pr, role, text) -> dict:
    mine = _collab_candidates(eng, user)
    with eng.connect() as c:
        catalog = all_(c, "SELECT sku,name FROM parts WHERE company_id=:c", c=user["company_id"])
    need = validate_material_need(orch.provider.parse_material_need(text, catalog, mine), catalog, mine, text)
    if need is None and orch.provider.mode == "llm":  # LLM output rejected by verification → deterministic parser, visibly labelled
        from .providers.deterministic import DeterministicProvider
        need = validate_material_need(DeterministicProvider().parse_material_need(text, catalog, mine), catalog, mine, text)
        if need:
            need.source = "deterministic (fallback tras rechazar la salida del LLM)"
    calls, body = [], ""
    if need is None:
        body = ("No pude identificar con seguridad qué material necesitas o para qué visita. Indica el SKU o el material y, si tienes varios trabajos, el id (req_…). "
                f"Trabajos esperando tu informe: {', '.join(r['id'] + ' (' + r['customer'] + ')' for r in mine)}.")
        calls.append({"tool": "collab.request_material", "ok": False, "summary": "pedido no interpretable"})
    else:
        run = orch.run_for_request(user["company_id"], need.request_id)
        try:
            out = invoke(eng, pr, "collab.request_material", {"request_id": need.request_id, "sku": need.sku, "qty": need.qty, "reason": need.reason}, run_id=run["id"], provider=orch.provider)
            started = False if out["deduplicated"] else orch.continue_collab(user["company_id"], run["id"])
            body = _fmt("collab.request_material", out) if not out["deduplicated"] else "Ese pedido de material ya estaba registrado; no se duplicó nada."
            body += f"\n(Interpretado por: {need.source} · {need.sku} x{need.qty} · {need.request_id})"
            calls.append({"tool": "collab.request_material", "ok": True, "summary": out["_summary"] + (" · administración en curso" if started else "")})
        except (Denied, ToolError) as e:
            body = f"No pude registrar el pedido: {e}"
            calls.append({"tool": "collab.request_material", "ok": False, "summary": str(e)})
    _log(eng, user, role, text, body, calls)
    return {"reply": body, "tool_calls": calls, "assistant": role}


def _log(eng, user, role, text, body, calls):
    with eng.begin() as c:
        conv = one(c, "SELECT id FROM conversations WHERE company_id=:c AND user_id=:u AND channel='assistant'", c=user["company_id"], u=user["id"])
        cid = conv["id"] if conv else uid("cnv_")
        if not conv:
            q(c, "INSERT INTO conversations(id,company_id,user_id,channel,created_at) VALUES(:i,:c,:u,'assistant',:t)", i=cid, c=user["company_id"], u=user["id"], t=now())
        for sender, b, extra in (("user", text, ""), (f"agent:{role}", body, jdump(calls))):
            q(c, "INSERT INTO messages(id,company_id,conversation_id,sender,body,status,created_at) VALUES(:i,:c,:cv,:s,:b,:st,:t)",
              i=uid("msg_"), c=user["company_id"], cv=cid, s=sender, b=b, st=extra or None, t=now())


def _already_requested(eng, user) -> bool:
    with eng.connect() as c:
        return bool(one(c, "SELECT 1 AS x FROM material_requests WHERE company_id=:c AND requested_by=:u AND idem_key LIKE 'collab:%' LIMIT 1", c=user["company_id"], u=user["id"]))
