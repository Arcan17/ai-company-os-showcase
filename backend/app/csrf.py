"""CSRF and cross-origin write protection for every state-changing request under /api/.

Two independent layers (OWASP CSRF Prevention Cheat Sheet: synchronizer token for stateful apps + Fetch Metadata / Origin as defence in depth):
  1. BROWSER ORIGIN — if the browser says where the request comes from, it must be THIS origin. `Sec-Fetch-Site` must be same-origin (`same-site` is rejected on
     purpose: the demo serves three roles from sibling hosts dueno./admin./tecnico.localhost, which are same-site but not same-origin) and `Origin` must match
     the Host (or an explicitly trusted origin). A client that sends neither (curl, Playwright's API context, tests) is not a browser: layer 2 still applies.
  2. SYNCHRONIZER TOKEN — whenever a valid session cookie rides along, `X-CSRF-Token` must equal the random token stored in THAT session row.
Exempt: safe methods (GET/HEAD/OPTIONS); the customer channel `/api/public/*` (no cookie is ever read there, so there is no ambient authority to forge);
`/api/auth/login` skips only layer 2 (it creates the session) — layer 1 still stops "login CSRF".
Pure ASGI (not BaseHTTPMiddleware) so SSE streams and cancellation behave exactly as before.
"""
from __future__ import annotations

import hmac
import json
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from .config import settings
from .security import lookup_session

SAFE = {"GET", "HEAD", "OPTIONS"}


def _hdr(scope, name: bytes) -> str | None:
    for k, v in scope["headers"]:
        if k == name:
            return v.decode("latin-1")
    return None


class CsrfMiddleware:
    def __init__(self, app, eng, cookie_name: str):
        self.app, self.eng, self.cookie = app, eng, cookie_name

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in SAFE or not scope["path"].startswith("/api/") or scope["path"].startswith("/api/public/"):
            return await self.app(scope, receive, send)
        why = self._origin_problem(scope) or (None if scope["path"] == "/api/auth/login" else self._token_problem(scope))
        if why:
            body = json.dumps({"detail": why}, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)

    def _origin_problem(self, scope) -> str | None:
        site, origin, host = _hdr(scope, b"sec-fetch-site"), _hdr(scope, b"origin"), (_hdr(scope, b"host") or "").lower()
        if site is not None and site.lower() not in ("same-origin", "none"):
            return "csrf: petición de otro origen rechazada"
        if origin is not None:
            netloc = urlsplit(origin).netloc.lower() if origin.lower() != "null" else ""
            if not netloc or (netloc != host and netloc not in settings.trusted_origins):
                return "csrf: origen no permitido"
        return None

    def _token_problem(self, scope) -> str | None:
        raw = _hdr(scope, b"cookie")
        if not raw:
            return None
        try:
            morsel = SimpleCookie(raw).get(self.cookie)
        except Exception:
            return None
        if not morsel:
            return None
        with self.eng.connect() as c:
            found = lookup_session(c, morsel.value)
        if not found:
            return None                # no valid session → nothing to forge; the endpoint answers 401 by itself
        expected = found[0]["csrf"]
        sent = _hdr(scope, b"x-csrf-token") or ""
        if not expected:               # a pre-V0.6 session: it must fetch /api/auth/me once (which issues the token) before writing
            return "csrf: falta el token de la sesión"
        return None if hmac.compare_digest(sent.encode(), expected.encode()) else "csrf: token inválido o ausente"
