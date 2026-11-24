"""Fictitious demo data (NO real people). Two isolated tenants: Clima Sur (HVAC) and Motor Norte (auto workshop)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Engine

from . import pilot
from .db import init_db, jdump, md, now, one, q, uid
from .permissions import AGENT_META, AGENT_PERMISSIONS
from .security import hash_password

DEMO_PASSWORD = "demo1234"  # fictitious demo accounts only; shown on the login screen on purpose

COMPANIES = {
    "clima-sur": dict(name="Clima Sur", industry="Instalación y mantenimiento de aire acondicionado", dom="climasur.test",
                      people=[("owner", "Carolina Vega", "dueno"), ("admin", "Javiera Soto", "admin"), ("technician", "Matías Fuentes", "tecnico")],
                      customers=[("Marta Rojas", "+56 9 5555 0101", "marta.rojas@example.test", "Av. Los Alerces 123, depto 45 (ficticia)"),
                                 ("Pedro Gálvez", "+56 9 5555 0102", "pedro.galvez@example.test", "Pasaje Los Olmos 88 (ficticia)"),
                                 ("Oficina Andes SpA", "+56 2 5555 0103", "contacto@oficinaandes.example.test", "Calle Ficticia 900, of. 12")],
                      parts=[("GAS-R410", "Gas refrigerante R410A (carga)", 12, 18000, None), ("MOTOR-VENT", "Motor ventilador unidad exterior", 3, 64000, None),
                             ("PLACA-INV", "Placa electrónica inverter original", 0, 135000, "PLACA-UNIV"), ("PLACA-UNIV", "Placa universal compatible", 2, 98000, None),
                             ("COMPRESOR", "Compresor split 12.000 BTU", 0, 320000, None)],
                      services=[("DIAG", "diagnostico", "Visita de diagnóstico", 25000), ("REP-GAS", "reparacion_gas", "Mano de obra recarga de gas", 45000),
                                ("REP-ELEC", "reparacion_electronica", "Mano de obra reparación electrónica", 55000),
                                ("REP-VENT", "reparacion_ventilador", "Mano de obra cambio de ventilador", 40000),
                                ("MANT", "mantencion", "Mantención preventiva split", 35000), ("INSTAL", "instalacion", "Instalación split", 120000)]),
    "motor-norte": dict(name="Motor Norte", industry="Taller mecánico", dom="motornorte.test",
                        people=[("owner", "Ricardo Paredes", "dueno"), ("admin", "Daniela Ruiz", "admin"), ("technician", "Sergio Mora", "tecnico")],
                        customers=[("Luis Herrera", "+56 9 5555 0201", "luis.herrera@example.test", "Av. Ficticia 456"),
                                   ("Ana Cortés", "+56 9 5555 0202", "ana.cortes@example.test", "Calle Imaginaria 77")],
                        parts=[("PAST-FRENO", "Pastillas de freno (juego)", 8, 32000, None), ("ACEITE-5W30", "Aceite 5W30 4L", 20, 28000, None)],
                        services=[("DIAG", "diagnostico", "Revisión de diagnóstico", 20000), ("REP-FRENOS", "reparacion_frenos", "Mano de obra cambio de frenos", 38000),
                                  ("MANT-ACEITE", "mantencion", "Cambio de aceite y filtro", 18000)]),
}
NOTES = {"owner": "Revisar margen de instalaciones antes del cierre de mes (nota privada del dueño).",
         "admin": "Recordar negociar descuento con proveedor de repuestos (nota privada de administración).",
         "technician": "Mi herramienta de vacío necesita calibración (nota privada del técnico)."}


def add_slots(c, company_id: str, technician_id: str) -> int:
    """Free visit slots for a technician: the next 4 days × (09:00, 11:00, 15:00). Used by the seed AND when a technician is added from the configuration."""
    base = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
    n = 0
    for day in range(0, 4):
        for hour in (9, 11, 15):
            st = base + timedelta(days=day, hours=hour - 9)
            q(c, "INSERT INTO availability(id,company_id,technician_id,slot_start,slot_end,status) VALUES(:i,:c,:t,:s,:e,'free')",
              i=uid("slt_"), c=company_id, t=technician_id, s=st.strftime("%Y-%m-%dT%H:%M"), e=(st + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            n += 1
    return n


def seed(eng: Engine) -> None:
    init_db(eng)
    if pilot.is_pilot():      # el piloto NUNCA siembra la demostración (base vacía: las empresas las da de alta el operador con la CLI); vale aunque otro código llame a seed() directamente
        return
    with eng.begin() as c:
        if one(c, "SELECT 1 AS x FROM companies LIMIT 1"):
            return
        for slug, d in COMPANIES.items():
            cid = uid("co_")
            q(c, "INSERT INTO companies(id,slug,name,industry,created_at) VALUES(:i,:s,:n,:in,:t)", i=cid, s=slug, n=d["name"], **{"in": d["industry"]}, t=now())
            users = {}
            for role, name, local in d["people"]:
                u = uid("usr_")
                users[role] = u
                q(c, "INSERT INTO users(id,company_id,email,name,role,password_hash,is_demo,created_at) VALUES(:i,:c,:e,:n,:r,:p,1,:t)",
                  i=u, c=cid, e=f"{local}@{d['dom']}", n=name, r=role, p=hash_password(DEMO_PASSWORD), t=now())
                q(c, "INSERT INTO private_notes(id,company_id,user_id,body,created_at) VALUES(:i,:c,:u,:b,:t)", i=uid("not_"), c=cid, u=u, b=NOTES[role], t=now())
            for arole, perms in AGENT_PERMISSIONS.items():
                nm, desc, urole = AGENT_META[arole]
                q(c, "INSERT INTO agents(id,company_id,role,name,description,user_id,permissions,config,enabled) VALUES(:i,:c,:r,:n,:d,:u,:p,:cf,1)",
                  i=uid("agt_"), c=cid, r=arole, n=nm, d=desc, u=users.get(urole) if urole else None, p=jdump(sorted(perms)), cf=jdump({"provider": "configurable"}))
            cust_ids = []
            for n, ph, em, ad in d["customers"]:
                k = uid("cus_")
                cust_ids.append(k)
                q(c, "INSERT INTO customers(id,company_id,name,phone,email,address,created_at) VALUES(:i,:c,:n,:p,:e,:a,:t)", i=k, c=cid, n=n, p=ph, e=em, a=ad, t=now())
            for sku, nm, st, pr, alt in d["parts"]:
                q(c, "INSERT INTO parts(id,company_id,sku,name,stock,unit_price,alt_sku) VALUES(:i,:c,:s,:n,:st,:p,:a)", i=uid("prt_"), c=cid, s=sku, n=nm, st=st, p=pr, a=alt)
            for code, cat, nm, pr in d["services"]:
                q(c, "INSERT INTO services(id,company_id,code,category,name,price) VALUES(:i,:c,:cd,:ca,:n,:p)", i=uid("svc_"), c=cid, cd=code, ca=cat, n=nm, p=pr)
            add_slots(c, cid, users["technician"])
            # one closed historical request so "consultar historial" returns real data
            past = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat(timespec="milliseconds")
            q(c, "INSERT INTO requests(id,company_id,customer_id,description,category,urgency,status,idem_key,created_at,updated_at) VALUES(:i,:c,:k,:d,:cat,'media','cerrada','seed-hist-1',:t,:t)",
              i=uid("req_"), c=cid, k=cust_ids[0], d="Mantención anual (histórico de demostración)", cat="mantencion", t=past)
            for kind, nm in [("whatsapp", "WhatsApp Business"), ("email", "Correo electrónico"), ("calendar", "Calendario"), ("voice", "Telefonía / voz"), ("payments", "Pagos")]:
                q(c, "INSERT INTO integrations(id,company_id,kind,name,mode,enabled,note) VALUES(:i,:c,:k,:n,'simulado',0,'No conectado en esta versión (demo local)')", i=uid("int_"), c=cid, k=kind, n=nm)


def reset_company_scenarios(eng: Engine, slug: str, backup_path: str | None = None) -> dict:
    """Explicit, scoped reset of ONE company's scenario data (chat-created requests and everything hanging from them).
    Seed customers/history/users/agents survive; part stock and availability go back to seed values. Other companies are untouched."""
    import shutil
    from datetime import datetime
    if backup_path:
        shutil.copy(backup_path, f"{backup_path}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    d = COMPANIES[slug]
    with eng.begin() as c:
        co = one(c, "SELECT id FROM companies WHERE slug=:s", s=slug)
        cid = co["id"]
        seed_names = [x[0] for x in d["customers"]]
        extra = [r["id"] for r in q(c, "SELECT id,name FROM customers WHERE company_id=:c", c=cid).mappings().all() if r["name"] not in seed_names]   # customers created during scenarios (chat OR manual registration)
        mark = ",".join(f"'{i}'" for i in extra) or "''"
        # Scenario data = requests born in the customer chat OR registered manually OR belonging to a non-seed customer; their conversations (chat_web / interno) and messages;
        # every run AND every coordinator plan of the company (their events/tasks/delegations/results live under the plan id). No dangling reference may survive.
        reqs = [r["id"] for r in q(c, f"SELECT id FROM requests WHERE company_id=:c AND (idem_key LIKE 'chat:%' OR idem_key LIKE 'manual:%' OR customer_id IN ({mark}))", c=cid).mappings().all()]
        runs_ = [r["id"] for r in q(c, "SELECT id FROM runs WHERE company_id=:c", c=cid).mappings().all()]
        plans_ = [r["id"] for r in q(c, "SELECT id FROM plans WHERE company_id=:c", c=cid).mappings().all()]
        convs = [r["id"] for r in q(c, f"SELECT id FROM conversations WHERE company_id=:c AND (channel IN ('chat_web','interno') OR customer_id IN ({mark}))", c=cid).mappings().all()]
        n = {}
        def delete(table, col, ids):
            for i in ids:
                n[table] = n.get(table, 0) + q(c, f"DELETE FROM {table} WHERE company_id=:c AND {col}=:i", c=cid, i=i).rowcount
        owners = runs_ + plans_
        delete("events", "run_id", owners); delete("results", "run_id", owners); delete("approvals", "run_id", runs_); delete("delegations", "run_id", owners)
        delete("tasks", "run_id", owners); delete("commitments", "run_id", runs_); delete("material_requests", "run_id", runs_)
        delete("plan_steps", "plan_id", plans_); delete("plans", "id", plans_)
        for t in ("tasks", "commitments", "material_requests", "visits", "quotes", "work_orders", "results", "approvals"):
            delete(t, "request_id", reqs)
        delete("runs", "id", runs_); delete("messages", "conversation_id", convs); delete("conversations", "id", convs); delete("requests", "id", reqs)
        for cu_id in extra:
            n["customers"] = n.get("customers", 0) + q(c, "DELETE FROM customers WHERE id=:i AND company_id=:c", i=cu_id, c=cid).rowcount
        q(c, "UPDATE availability SET status='free',request_id=NULL WHERE company_id=:c", c=cid)
        for sku, _nm, st, _pr, _alt in d["parts"]:
            q(c, "UPDATE parts SET stock=:s WHERE company_id=:c AND sku=:k", s=st, c=cid, k=sku)
        q(c, "INSERT INTO audit_log(company_id,ts,actor_type,actor_id,action,resource,outcome,detail) VALUES(:c,:t,'user','owner','demo.reset_scenarios',:s,'ok',:d)", c=cid, t=now(), s=slug, d=jdump(n))
    return {"deleted": n, "requests": len(reqs), "runs": len(runs_), "plans": len(plans_)}


def sync_agents(eng: Engine) -> int:
    """Agent TEMPLATES are code-defined (permissions.AGENT_PERMISSIONS): every existing agent row — shared or personal, any number per company — gets the template's
    current permission set, so an upgraded DB picks up new permissions. Shared agents (reception, coordinator) are ensured per company. Personal assistants are NEVER
    auto-created for new employees (assigning them is an explicit, audited configuration step) — only repaired for legacy companies that have none for a role and
    exactly one active employee of it. Never touches users or data."""
    changed = 0
    with eng.begin() as c:
        for co in q(c, "SELECT id FROM companies").mappings().all():
            # Personal assistants are only ever REPAIRED for an unmistakably legacy company: never configured (no `config.*` audit) and with no personal assistant at all.
            # A company configured from the UI keeps exactly the assignments its owner made, restart after restart.
            legacy = not one(c, "SELECT 1 AS x FROM audit_log WHERE company_id=:c AND action LIKE 'config.%' LIMIT 1", c=co["id"]) and not one(c, "SELECT 1 AS x FROM agents WHERE company_id=:c AND user_id IS NOT NULL LIMIT 1", c=co["id"])
            users_by_role: dict[str, list[str]] = {}
            for r in q(c, "SELECT id,role FROM users WHERE company_id=:c AND COALESCE(active,1)=1", c=co["id"]).mappings().all():
                users_by_role.setdefault(r["role"], []).append(r["id"])
            for arole, perms in AGENT_PERMISSIONS.items():
                want = jdump(sorted(perms))
                nm, desc, urole = AGENT_META[arole]
                rows = q(c, "SELECT id,permissions FROM agents WHERE company_id=:c AND role=:r", c=co["id"], r=arole).mappings().all()
                if not rows and (urole is None or (legacy and len(users_by_role.get(urole, [])) == 1)):
                    q(c, "INSERT INTO agents(id,company_id,role,name,description,user_id,permissions,config,enabled) VALUES(:i,:c,:r,:n,:d,:u,:p,:cf,1)",
                      i=uid("agt_"), c=co["id"], r=arole, n=nm, d=desc, u=users_by_role[urole][0] if urole else None, p=want, cf=jdump({"provider": "configurable"}))
                    changed += 1
                for row in rows:
                    if row["permissions"] != want:
                        q(c, "UPDATE agents SET permissions=:p WHERE id=:i AND company_id=:c", p=want, i=row["id"], c=co["id"])
                        changed += 1
    return changed
