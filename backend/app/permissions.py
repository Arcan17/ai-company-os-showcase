"""Permission model. Humans get role permissions; agents get their own set. A personal assistant acts
on behalf of a user, so its EFFECTIVE permissions = agent permissions ∩ the user's role permissions
(an assistant can never exceed its human, nor read what its human cannot).
`approvals.decide` is human-only: no agent holds it, so an LLM/agent can never approve spending.
"""
from __future__ import annotations

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {"requests.read", "customers.read", "tasks.read", "approvals.read", "approvals.decide", "company.summary",
              "workorders.read_all", "visits.read", "notes.read_own", "results.read", "runs.start", "runs.resolve",
              "agents.read", "events.read", "quotes.read", "commitments.read", "commitments.write", "commitments.process",
              "plans.create", "plans.confirm", "plans.read", "config.manage", "requests.create_manual",
              "customers.access_log", "customers.export", "customers.anonymize"},
    "admin": {"delegations.write", "requests.read", "customers.read", "customers.write", "pricing.read", "inventory.read", "quotes.write",
              "quotes.read", "visits.propose", "visits.read", "tasks.write", "tasks.read", "availability.read",
              "workorders.read_all", "workorders.write", "materials.request", "notes.read_own", "approvals.read",
              "results.read", "runs.start", "runs.resolve", "agents.read", "events.read", "commitments.read", "commitments.write",
              "commitments.process", "tasks.complete", "materials.manage", "requests.create_manual"},
    "technician": {"workorders.read_assigned", "workorders.update", "materials.request", "issues.report", "tasks.read",
                   "notes.read_own", "agents.read", "events.read", "commitments.read", "tasks.complete"},
}

AGENT_PERMISSIONS: dict[str, set[str]] = {
    "reception": {"delegations.write", "customers.read", "customers.write", "requests.create", "conversations.write", "messages.prepare", "visits.read"},
    "coordinator": {"requests.read", "requests.update", "classify.run", "availability.read", "delegations.write",
                    "approvals.request", "tasks.write", "results.write", "customers.read", "commitments.write", "commitments.read",
                    "materials.manage", "plans.read", "plans.assign", "plans.verify", "plans.finish"},
    "owner_assistant": {"approvals.read", "company.summary", "requests.read", "tasks.read", "workorders.read_all",
                        "notes.read_own", "results.read", "customers.read", "visits.read", "commitments.read"},
    "admin_assistant": {"delegations.write", "pricing.read", "inventory.read", "quotes.write", "quotes.read", "visits.propose", "tasks.write",
                        "tasks.read", "availability.read", "requests.read", "customers.read", "materials.request",
                        "notes.read_own", "workorders.write", "workorders.read_all", "visits.read", "tasks.complete", "commitments.read",
                        "materials.manage"},
    "tech_assistant": {"workorders.read_assigned", "workorders.update", "materials.request", "issues.report", "tasks.read",
                       "notes.read_own", "tasks.complete", "commitments.read"},
}

AGENT_META = {
    "reception": ("Agente de recepción", "Atiende al cliente por chat: identifica, consulta historial, registra la solicitud y la entrega al coordinador.", None),
    "coordinator": ("Coordinador central", "Clasifica, asigna, delega, revisa bloqueos y solicita aprobación humana.", None),
    "owner_assistant": ("Asistente del dueño", "Estado de la empresa, compromisos, aprobaciones y resultados.", "owner"),
    "admin_assistant": ("Asistente administrativo", "Presupuestos, inventario, reservas y tareas; coordina con el técnico.", "admin"),
    "tech_assistant": ("Asistente del técnico", "Trabajos asignados, avances, problemas y materiales.", "technician"),
}


def effective_permissions(agent_perms: set[str], user_role: str | None) -> set[str]:
    if user_role is None:
        return set(agent_perms)
    return agent_perms & ROLE_PERMISSIONS[user_role]
