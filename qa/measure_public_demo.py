#!/usr/bin/env python3
"""Mide el consumo de la DEMO PÚBLICA con N visitantes simultáneos (spec 004, tarea T016): memoria residente del proceso (antes, pico y después), tiempos de respuesta y cuántos espacios se crean.
Arranca su PROPIO servidor en un puerto libre y un directorio temporal (no toca nada real). Cada visitante: crea su espacio, entra como la dueña, escribe como cliente, espera la aprobación y la aprueba.
Uso (raíz del repo):  backend/.venv/bin/python qa/measure_public_demo.py [--visitors 20] [--step-delay 0.12]      (en el HP: el mismo comando dentro del contenedor o con el entorno del backend)"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else 0.0


class Visitor:
    def __init__(self, base: str):
        self.base, self.csrf, self.lat = base, "", []
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, method, path, body=None):
        h = {"Origin": self.base}
        if body is not None:
            h["Content-Type"] = "application/json"
        if self.csrf and method != "GET":
            h["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
        t = time.monotonic()
        try:
            with self.op.open(req, timeout=60) as r:
                data = json.loads(r.read() or b"null"); code = r.status
        except urllib.error.HTTPError as e:
            data, code = None, e.code
        self.lat.append(time.monotonic() - t)
        return code, data

    def run(self, i: int) -> str:
        s, d = self.call("POST", "/api/auth/login", {"email": "dueno@climasur.test", "password": "demo1234"})
        if s != 200:
            return f"login {s}"
        self.csrf = d["csrf_token"]
        s, _ = self.call("POST", "/api/public/clima-sur/chat", {"message": "Hola, mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas.", "name": "Marta Rojas", "client_msg_id": f"m-{i:04d}-x"})
        if s != 200:
            return f"chat {s}"
        for _ in range(120):
            s, d = self.call("GET", "/api/dashboard")
            if s == 200 and d["pending_approvals"]:
                s, _ = self.call("POST", f"/api/approvals/{d['pending_approvals'][0]['id']}/decide", {"decision": "approved"})
                return "ok" if s == 200 else f"decide {s}"
            time.sleep(0.5)
        return "sin aprobación"


def unit_mem_mb(host: str, unit: str, prop: str = "MemoryCurrent") -> float:
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"systemctl --user show {unit} -p {prop} --value"], capture_output=True, text=True, timeout=30).stdout.strip()
    return int(out) / 1048576 if out.isdigit() else 0.0


def measure_remote(a) -> int:
    """Mide una demo ya levantada: lanza N visitantes por `--base` y lee la memoria de la unidad por SSH (antes, pico del cgroup y después). Respeta los límites reales de la instancia (tope y espacios nuevos por origen)."""
    idle = unit_mem_mb(a.ssh_host, a.unit) if a.ssh_host else 0.0
    vs = [Visitor(a.base.rstrip("/")) for _ in range(a.visitors)]
    res = [None] * a.visitors
    t0 = time.monotonic()
    ths = [threading.Thread(target=lambda i=i: res.__setitem__(i, vs[i].run(i + 5000))) for i in range(a.visitors)]
    [t.start() for t in ths]; [t.join() for t in ths]
    total = time.monotonic() - t0
    lat = sorted(x for v in vs for x in v.lat)
    ok = sum(1 for r in res if r == "ok")
    print(f"visitantes: {a.visitors} · flujo completo OK: {ok} · tiempo total {total:.1f} s · latencia (incluye el túnel): mediana {statistics.median(lat) * 1000:.0f} ms · p95 {lat[max(0, int(len(lat) * .95) - 1)] * 1000:.0f} ms")
    if a.ssh_host:
        after, peak = unit_mem_mb(a.ssh_host, a.unit), unit_mem_mb(a.ssh_host, a.unit, "MemoryPeak")
        print(f"memoria de la unidad (cgroup): antes {idle:.0f} MB · después {after:.0f} MB · pico desde el arranque {peak:.0f} MB · por visitante ≈ {(after - idle) / max(a.visitors, 1):.1f} MB")
    if ok != a.visitors:
        print("✖ fallos:", [r for r in res if r != "ok"]); return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--visitors", type=int, default=20)
    ap.add_argument("--step-delay", default="0.12")
    ap.add_argument("--base", default="", help="URL de una demo YA levantada (p. ej. por túnel SSH): no se arranca servidor propio")
    ap.add_argument("--ssh-host", default="", help="con --base: host SSH donde corre la unidad, para leer su memoria (cgroup de systemd)")
    ap.add_argument("--unit", default="aicos-demo.service", help="con --ssh-host: unidad systemd de usuario a medir")
    a = ap.parse_args()
    if a.base:
        return measure_remote(a)
    py = ROOT / "backend" / ".venv" / "bin" / "python"
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="aicos-measure-") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AICOS_")}
        env.update(AICOS_PUBLIC_DEMO="1", AICOS_PUBLIC_DEMO_DIR=tmp, AICOS_PUBLIC_MAX_VISITORS=str(a.visitors + 5), AICOS_PUBLIC_NEW_PER_SOURCE="1000",
                   AICOS_STEP_DELAY=a.step_delay, AICOS_STATIC_DIR=str(ROOT / "frontend" / "dist"), PYTHONDONTWRITEBYTECODE="1",
                   AICOS_RL_CHAT_SRC="1000/600", AICOS_RL_CHAT_CO="1000/600")
        srv = subprocess.Popen([str(py), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-proxy-headers"], cwd=ROOT / "backend", env=env, stdout=subprocess.DEVNULL, stderr=open(os.environ["AICOS_MEASURE_LOG"], "w") if os.environ.get("AICOS_MEASURE_LOG") else subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen(base + "/api/health", timeout=1); break
                except Exception:
                    time.sleep(0.5)
            else:
                print("✖ el servidor no arrancó"); return 1
            idle = rss_mb(srv.pid)
            peak, stop = idle, threading.Event()

            def sampler():
                nonlocal peak
                while not stop.is_set():
                    peak = max(peak, rss_mb(srv.pid)); time.sleep(0.1)
            threading.Thread(target=sampler, daemon=True).start()
            vs = [Visitor(base) for _ in range(a.visitors)]
            res = [None] * a.visitors
            t0 = time.monotonic()
            ths = [threading.Thread(target=lambda i=i: res.__setitem__(i, vs[i].run(i))) for i in range(a.visitors)]
            [t.start() for t in ths]; [t.join() for t in ths]
            total = time.monotonic() - t0
            time.sleep(1); stop.set()
            _, info = Visitor(base).call("GET", "/api/public-demo/info")
            lat = sorted(x for v in vs for x in v.lat)
            ok = sum(1 for r in res if r == "ok")
            print(f"visitantes: {a.visitors} · flujo completo OK: {ok} · espacios activos: {info['active']}")
            print(f"memoria residente del servidor: reposo {idle:.0f} MB · pico {peak:.0f} MB · final {rss_mb(srv.pid):.0f} MB · por visitante ≈ {(peak - idle) / a.visitors:.1f} MB")
            print(f"tiempo total del flujo (todos a la vez): {total:.1f} s · latencia de peticiones: mediana {statistics.median(lat) * 1000:.0f} ms · p95 {lat[int(len(lat) * .95) - 1] * 1000:.0f} ms · máx {lat[-1] * 1000:.0f} ms")
            if ok != a.visitors:
                print("✖ fallos:", [r for r in res if r != "ok"]); return 1
            return 0
        finally:
            srv.terminate()
            try:
                srv.wait(10)
            except Exception:
                srv.kill()


if __name__ == "__main__":
    sys.exit(main())
