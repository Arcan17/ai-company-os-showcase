"""V0.8 (spec 003) — control de datos de clientes: quién ve teléfono, correo y dirección.

Regla única: una persona cuyo rol no tiene `customers.read` (hoy, el técnico/a) NUNCA recibe los datos de contacto de un cliente, ni siquiera dentro de los eventos de una
ejecución, las tareas o los mensajes preparados. Se enmascara en el servidor, al leer (los eventos guardados no se tocan: la auditoría conserva el original).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine

import json
from datetime import timedelta

from sqlalchemy import String, Text, text

from . import clock
from .db import all_, md, now, one, q
from .permissions import ROLE_PERMISSIONS

MASK = "[oculto]"
CONTACT_KEYS = {"phone", "email", "address", "telefono", "teléfono", "correo", "direccion", "dirección"}
DEDUP_SECONDS = 600   # una persona consultando la misma ficha (o con la pantalla abierta, que se actualiza sola) deja UNA entrada cada 10 minutos
MIN_LEN = 5   # no enmascarar valores triviales (evita falsos positivos); un teléfono o correo real es más largo


def can_see_contact(role: str) -> bool:
    return "customers.read" in ROLE_PERMISSIONS.get(role, set())


def contact_values(eng: Engine, company_id: str) -> list[str]:
    """Valores de contacto REALES de los clientes de esta empresa (más largos primero para que un correo no quede a medias)."""
    with eng.connect() as c:
        rows = all_(c, "SELECT phone,email,address FROM customers WHERE company_id=:c", c=company_id)
    vals = {str(v).strip() for r in rows for v in (r["phone"], r["email"], r["address"]) if v and len(str(v).strip()) >= MIN_LEN}
    return sorted(vals, key=len, reverse=True)


def _walk(o: Any, vals: list[str]) -> Any:
    if isinstance(o, dict):
        return {k: (MASK if isinstance(k, str) and k.lower() in CONTACT_KEYS and v else _walk(v, vals)) for k, v in o.items()}
    if isinstance(o, list):
        return [_walk(x, vals) for x in o]
    if isinstance(o, str):
        for v in vals:
            if v in o:
                o = o.replace(v, MASK)
        return o
    return o


def scrub(eng: Engine, user: dict, payload: Any) -> Any:
    """Devuelve `payload` sin datos de contacto si el rol del usuario no puede verlos; si puede, lo devuelve igual."""
    if can_see_contact(user["role"]):
        return payload
    return _walk(payload, contact_values(eng, user["company_id"]))


# ───────────────────────── registro de consultas, exportación y anonimización (spec 003, historias 2 y 3) ─────────────────────────
ANON = "[anonimizado]"
HISTORIC_REQUEST = ("cerrada", "ejecucion_fallida", "presupuesto_rechazado")
ACTIVE_RUN = ("running", "waiting_approval", "waiting_technician", "waiting_task", "blocked")


def log_view(conn, company_id: str, actor_id: str, customer_id: str, fields: str = "name,phone,email,address") -> None:
    """Quién consultó la ficha de un cliente. NO guarda el dato personal: solo el id del cliente y los CAMPOS que se entregaron."""
    from .events import audit
    if one(conn, "SELECT 1 AS x FROM audit_log WHERE company_id=:c AND actor_id=:a AND action='customer.view' AND resource=:r AND ts>:t", c=company_id, a=actor_id, r=customer_id,
           t=(clock.utcnow() - timedelta(seconds=DEDUP_SECONDS)).isoformat(timespec="milliseconds")):
        return   # la misma persona recargando la misma ficha no llena el registro
    audit(conn, company_id, "user", actor_id, "customer.view", customer_id, "ok", f"campos: {fields}")


def access_log(eng: Engine, company_id: str, customer_id: str) -> list[dict] | None:
    with eng.connect() as c:
        if not one(c, "SELECT 1 AS x FROM customers WHERE id=:i AND company_id=:c", i=customer_id, c=company_id):
            return None
        return all_(c, """SELECT a.ts, a.actor_id AS user_id, u.name AS actor, u.role AS role, a.detail FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id AND u.company_id=a.company_id
                          WHERE a.company_id=:c AND a.action='customer.view' AND a.resource=:i ORDER BY a.id DESC LIMIT 200""", c=company_id, i=customer_id)


def export_customer(eng: Engine, company_id: str, customer_id: str) -> dict | None:
    with eng.connect() as c:
        cu = one(c, "SELECT id,name,phone,email,address,created_at FROM customers WHERE id=:i AND company_id=:c", i=customer_id, c=company_id)
        if not cu:
            return None
        reqs = all_(c, "SELECT id,description,category,urgency,status,created_at FROM requests WHERE company_id=:c AND customer_id=:k ORDER BY created_at", c=company_id, k=customer_id)
        ids = [r["id"] for r in reqs]
        convs = all_(c, "SELECT id,channel,created_at FROM conversations WHERE company_id=:c AND customer_id=:k", c=company_id, k=customer_id)
        msgs = [m for cv in convs for m in all_(c, "SELECT sender,body,status,created_at FROM messages WHERE company_id=:c AND conversation_id=:v ORDER BY created_at", c=company_id, v=cv["id"])]
        quotes = [x for i in ids for x in all_(c, "SELECT request_id,kind,total,status,lines FROM quotes WHERE company_id=:c AND request_id=:r", c=company_id, r=i)]
        visits = [x for i in ids for x in all_(c, "SELECT request_id,status,slot_start,customer_confirmed,confirmed_at FROM visits WHERE company_id=:c AND request_id=:r", c=company_id, r=i)]
    for qd in quotes:
        try:
            qd["lines"] = json.loads(qd["lines"])
        except Exception:
            pass
    return {"customer": cu, "requests": reqs, "messages": msgs, "quotes": quotes, "visits": visits, "exported_at": now()}


def _needles(cu: dict) -> list[str]:
    out = set()
    for key in ("name", "phone", "email", "address"):
        v = (cu.get(key) or "").strip()
        if len(v) >= MIN_LEN:
            out.add(v); out.add(json.dumps(v)[1:-1])      # también la forma escapada que usan los campos JSON (acentos como \u00e1)
    return sorted(out, key=len, reverse=True)


def anonymize_customer(eng: Engine, company_id: str, actor: dict, customer_id: str, confirm_name: str) -> dict:
    """Reemplaza el dato personal del cliente por «[anonimizado]» en TODAS las tablas de la empresa y conserva montos, fechas y estados (spec 003 FR-009).
    Devuelve {"ok": True, ...} o {"error": codigo}. Se niega con trabajo activo y si no se confirma escribiendo el nombre exacto."""
    with eng.begin() as c:
        cu = one(c, "SELECT * FROM customers WHERE id=:i AND company_id=:c", i=customer_id, c=company_id)
        if not cu:
            return {"error": "not_found"}
        if (confirm_name or "").strip() != cu["name"].strip():
            return {"error": "confirm_mismatch"}
        marks = ",".join(f":h{i}" for i in range(len(HISTORIC_REQUEST)))
        hp = {f"h{i}": v for i, v in enumerate(HISTORIC_REQUEST)}
        active = one(c, f"SELECT 1 AS x FROM requests WHERE company_id=:c AND customer_id=:k AND status NOT IN ({marks})", c=company_id, k=customer_id, **hp)
        if active:
            return {"error": "active_work"}
        needles = _needles(cu)
        touched = 0
        for t in md.sorted_tables:
            if "company_id" not in t.c or t.name in ("customers", "users", "identities", "invitations", "sessions", "agents"):
                continue
            for col in t.c:
                if isinstance(col.type, (String, Text)) and not col.primary_key and col.name not in ("company_id", "idem_key", "token", "token_hash"):
                    for i, nd in enumerate(needles):
                        r = c.execute(text(f"UPDATE {t.name} SET {col.name}=REPLACE({col.name}, :n, :a) WHERE company_id=:c AND {col.name} LIKE :l"), {"n": nd, "a": ANON, "c": company_id, "l": f"%{nd}%"})
                        touched += r.rowcount or 0
        q(c, "UPDATE customers SET name=:a, phone=NULL, email=NULL, address=NULL WHERE id=:i AND company_id=:c", a="Cliente anonimizado", i=customer_id, c=company_id)
        from .events import audit
        audit(c, company_id, "user", actor["id"], "customer.anonymize", customer_id, "ok", f"filas tocadas: {touched}")
    return {"ok": True, "touched": touched}
