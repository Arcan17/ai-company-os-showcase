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
def pending_ids(v):
    s, d = v.call("GET", "/api/dashboard")
    return {x["id"] for x in d["pending_approvals"]} if s == 200 else set()


# Con AICOS_DEMO_EXAMPLE=1 cada visitante ya abre con una decisión de ejemplo (la MISMA en cada copia de la plantilla): se compara contra esa línea base, no contra «vacío».
base_a, base_b = pending_ids(a), pending_ids(b)
ok(len(base_a) == len(base_b) and len(base_a) <= 1, f"cada visitante abre con la misma base ({len(base_a)} decisión de ejemplo o ninguna)")
s, _ = a.call("POST", "/api/public/clima-sur/chat", {"message": "Hola, mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas", "client_msg_id": "smoke-pub-1"})
ok(s == 200, "el visitante A escribe como cliente")
nuevas = set()
for _ in range(40):
    nuevas = pending_ids(a) - base_a
    if nuevas:
        break
    time.sleep(1)
ok(len(nuevas) == 1, "A ve su aprobación pendiente NUEVA (además de la de ejemplo, si la hay)")
ok(pending_ids(b) == base_b and not (nuevas & pending_ids(b)), "B NO ve nada de lo de A (espacios aislados)")
print("OK: demo pública aislada por visitante")
