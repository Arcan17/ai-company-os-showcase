#!/usr/bin/env python3
"""Prueba de humo de la DEMO PÚBLICA ya levantada: dos visitantes (dos frascos de cookies) tienen espacios AISLADOS. A hace llegar una solicitud; B no la ve. Solo biblioteca estándar.
Uso: smoke_public.py http://127.0.0.1:8731"""
import json
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8731").rstrip("/")


class Visitor:
    def __init__(self):
        self.jar = CookieJar(); self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar)); self.csrf = ""

    def call(self, method, path, body=None):
        h = {"Origin": BASE}
        if body is not None:
            h["Content-Type"] = "application/json"
        if self.csrf and method != "GET":
            h["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(BASE + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.op.open(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def login(self):
        s, d = self.call("POST", "/api/auth/login", {"email": "dueno@climasur.test", "password": "demo1234"})
        assert s == 200, f"login {s}"
        self.csrf = d["csrf_token"]


def ok(cond, msg):
    print(("  ✔ " if cond else "  ✖ ") + msg)
    if not cond:
        sys.exit(1)


a, b = Visitor(), Visitor()
s, info = a.call("GET", "/api/public-demo/info"); ok(s == 200 and info["public_demo"] is True, "la instancia es una DEMO PÚBLICA")
a.login(); b.login()
names = {c.name for c in a.jar} & {c.name for c in b.jar}
ok("aicos_visitor" in names, "cada visitante recibe su cookie de espacio")
ok({c.value for c in a.jar if c.name == "aicos_visitor"} != {c.value for c in b.jar if c.name == "aicos_visitor"}, "las cookies de espacio son distintas")
s, _ = a.call("POST", "/api/public/clima-sur/chat", {"message": "Hola, mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas", "client_msg_id": "smoke-pub-1"})
ok(s == 200, "el visitante A escribe como cliente")
pend = []
for _ in range(40):
    s, d = a.call("GET", "/api/dashboard"); pend = d["pending_approvals"] if s == 200 else []
    if pend:
        break
    time.sleep(1)
ok(len(pend) == 1, "A ve su aprobación pendiente")
s, d = b.call("GET", "/api/dashboard"); ok(s == 200 and d["pending_approvals"] == [], "B NO ve nada de lo de A (espacios aislados)")
print("OK: demo pública aislada por visitante")
