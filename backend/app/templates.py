"""Agent TEMPLATES (V0.5): the catalogue of assistants a company can configure. Templates are defined in code (permissions.AGENT_PERMISSIONS / AGENT_META) — an owner
can ASSIGN a template to an employee and enable/disable that assignment, but cannot invent agents, prompts or tools, and configuring an assistant never grants
permissions: a personal assistant acts with (template permissions ∩ the employee's role permissions). Capabilities are DERIVED from the tool registry."""
from __future__ import annotations

from .directory import ROLE_BY_TEMPLATE
from .permissions import AGENT_META, AGENT_PERMISSIONS, ROLE_PERMISSIONS, effective_permissions
from .tools.registry import REGISTRY

ORDER = ["reception", "coordinator", "owner_assistant", "admin_assistant", "tech_assistant"]


def describe_templates() -> list[dict]:
    out = []
    for key in ORDER:
        name, desc, urole = AGENT_META[key]
        perms = AGENT_PERMISSIONS[key]
        eff = effective_permissions(set(perms), urole) if urole else set(perms)
        tools = [{"name": t.name, "access": t.access, "mode": t.mode, "description": t.description[:140]} for t in sorted(REGISTRY.values(), key=lambda x: x.name) if t.permission in eff]
        out.append({"key": key, "name": name, "description": desc, "scope": "personal" if urole else "shared", "role": urole,
                    "permissions": sorted(perms), "effective_permissions": sorted(eff), "capabilities": tools,
                    "note": ("Actúa con los permisos de la plantilla ∩ los del rol del empleado: no puede exceder a su persona." if urole else "Agente compartido de la empresa (no pertenece a un empleado).")})
    return out


def template_role(key: str) -> str | None:
    """Role an employee must have to receive this PERSONAL template (None for shared agents)."""
    return ROLE_BY_TEMPLATE.get(key)
