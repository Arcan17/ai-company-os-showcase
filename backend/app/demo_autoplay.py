"""Plays the guided-demo script (scenario E) through the REAL API, in-process, as the three fictitious users — used for the plan-B database and for tests.
Nothing is faked: it is the same HTTP surface the browser uses (auth cookie, assistant chat, task completion, plan confirmation).

  python -m app.demo_autoplay --db backend/data/demo_comercial_planb.sqlite3     # seeds (idempotent) and plays the script once
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from .demo_seed import FORBIDDEN_TRY, INSTRUCTION, DemoSafetyError, assert_demo_path

EVIDENCE_ADMIN = "Revisé el inventario: hay 6 kits de mantención (necesito 1) y las herramientas están completas para la visita de Marta."
EVIDENCE_TECH = "Confirmado: llevo el kit verificado por administración y puedo realizar la mantención de Marta Rojas."


def play(app, instruction: str = INSTRUCTION) -> dict:
    from fastapi.testclient import TestClient
    from .seed import DEMO_PASSWORD

    def login(email):
        c = TestClient(app)
        assert c.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD}).status_code == 200
        return c
    owner, admin, tech = login("dueno@climasur.test"), login("admin@climasur.test"), login("tecnico@climasur.test")
    out: dict = {}
    r = owner.post("/api/plans", json={"text": instruction, "client_plan_id": "demo-" + uuid.uuid4().hex[:10]}).json()
    plan = r["plan"]; pid = plan["id"]
    out.update(plan_id=pid, proposed_status=plan["status"], proposed_steps=[s["action"] for s in plan["steps"]], request_id=plan["request_id"])
    out["tasks_before_confirm"] = len([t for t in admin.get("/api/tasks").json()["tasks"] if t["kind"] == "plan_step"])
    out["confirm"] = owner.post(f"/api/plans/{pid}/confirm").json()
    atask = [t for t in admin.get("/api/tasks").json()["tasks"] if t["kind"] == "plan_step"][0]
    out["admin_stock_reply"] = admin.post("/api/assistant/chat", json={"message": "¿Cuánto stock hay de KIT-MANT?"}).json()["reply"]
    out["admin_close"] = admin.post("/api/assistant/chat", json={"message": f"completar tarea {atask['id']} evidencia: {EVIDENCE_ADMIN}"}).json()["reply"]
    ttask = [t for t in tech.get("/api/tasks").json()["tasks"] if t["kind"] == "plan_step"][0]
    out["tech_close"] = tech.post("/api/assistant/chat", json={"message": f"completar tarea {ttask['id']} evidencia: {EVIDENCE_TECH}"}).json()["reply"]
    final = owner.get(f"/api/plans/{pid}").json()
    out.update(final_status=final["status"], result=final["result_summary"], events=len(owner.get(f"/api/plans/{pid}/events").json()["events"]))
    blocked = owner.post("/api/plans", json={"text": FORBIDDEN_TRY, "client_plan_id": "demo-" + uuid.uuid4().hex[:10]}).json()["plan"]
    out.update(forbidden_plan_id=blocked["id"], forbidden_status=blocked["status"], forbidden_steps=len(blocked["steps"]), forbidden_excluded=[e["code"] for e in blocked["excluded"]])
    out["ok"] = (out["proposed_status"] == "awaiting_user_confirmation" and out["tasks_before_confirm"] == 0 and out["final_status"] == "completed"
                 and out["forbidden_status"] == "blocked" and out["forbidden_steps"] == 0)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    a = ap.parse_args(argv)
    path = assert_demo_path(a.db)
    from .config import settings
    settings.sync_runs, settings.step_delay, settings.demo_mode = True, 0, True
    from .api import create_app
    from .db import make_engine
    from .demo_seed import build
    build(make_engine(f"sqlite:///{path}"))
    app = create_app(f"sqlite:///{path}")
    res = play(app)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DemoSafetyError as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(2)
