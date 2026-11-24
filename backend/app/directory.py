"""Company directory (V0.5): who can receive work, derived from REAL configuration — employees (role, active) and their personal assistants
(agents rows created from templates). Nothing here guesses «the first administrator»: eligibility is explicit and workload is visible.

An employee is ELIGIBLE for a role's work only if: user.active AND user.role == role AND an ENABLED personal assistant of the matching template is assigned to them.
"""
from __future__ import annotations

from .db import all_, one

TEMPLATE_BY_ROLE = {"owner": "owner_assistant", "admin": "admin_assistant", "technician": "tech_assistant"}
ROLE_BY_TEMPLATE = {v: k for k, v in TEMPLATE_BY_ROLE.items()}
OPEN_TASK = ("pendiente", "en_espera")


def eligible_staff(conn, company_id: str, role: str) -> list[dict]:
    """Eligible employees of `role`, least-loaded first (ties: oldest account), each with its open-task workload."""
    tpl = TEMPLATE_BY_ROLE[role]
    rows = all_(conn, """SELECT u.id,u.name,u.role,u.email,a.id AS agent_id,
                                (SELECT COUNT(*) FROM tasks t WHERE t.company_id=u.company_id AND t.assignee_user_id=u.id AND t.status IN ('pendiente','en_espera')) AS workload
                         FROM users u JOIN agents a ON a.company_id=u.company_id AND a.user_id=u.id AND a.role=:tpl AND a.enabled=1
                         WHERE u.company_id=:c AND u.role=:r AND COALESCE(u.active,1)=1""", c=company_id, r=role, tpl=tpl)
    return sorted(rows, key=lambda r: (r["workload"], r["name"].lower(), r["id"]))


def ineligible_staff(conn, company_id: str, role: str) -> list[dict]:
    """Employees of `role` that exist but cannot receive work, with the honest reason (inactive / no assistant / assistant disabled)."""
    tpl = TEMPLATE_BY_ROLE[role]
    out = []
    for u in all_(conn, "SELECT id,name,COALESCE(active,1) AS active FROM users WHERE company_id=:c AND role=:r", c=company_id, r=role):
        a = one(conn, "SELECT enabled FROM agents WHERE company_id=:c AND user_id=:u AND role=:t", c=company_id, u=u["id"], t=tpl)
        if not u["active"]:
            out.append({**u, "reason": "está desactivado/a"})
        elif not a:
            out.append({**u, "reason": "no tiene un asistente personal asignado"})
        elif not a["enabled"]:
            out.append({**u, "reason": "su asistente personal está desactivado"})
    return out


def is_eligible(conn, company_id: str, user_id: str, role: str) -> bool:
    return any(s["id"] == user_id for s in eligible_staff(conn, company_id, role))


def pick_assignee(conn, company_id: str, role: str) -> dict | None:
    """Default responsible for work nobody named (legacy scenarios A–D, material requests): the eligible employee with the FEWEST open tasks (balanced), not «the first»."""
    s = eligible_staff(conn, company_id, role)
    return s[0] if s else None


def personal_agent(conn, company_id: str, user_id: str, template: str) -> dict | None:
    return one(conn, "SELECT * FROM agents WHERE company_id=:c AND user_id=:u AND role=:t", c=company_id, u=user_id, t=template)
