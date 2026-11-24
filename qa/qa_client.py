"""Shared login helper for the standalone QA scripts (qa/evidence.py, qa/llm_audit.py).

The API refuses any write that carries a session cookie without the session's `X-CSRF-Token` (backend/app/csrf.py). A script is not a browser, so it does
what the SPA does: sign in, keep the token the server returned, send it on every request. The defence is NOT relaxed for QA — there is no bypass flag.
Works with `httpx.Client` (live server) and Starlette's `TestClient` (in-process app): both are httpx clients.
"""
from __future__ import annotations

DEMO_PASSWORD = "demo1234"   # the FICTITIOUS demo accounts' password (shown on the login screen)


def csrf_login(client, email: str, password: str = DEMO_PASSWORD):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text[:200]}"
    token = r.json().get("csrf_token")
    assert token, "the server did not return csrf_token (is this the V0.6 access-hardening API?)"
    client.headers["X-CSRF-Token"] = token      # replaced on every login: the token belongs to THIS session
    return client
