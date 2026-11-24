#!/usr/bin/env python3
"""Prueba de humo HTTP de una instancia ya levantada (contenedor o local): salud, interfaz, login, y el flujo corto
solicitud → aprobación humana → visita PROPUESTA (nunca confirmada). Solo biblioteca estándar. Uso: smoke_http.py http://127.0.0.1:8731"""
import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8731").rstrip("/")
jar = CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
csrf = ""


def call(method, path, body=None, auth=True):
    h = {"Origin": BASE}
    if body is not None:
        h["Content-Type"] = "application/json"
    if auth and csrf and method != "GET":
        h["X-CSRF-Token"] = csrf
    req = urllib.request.Request(BASE + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    try:
        with op.open(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def ok(cond, msg):
    print(("  ✔ " if cond else "  ✖ ") + msg)
    if not cond:
        sys.exit(1)


s, b = call("GET", "/api/health"); ok(s == 200, f"/api/health responde 200 ({s})")
s, b = call("GET", "/"); ok(s == 200 and b"<div id=\"root\"" in b, "la interfaz (SPA) se sirve en /")
s, b = call("POST", "/api/auth/login", {"email": "dueno@climasur.test", "password": "demo1234"}, auth=False)
ok(s == 200, f"login de la dueña ficticia ({s})")
csrf = json.loads(b)["csrf_token"]
s, b = call("POST", "/api/public/clima-sur/chat", {"message": "Hola, mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas", "phone": "+56 9 5555 0101", "client_msg_id": "smoke-0001"}, auth=False)
ok(s == 200, f"el cliente escribe por el chat público ({s})")
import time
ap = None
for _ in range(40):
    s, b = call("GET", "/api/dashboard")
    pend = json.loads(b).get("pending_approvals", []) if s == 200 else []
    if pend:
        ap = pend[0]; break
    time.sleep(1)
ok(ap is not None, "la ejecución se PAUSA pidiendo aprobación humana")
s, b = call("POST", f"/api/approvals/{ap['id']}/decide", {"decision": "approved"}); ok(s == 200, f"la dueña aprueba ({s})")
vis = None
for _ in range(40):
    s, b = call("GET", f"/api/requests/{ap['request_id']}")
    d = json.loads(b) if s == 200 else {}
    if d.get("visit"):
        vis = d["visit"]; break
    time.sleep(1)
ok(vis is not None and vis["customer_confirmed"] == 0, "la visita queda PROPUESTA, no confirmada por el cliente")
print("OK: prueba de humo completa")
