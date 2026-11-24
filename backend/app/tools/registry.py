"""Tool interface: every action an agent (or human via the API) can take goes through `invoke`.

Pipeline: lookup → permission check (principal's EFFECTIVE permissions) → pydantic validation (extra fields
forbidden) → handler inside ONE DB transaction scoped by company_id → audit row → events.
The schema list (`describe_tools`) is MCP-shaped (name/description/inputSchema) so an MCP server can expose
the same registry later without touching business logic. MCP is an interface, not the datastore.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.engine import Connection, Engine

from ..events import audit, emit


# Successful reads that ARE audited (sensitive personal data). Denied / invalid / failed calls are always audited, reads included.
AUDITED_READS = {"notes.read_own", "notes.read_others"}


class ToolError(Exception):
    pass


class Denied(Exception):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Principal:
    kind: str                 # "agent" | "user"
    id: str
    name: str
    role: str                 # agent role (reception, coordinator, ...) or user role (owner, admin, technician)
    company_id: str
    permissions: set[str]
    user_id: str | None = None      # human behind it (user itself, or the employee a personal assistant serves)
    user_role: str | None = None


@dataclass
class ToolCtx:
    conn: Connection
    company_id: str
    principal: Principal
    run_id: str | None
    provider: Any = None
    pending: list = field(default_factory=list)  # events emitted AFTER the transaction commits: (type, title, data, agent, run_id, mode)

    def event(self, type_: str, title: str, data: dict | None = None, *, agent: str | None = None, run_id: str | None = None, mode: str = "real"):
        self.pending.append((type_, title, data or {}, agent, run_id or self.run_id, mode))


@dataclass
class Tool:
    name: str
    description: str
    permission: str
    mode: str      # real | deterministic | simulated | llm
    access: str    # read | write
    Input: type[Strict]
    handler: Callable[[ToolCtx, Any], dict]


REGISTRY: dict[str, Tool] = {}


def tool(name: str, permission: str, Input: type[Strict], *, mode: str = "real", access: str = "read", description: str = ""):
    def deco(fn):
        REGISTRY[name] = Tool(name, description or (fn.__doc__ or "").strip(), permission, mode, access, Input, fn)
        return fn
    return deco


def describe_tools() -> list[dict]:
    return [{"name": t.name, "description": t.description, "permission": t.permission, "mode": t.mode, "access": t.access,
             "inputSchema": t.Input.model_json_schema()} for t in sorted(REGISTRY.values(), key=lambda x: x.name)]


def invoke(eng: Engine, principal: Principal, name: str, args: dict | None = None, *, run_id: str | None = None,
           provider: Any = None, pace: Callable[[], None] | None = None) -> dict:
    t = REGISTRY.get(name)
    actor = principal.role
    ev = (lambda typ, mode=None, title="", data=None: emit(eng, principal.company_id, run_id, typ, agent=actor, mode=mode,
                                                           title=title, data=data)) if run_id else (lambda *a, **k: None)
    if t is None:
        raise ToolError(f"herramienta desconocida: {name}")
    args = args or {}
    ev("tool.called", t.mode, f"{name}", {"tool": name, "input": args, "access": t.access})
    if pace:
        pace()
    if t.permission not in principal.permissions:
        with eng.begin() as c:
            audit(c, principal.company_id, principal.kind, principal.id, name, "-", "denied", f"falta permiso {t.permission}")
        ev("tool.denied", t.mode, f"{name} denegada", {"tool": name, "missing_permission": t.permission})
        raise Denied(f"{principal.role} no tiene el permiso {t.permission}")
    try:
        parsed = t.Input.model_validate(args)
    except ValidationError as e:
        msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:300]
        with eng.begin() as c:
            audit(c, principal.company_id, principal.kind, principal.id, name, "-", "invalid_input", msg)
        ev("tool.error", t.mode, f"{name}: entrada inválida", {"tool": name, "error": msg})
        raise ToolError(f"entrada inválida: {msg}") from e
    try:
        with eng.begin() as c:
            tctx = ToolCtx(c, principal.company_id, principal, run_id, provider)
            out = t.handler(tctx, parsed)
            if t.access == "write" or name in AUDITED_READS:  # UI polling reads (dashboard every 4 s) must not fill the audit log: found by the QA audit (320 rows/27 min from one open tab)
                audit(c, principal.company_id, principal.kind, principal.id, name, str(out.get("_resource", "-")),
                      "ok", f"{t.access}")
    except ToolError as e:
        with eng.begin() as c:
            audit(c, principal.company_id, principal.kind, principal.id, name, "-", "error", str(e)[:200])
        ev("tool.error", t.mode, f"{name}: {e}", {"tool": name, "error": str(e)})
        raise
    for typ, title, data, agent, rid, md in tctx.pending:  # committed → now visible in the event log
        if rid:
            emit(eng, principal.company_id, rid, typ, agent=agent or actor, mode=md, title=title, data=data)
    mode = out.pop("_mode", t.mode)
    out.pop("_resource", None)
    ev("tool.result", mode, f"{name} → {out.get('_summary', 'ok')}",
       {"tool": name, "access": t.access, "output": {k: v for k, v in out.items() if k != "_summary"}, "summary": out.get("_summary")})
    return out
