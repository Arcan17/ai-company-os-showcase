"""Standby gate: an instance that does NOT hold the database lease is read-only.

Why (PR #12 review, P1): two servers on the same SQLite file. The one without the lease skipped recovery but still accepted work, so it launched runs/plans in threads that
nothing supervises and no restart would ever recover — and the lease holder, restarting, could declare those live runs interrupted. Now every write that could start, continue
or finish work is refused with 503 BEFORE any state is touched; reads, sign-in and the customer's visit confirmation (plain DB writes, no thread) keep working.
Pure ASGI, like csrf.py (SSE-safe).
"""
from __future__ import annotations

import json
import re

SAFE = {"GET", "HEAD", "OPTIONS"}
ALWAYS_OK = re.compile(r"^/api/(auth/.*|public/[^/]+/visits/confirm)$")
DETAIL = "Esta instancia está en espera: otra instancia activa gestiona la ejecución de esta base de datos. Reintenta en unos segundos."


class ActiveGate:
    def __init__(self, app, is_active):
        self.app, self.is_active = app, is_active

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in SAFE and scope["path"].startswith("/api/") and not ALWAYS_OK.match(scope["path"]) and not self.is_active():
            body = json.dumps({"detail": DETAIL}, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"retry-after", b"10"),
                                                                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)
