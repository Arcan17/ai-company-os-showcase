"""Persistent event log: the single source of truth for the flow visualizer.

The engine emits an event for each real step (agent activation, delegation, tool call, DB access,
approval, outcome). They are committed immediately, so SSE consumers and later replays see the same thing.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from .db import all_, jdump, jload, now, one, q


def emit_in(conn, company_id: str, run_id: str, type_: str, *, agent: str | None = None, mode: str | None = None,
            title: str = "", data: dict | None = None) -> dict:
    """Insert an event INSIDE the caller's transaction (so it commits or rolls back together with the state change that caused it)."""
    seq = one(conn, "SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE run_id=:r", r=run_id)["n"]
    q(conn, "INSERT INTO events(company_id,run_id,seq,ts,type,agent,mode,title,data) VALUES(:c,:r,:s,:t,:ty,:a,:m,:ti,:d)",
      c=company_id, r=run_id, s=seq, t=now(), ty=type_, a=agent, m=mode, ti=title, d=jdump(data or {}))
    return {"seq": seq, "type": type_}


def emit(eng: Engine, company_id: str, run_id: str, type_: str, *, agent: str | None = None, mode: str | None = None,
         title: str = "", data: dict | None = None) -> dict:
    with eng.begin() as c:
        return emit_in(c, company_id, run_id, type_, agent=agent, mode=mode, title=title, data=data)


def fetch(eng: Engine, company_id: str, run_id: str, after: int = 0) -> list[dict]:
    with eng.connect() as c:
        rows = all_(c, "SELECT id,seq,ts,type,agent,mode,title,data FROM events WHERE company_id=:c AND run_id=:r AND seq>:a ORDER BY seq",
                    c=company_id, r=run_id, a=after)
    for r in rows:
        r["data"] = jload(r["data"], {})
    return rows


def audit(conn, company_id: str, actor_type: str, actor_id: str, action: str, resource: str, outcome: str, detail: str = "") -> None:
    q(conn, "INSERT INTO audit_log(company_id,ts,actor_type,actor_id,action,resource,outcome,detail) VALUES(:c,:t,:at,:ai,:a,:r,:o,:d)",
      c=company_id, t=now(), at=actor_type, ai=actor_id, a=action, r=resource, o=outcome, d=detail)
