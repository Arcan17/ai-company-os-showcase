"""Static HTML for /demo (commercial-demo launcher). Only mounted when AICOS_DEMO_MODE=1. No external requests, no scripts beyond one clipboard button."""
from __future__ import annotations

from html import escape

from .demo_seed import FORBIDDEN_TRY, INSTRUCTION


def page(info: dict, port: int, summary: dict) -> str:
    p = f"http://{{h}}.localhost:{port}/demo-login/{{r}}"
    links = [("Dueña — Carolina Vega", "dueno", "owner", "Crea el plan, lo confirma, ve el visualizador"), ("Administración — Javiera Soto", "admin", "admin", "Consulta stock y cierra su tarea con evidencia"),
             ("Técnico — Matías Fuentes", "tecnico", "tech", "Su tarea espera a administración; confirma con evidencia")]
    btn = "".join(f'<a class="btn" href="{p.format(h=h, r=r)}" target="_blank" rel="noopener"><b>{escape(t)}</b><span>{escape(d)}</span><code>{h}.localhost:{port}</code></a>' for t, h, r, d in links)
    rows = "".join(f"<tr><td>{escape(k)}</td><td><b>{escape(str(v))}</b></td></tr>" for k, v in [
        ("Base de datos (solo el nombre)", info["db"]), ("Modo del motor", info["mode_label"]), ("Versión / commit", info["commit"] or "—"), ("Empresa de la demo", "Clima Sur (ficticia)"),
        ("Solicitudes abiertas / mantenciones abiertas de Marta", f"{summary['open_requests']} / {summary['open_marta_maintenance']}"), ("Planes · tareas · ejecuciones fallidas", f"{summary['plans']} · {summary['tasks']} · {summary['failed_runs']}"),
        ("Mensajes ENVIADOS a terceros", summary["messages_sent"])])
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Demo comercial · AI Company OS</title>
<style>:root{{color-scheme:light dark}}body{{font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:920px;margin:24px auto;padding:0 16px;background:#f8fafc;color:#0f172a}}
@media(prefers-color-scheme:dark){{body{{background:#0f172a;color:#e2e8f0}}.card{{background:#1e293b!important;border-color:#334155!important}}.btn{{background:#1e293b!important;border-color:#475569!important;color:#e2e8f0!important}}}}
h1{{font-size:24px;margin:0}}h2{{font-size:16px;margin:0 0 8px}}.sub{{color:#64748b;margin:4px 0 16px}}.card{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px;margin:14px 0}}
.btn{{display:block;background:#fff;border:1px solid #c7d2fe;border-radius:12px;padding:10px 14px;margin:8px 0;text-decoration:none;color:#1e1b4b}}.btn span{{display:block;color:#64748b;font-size:13px}}.btn code{{font-size:12px;color:#4f46e5}}
table{{border-collapse:collapse;width:100%}}td{{padding:4px 8px;border-top:1px solid #e2e8f0;font-size:14px}}td:first-child{{color:#64748b;width:48%}}.warn{{background:#fffbeb;border-color:#fcd34d;color:#78350f}}
.warn *{{color:inherit}}pre{{white-space:pre-wrap;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:10px;font-size:13px}}button{{cursor:pointer}}ul{{margin:6px 0;padding-left:20px}}.ok{{color:#047857}}.no{{color:#b91c1c}}</style></head><body>
<h1>DEMO COMERCIAL · AI Company OS V0.4</h1><p class="sub">Entorno aislado con datos 100 % ficticios · solo en este Mac (127.0.0.1) · independiente del laboratorio (:8731)</p>
<div class="card warn"><h2>Antes de empezar</h2><ul><li>Abre cada perfil con su botón: usan <b>nombres de host distintos</b> (<code>dueno.localhost</code>, <code>admin.localhost</code>, <code>tecnico.localhost</code>) para que las <b>sesiones no se mezclen</b> en el mismo navegador (las cookies no se aíslan por puerto).</li>
<li>No envíes mensajes desde el «Chat de cliente»: crearía otra solicitud de Marta y el camino rápido dejaría de ser «una sola mantención abierta».</li></ul></div>
<div class="card"><h2>1 · Abrir las tres sesiones</h2>{btn}</div>
<div class="card"><h2>2 · Instrucción principal (se escribe en «Coordinador (pedir trabajo)»)</h2><pre id="ins">{escape(INSTRUCTION)}</pre><button onclick="navigator.clipboard.writeText(document.getElementById('ins').innerText)">Copiar instrucción</button>
<h2 style="margin-top:14px">Prueba de control (al final)</h2><pre>{escape(FORBIDDEN_TRY)}</pre></div>
<div class="card"><h2>3 · Estado verificable del entorno</h2><table>{rows}</table></div>
<div class="card"><h2>4 · Qué es real y qué es simulado</h2><div style="display:grid;grid-template-columns:1fr 1fr;gap:16px"><div><b class="ok">REAL</b><ul><li>Base de datos, permisos por rol y empresa, auditoría</li><li>Plan → confirmación humana → tareas → evidencia → verificación</li><li>Stock del inventario ficticio</li><li>Eventos persistidos y en vivo (SSE) en el visualizador</li></ul></div>
<div><b class="no">SIMULADO / NO EXISTE</b><ul><li>WhatsApp, correo, teléfono, pagos, calendarios: <b>no están conectados</b> (los mensajes a clientes son borradores «preparado, no enviado»)</li><li>«Agentes»: son roles lógicos (código + herramientas), no autónomos</li><li>Empresa, personas y datos: ficticios</li></ul></div></div></div>
<p class="sub">Guion de 10 minutos, plan B y límites: <code>docs/demo/RUNBOOK_DEMO.md</code> en el repositorio.</p></body></html>"""
