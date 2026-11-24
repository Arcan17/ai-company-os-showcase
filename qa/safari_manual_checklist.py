#!/usr/bin/env python3
"""Safari verification helper — SAFE BY DESIGN (Issue #4).

It NEVER drives Safari or any other GUI: no scripting bridge, no webdriver, no screen capture, no window creation / resizing / focusing /
closing, no clicks, no keystrokes (tests/test_qa_safety.py enforces this over the whole repository). It only (1) checks over plain HTTP that the demo server is ready and sends the headers
Safari needs, and (2) prints the URLs and the MANUAL checklist for the person at the keyboard (Safari personal is verified by hand).

  python3 qa/safari_manual_checklist.py [port]      # default: port of the running commercial demo (backend/data/.demo_comercial.pid) or 8743
"""
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def demo_port() -> int:
    if len(sys.argv) > 1:
        return int(sys.argv[1])
    pidf = os.path.join(ROOT, "backend", "data", ".demo_comercial.pid")
    try:
        return int(open(pidf).read().split()[1])
    except Exception:
        return 8743


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url: str, redirect: bool = True):
    opener = urllib.request.build_opener() if redirect else urllib.request.build_opener(_NoRedirect)
    try:
        r = opener.open(url, timeout=5)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:  # server down
        return 0, {}, str(e).encode()


def main() -> int:
    port = demo_port()
    base = f"http://127.0.0.1:{port}"
    checks = []
    st, h, body = get(f"{base}/demo")
    checks.append(("La guía /demo responde en UTF-8", st == 200 and "utf-8" in h.get("content-type", "").lower() and "DEMO COMERCIAL" in body.decode("utf-8", "replace")))
    st, h, body = get(f"{base}/api/auth/demo-accounts")
    checks.append(("El JSON declara charset=utf-8 (Safari lo muestra mal si no)", st == 200 and "charset=utf-8" in h.get("content-type", "").lower()))
    st, h, _ = get(f"{base}/demo-login/owner", redirect=False)
    checks.append(("El acceso por URL fija una cookie HttpOnly y redirige", st == 303 and "httponly" in h.get("set-cookie", "").lower()))
    st, _, _ = get(f"{base}/api/plans")
    checks.append(("Sin sesión la API protegida responde 401", st == 401))
    print(f"Servidor comprobado por HTTP: {base}   (no se abre ni se controla ningún navegador)\n")
    ok = True
    for name, passed in checks:
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    if all(not p for _, p in checks):
        print("\n  ⚠ El servidor no responde: ejecuta ./run_demo_comercial.sh start")
    print(f"""
CHECKLIST MANUAL EN SAFARI (hazla tú, en una ventana que abras tú; ninguna herramienta toca tus ventanas)
  1. {base}/demo   → se ve «DEMO COMERCIAL», las tres ventanas de acceso y los acentos bien (sin «Ã³»).
  2. Dueña   : http://dueno.localhost:{port}/demo-login/owner      → Dashboard con banner, 4 solicitudes (2 activas · 2 históricas).
  3. Admin.  : http://admin.localhost:{port}/demo-login/admin      → otra sesión (otro host = otras cookies).
  4. Técnico : http://tecnico.localhost:{port}/demo-login/tech
  5. Dueña → «Coordinador»: escribe la instrucción principal (está en /demo) → «Proponer plan» → 4 pasos, 0 tareas → «Confirmar plan».
  6. Administración → «Mi asistente»: «¿Cuánto stock hay de KIT-MANT?» y completa la tarea con evidencia (≥ 10 caracteres).
  7. Técnico → «Mi asistente»: aparece su tarea; complétala con evidencia.
  8. Dueña → plan «Completada» → «Ver en el visualizador»: «SSE conectado», 37 eventos, nodos en verde; el lienzo es CLARO aunque Safari esté en modo oscuro.
  9. Dueña → «Compra repuestos y avisa a todos por WhatsApp.» → plan «Bloqueada», sin botón de confirmar.
 10. Ventana estrecha (≈430 px): coordinador y dashboard sin desbordamiento horizontal.
Capturas: hazlas tú (⇧⌘4 → barra espaciadora → clic en TU ventana de la demo). Esta herramienta no captura ventanas.
""")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
