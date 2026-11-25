"""Persistent event log: the single source of truth for the flow visualizer.

The engine emits an event for each real step (agent activation, delegation, tool call, DB access,
approval, outcome). They are committed immediately, so SSE consumers and later replays see the same thing.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .db import all_, jdump, jload, now, one, q


def emit_in(conn, company_id: str, run_id: str, type_: str, *, agent: str | None = None, mode: str | None = None,
            title: str = "", data: dict | None = None) -> dict:
    """Insert an event INSIDE the caller's transaction (so it commits or rolls back together with the state change that caused it)."""
    # El número de secuencia se asigna en UNA sola sentencia (MAX+1 dentro del propio INSERT, bajo el bloqueo de escritura): antes se leía el MAX y se insertaba después, y dos escritores de la
    # misma ejecución (el hilo del motor y la petición de aprobación) podían elegir el mismo `seq` (UNIQUE) y dejar la ejecución «failed» o la aprobación en 500. Si aun así hay una colisión
    # (otra base de datos con otro aislamiento), se reintenta con un número fresco.
    params = dict(c=company_id, r=run_id, t=now(), ty=type_, a=agent, m=mode, ti=title, d=jdump(data or {}))
    sql = ("INSERT INTO events(company_id,run_id,seq,ts,type,agent,mode,title,data) "
           "SELECT :c,:r,COALESCE(MAX(seq),0)+1,:t,:ty,:a,:m,:ti,:d FROM events WHERE run_id=:r RETURNING seq")
    for attempt in range(5):
        try:
            seq = q(conn, sql, **params).scalar_one()
            break
        except IntegrityError:
            if attempt == 4:
                raise
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
