"""Commercial-demo fixture: a NEW, separate SQLite with 100 % fictitious data prepared for the guided presentation.

Built through the real tool registry (agent principals, validation, idempotency keys, audit) on top of the normal seed, so every row is
consistent with what the engine would create. Idempotent: running it twice never duplicates anything.
Safety: refuses any database file whose name does not start with `demo_comercial` (the lab DB `aicos.db` can never be touched from here).

  python -m app.demo_seed --db backend/data/demo_comercial.sqlite3        # create/seed (idempotent) and print a summary
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from sqlalchemy import text

from .db import jdump, now, one, q, uid
from .providers.deterministic import DeterministicProvider
from .seed import seed, sync_agents

DEMO_PREFIX = "demo_comercial"
MARTA_TEXT = "Hola, quiero una mantención preventiva de mi split del living antes del invierno. Me acomoda cualquier día de la próxima semana."
PEDRO_TEXT = "Mi aire split no enfría desde ayer, creo que le falta gas."
ANDES_TEXT = "Instalación de un split en la oficina (histórico de demostración)."
INSTRUCTION = "Revisa la mantención pendiente de Marta Rojas; pide a administración que verifique materiales y al técnico que confirme después de administración."
FORBIDDEN_TRY = "Compra repuestos y avisa a todos por WhatsApp."


class DemoSafetyError(RuntimeError):
    pass


def assert_demo_path(path_or_url: str) -> str:
    """Return the file path if (and only if) it is a commercial-demo database."""
    path = path_or_url.replace("sqlite:///", "")
    name = os.path.basename(path)
    if not name.startswith(DEMO_PREFIX) or name.endswith(("-wal", "-shm")):
        raise DemoSafetyError(f"«{name}» no es una base de la demo comercial (debe llamarse {DEMO_PREFIX}*): me niego a tocarla")
    return path


def _principal(orch, cid: str, role: str):
    return orch.principal(cid, role)


def build(eng) -> dict:
    """Idempotently create the commercial dataset in `eng` and return a summary."""
    from .engine.orchestrator import Orchestrator
    from .tools import invoke
    seed(eng)
    sync_agents(eng)
    with eng.connect() as c:  # fixture already complete (marker written LAST): a restart must not write anything
        if one(c, "SELECT 1 AS x FROM audit_log WHERE action='demo.fixture'"):
            return summary(eng)
    orch = Orchestrator(eng)
    orch.provider = DeterministicProvider()      # the demo is deterministic by default: no LLM, no network
    prov = orch.provider
    with eng.begin() as c:
        co = one(c, "SELECT id FROM companies WHERE slug='clima-sur'")
        cid = co["id"]
        if not one(c, "SELECT 1 AS x FROM parts WHERE company_id=:c AND sku='KIT-MANT'", c=cid):
            q(c, "INSERT INTO parts(id,company_id,sku,name,stock,unit_price,alt_sku) VALUES(:i,:c,'KIT-MANT','Kit de mantención split (filtros + limpieza)',6,12000,NULL)", i=uid("prt_"), c=cid)
        cust = {r["name"]: r["id"] for r in q(c, "SELECT id,name FROM customers WHERE company_id=:c", c=cid).mappings().all()}
        tech = one(c, "SELECT id FROM users WHERE company_id=:c AND role='technician'", c=cid)["id"]
        slot = one(c, "SELECT id FROM availability WHERE company_id=:c AND status='free' ORDER BY slot_start LIMIT 1", c=cid)

    def customer_message(name: str, body: str, key: str) -> str:
        with eng.begin() as c:
            ex = one(c, "SELECT conversation_id FROM messages WHERE company_id=:c AND idem_key=:k", c=cid, k=f"in:{key}")
            if ex:
                return ex["conversation_id"]
            conv = uid("cnv_")
            q(c, "INSERT INTO conversations(id,company_id,customer_id,channel,token,created_at) VALUES(:i,:c,:k,'chat_web',:t,:n)", i=conv, c=cid, k=cust[name], t=uid("tok_") + uid(), n=now())
            q(c, "INSERT INTO messages(id,company_id,conversation_id,sender,body,status,idem_key,created_at) VALUES(:i,:c,:cv,'customer',:b,'recibido',:k,:n)", i=uid("msg_"), c=cid, cv=conv, b=body, k=f"in:{key}", n=now())
            return conv

    rec, coo, adm = _principal(orch, cid, "reception"), _principal(orch, cid, "coordinator"), _principal(orch, cid, "admin_assistant")

    def request(name: str, body: str, key: str) -> str:
        conv = customer_message(name, body, key)
        out = invoke(eng, rec, "requests.create", {"customer_id": cust[name], "description": body, "idem_key": f"demo:{key}", "conversation_id": conv})
        invoke(eng, coo, "requests.classify", {"request_id": out["request_id"]}, provider=prov)
        return out["request_id"]

    # 1) Marta Rojas — EXACTLY ONE open maintenance request (the fast path of the guided demo): classified, draft quote with a real part (stock 6)
    r_marta = request("Marta Rojas", MARTA_TEXT, "marta-mantencion")
    invoke(eng, adm, "quotes.prepare", {"request_id": r_marta, "service_codes": ["MANT"], "part_skus": ["KIT-MANT"]})
    # 2) Pedro Gálvez — gas repair with the visit PROPOSED (not confirmed) and a customer message PREPARED, NOT SENT (simulated channel)
    r_pedro = request("Pedro Gálvez", PEDRO_TEXT, "pedro-gas")
    invoke(eng, adm, "quotes.prepare", {"request_id": r_pedro, "service_codes": ["REP-GAS"], "part_skus": ["GAS-R410"]})
    invoke(eng, adm, "quotes.set_status", {"request_id": r_pedro, "status": "aprobado"})
    if slot:
        v = invoke(eng, adm, "visits.propose", {"request_id": r_pedro, "availability_id": slot["id"]})
        invoke(eng, adm, "workorders.create", {"request_id": r_pedro, "technician_id": tech})
        body = prov.draft_customer_message("visit_proposal", {"customer": "Pedro Gálvez", "slot": v["slot_start"].replace("T", " "), "total": 70000})
        invoke(eng, rec, "messages.prepare_customer", {"request_id": r_pedro, "kind": "visit_proposal", "body": body})
    invoke(eng, coo, "requests.update_status", {"request_id": r_pedro, "status": "propuesta_preparada"})
    # 3) Oficina Andes — closed history (so the dashboard separates active from historical)
    r_hist = request("Oficina Andes SpA", ANDES_TEXT, "andes-historico")
    invoke(eng, coo, "requests.update_status", {"request_id": r_hist, "status": "cerrada"})
    with eng.begin() as c:
        old = "2026-07-20T10:00:00.000+00:00"
        q(c, "UPDATE requests SET created_at=:o,updated_at=:o,category='instalacion' WHERE id=:i AND company_id=:c", o=old, i=r_hist, c=cid)
        if not one(c, "SELECT 1 AS x FROM audit_log WHERE company_id=:c AND action='demo.fixture'", c=cid):
            q(c, "INSERT INTO audit_log(company_id,ts,actor_type,actor_id,action,resource,outcome,detail) VALUES(:c,:t,'system','demo_seed','demo.fixture',:s,'ok',:d)", c=cid, t=now(), s="demo_comercial", d=jdump({"fixture": "v0.4-demo-comercial", "fictitious": True}))
    return summary(eng)


def summary(eng) -> dict:
    with eng.connect() as c:
        cid = one(c, "SELECT id FROM companies WHERE slug='clima-sur'")["id"]
        def n(sql, **p): return one(c, sql, c=cid, **p)["n"]
        return {
            "requests": n("SELECT COUNT(*) AS n FROM requests WHERE company_id=:c"),
            "open_requests": n("SELECT COUNT(*) AS n FROM requests WHERE company_id=:c AND status NOT IN ('cerrada','ejecucion_fallida','presupuesto_rechazado')"),
            "open_marta_maintenance": n("SELECT COUNT(*) AS n FROM requests r JOIN customers cu ON cu.id=r.customer_id WHERE r.company_id=:c AND cu.name='Marta Rojas' AND r.category='mantencion' AND r.status NOT IN ('cerrada','ejecucion_fallida','presupuesto_rechazado')"),
            "runs": n("SELECT COUNT(*) AS n FROM runs WHERE company_id=:c"), "failed_runs": n("SELECT COUNT(*) AS n FROM runs WHERE company_id=:c AND status='failed'"),
            "plans": n("SELECT COUNT(*) AS n FROM plans WHERE company_id=:c"), "tasks": n("SELECT COUNT(*) AS n FROM tasks WHERE company_id=:c"),
            "pending_approvals": n("SELECT COUNT(*) AS n FROM approvals WHERE company_id=:c AND status='pendiente'"),
            "visits_proposed": n("SELECT COUNT(*) AS n FROM visits WHERE company_id=:c AND status='propuesta' AND customer_confirmed=0"),
            "visits_confirmed": n("SELECT COUNT(*) AS n FROM visits WHERE company_id=:c AND customer_confirmed=1"),
            "messages_sent": one(c, "SELECT COUNT(*) AS n FROM messages WHERE status='enviado'")["n"],
            "messages_prepared_not_sent": n("SELECT COUNT(*) AS n FROM messages WHERE company_id=:c AND status='preparado_no_enviado'"),
            "users": n("SELECT COUNT(*) AS n FROM users WHERE company_id=:c"), "kit_mant_stock": one(c, "SELECT stock AS n FROM parts WHERE company_id=:c AND sku='KIT-MANT'", c=cid)["n"],
            "gas_stock": one(c, "SELECT stock AS n FROM parts WHERE company_id=:c AND sku='GAS-R410'", c=cid)["n"],
        }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="ruta del archivo SQLite de la demo (debe llamarse demo_comercial*)")
    a = ap.parse_args(argv)
    path = assert_demo_path(a.db)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    from .db import make_engine
    eng = make_engine(f"sqlite:///{path}")
    print(json.dumps({"db": os.path.basename(path), **build(eng)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DemoSafetyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
