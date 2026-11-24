#!/usr/bin/env python3
"""Genera site/index.html (una sola página, sin dependencias externas) desde site/catalog.json y las cifras de site/facts.py. Determinista: la comprobación recompone la página y la compara.
Uso (raíz del repo):  backend/.venv/bin/python site/build_site.py"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import facts as F  # noqa: E402

HERE = Path(__file__).resolve().parent
E = html.escape

CSS = """:root{--bg:#f4f5fb;--ink:#14162b;--mut:#5b6080;--card:rgba(255,255,255,.78);--line:rgba(20,22,43,.1);--acc:#4f46e5;--acc2:#7c3aed;--ok:#0f7b55;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#0e1020;--ink:#eceefb;--mut:#a3a8c8;--card:rgba(30,33,60,.7);--line:rgba(236,238,251,.14);--acc:#8b86ff;--acc2:#b08bff;--ok:#5fd6a6;--warn:#f0c36a}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font:16px/1.65 system-ui,-apple-system,"Segoe UI",Inter,sans-serif;color:var(--ink);background:radial-gradient(60rem 40rem at 10% -10%,rgba(124,58,237,.16),transparent 60%),radial-gradient(50rem 36rem at 100% 0%,rgba(79,70,229,.14),transparent 55%),var(--bg);background-attachment:fixed}
a{color:var(--acc)}a:focus-visible,summary:focus-visible{outline:3px solid var(--acc);outline-offset:3px;border-radius:6px}
.skip{position:absolute;left:-999px;top:0;background:var(--ink);color:var(--bg);padding:.5rem 1rem;z-index:9}.skip:focus{left:1rem;top:1rem}
.wrap{max-width:68rem;margin:0 auto;padding:0 1.25rem}
header.hero{padding:4.5rem 0 2.5rem}.eyebrow{font-size:.78rem;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);font-weight:600}
h1{font-size:clamp(2.2rem,6vw,3.8rem);line-height:1.05;margin:.4rem 0 1rem;letter-spacing:-.02em}h1 span{color:var(--acc)}
.lead{font-size:1.2rem;color:var(--mut);max-width:44rem}
h2{font-size:1.6rem;margin:3.2rem 0 1rem;letter-spacing:-.01em}
.btns{display:flex;flex-wrap:wrap;gap:.7rem;margin-top:1.6rem}.btn{display:inline-block;padding:.7rem 1.2rem;border-radius:999px;border:1px solid var(--line);text-decoration:none;font-weight:600;background:var(--card);color:var(--ink)}
.btn.p{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;border-color:transparent}
.grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(17rem,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:1.3rem 1.4rem}
.card h3{margin:.1rem 0 .4rem;font-size:1.12rem}.card p{margin:.3rem 0;color:var(--mut)}
.fact{font-size:2.6rem;font-weight:700;letter-spacing:-.02em;line-height:1}.fact small{display:block;font-size:.85rem;font-weight:500;color:var(--mut);margin-top:.5rem}
code{font:.82em ui-monospace,SFMono-Regular,Menlo,monospace;background:rgba(127,127,160,.16);padding:.1em .4em;border-radius:6px;overflow-wrap:anywhere}
details{border-top:1px solid var(--line);margin-top:.8rem;padding-top:.6rem}summary{cursor:pointer;font-weight:600;color:var(--acc)}
ul{margin:.4rem 0 .4rem 1.1rem;padding:0;color:var(--mut)}li{margin:.2rem 0}.no li::marker{color:var(--warn)}.yes li::marker{color:var(--ok)}
.tag{display:inline-block;font-size:.72rem;font-weight:600;padding:.12rem .55rem;border-radius:999px;background:rgba(127,127,160,.16);color:var(--mut)}
.notice{border-left:4px solid var(--warn);padding:.2rem 1rem;margin:1rem 0;color:var(--mut)}
footer{margin:4rem 0 3rem;color:var(--mut);font-size:.9rem}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}}"""


def li(items):
    return "".join(f"<li>{E(x)}</li>" for x in items)


def build() -> str:
    cat = json.loads((HERE / "catalog.json").read_text())
    f = F.compute()
    repo = cat["repo"].rstrip("/")
    mods = []
    for m in cat["modulos"]:
        ev = "".join(f'<li><a href="{E(repo)}/blob/main/{E(p)}"><code>{E(p)}</code></a></li>' for p in m["evidencia"])
        mods.append(f"""<article class="card" id="{E(m['id'])}"><span class="tag">Módulo</span><h3>{E(m['nombre'])}</h3><p>{E(m['resuelve'])}</p>
<details><summary>Qué incluye, qué no y su evidencia</summary><p><b>Incluye</b></p><ul class="yes">{li(m['alcance'])}</ul><p><b>No incluye</b></p><ul class="no">{li(m['no_incluye'])}</ul>
<p><b>Evidencia</b></p><ul>{ev}</ul><p><b>Dónde verlo:</b> {E(m['demo'])}</p></details></article>""")
    facts = "".join(f"""<div class="card"><div class="fact" aria-label="{E(label)}"><span data-fact="{k}">{f[k]}</span><small>{E(label)}</small></div><p>Fuente: <code>{E(F.SOURCES[k])}</code></p></div>"""
                    for k, label in (("tests", "pruebas automáticas"), ("mutaciones", "mutaciones de seguridad (se rompe una propiedad y las pruebas deben notarlo)"), ("e2e", "pruebas de navegador real")))
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(cat['titulo'])} · agentes de IA con aprobación humana</title>
<meta name="description" content="{E(cat['bajada'])}">
<style>{CSS}</style></head><body>
<a class="skip" href="#modulos">Saltar a los módulos</a>
<div class="wrap">
<header class="hero"><div class="eyebrow">Prototipo con evidencia · datos 100 % ficticios</div>
<h1>Agentes de IA que trabajan <span>a la vista</span></h1><p class="lead">{E(cat['bajada'])}</p>
<p class="lead">Pensado para: {E(cat['para_quien'])}</p>
<div class="btns"><a class="btn p" href="#probar">Cómo probarlo</a><a class="btn" href="#modulos">Qué se puede construir</a><a class="btn" href="{E(repo)}">Código y caso de estudio</a></div></header>
<main>
<h2 id="probar">Cómo probarlo</h2>
<div class="grid">
<div class="card"><span class="tag">Un comando</span><h3>En tu computador</h3><p>Con Podman o Docker: <code>./start.sh</code> y abre <code>http://127.0.0.1:8731</code>.</p></div>
<div class="card"><span class="tag">Próximamente</span><h3>Demo en vivo</h3><p>Un espacio propio para cada visitante, sin instalar nada. Aún no está publicada.</p></div>
<div class="card"><span class="tag">Próximamente</span><h3>Video de 4 minutos</h3><p>El recorrido completo: solicitud, aprobación, visita propuesta y control de datos.</p></div>
</div>
<h2 id="modulos">Módulos que se pueden ofrecer</h2>
<p>Cada módulo se contrata a medida para un flujo concreto de tu negocio; no hay precio público. Cada uno dice qué incluye, qué no y dónde está la evidencia.</p>
<div class="grid">{''.join(mods)}</div>
<h2 id="evidencia">La evidencia, con su fuente</h2>
<p>Cada cifra se recalcula desde el repositorio en cada cambio; si no coincide con esta página, la comprobación automática falla.</p>
<div class="grid">{facts}</div>
<h2>Lo que no es</h2>
<div class="notice"><p>No es un servicio en producción: no tiene proveedor de identidad real ni autenticación de múltiples factores. WhatsApp, correo y pagos están <b>simulados</b> y se rotulan así. Los datos son ficticios. No promete ahorros ni resultados.</p></div>
<h2>Hablemos</h2>
<p>Si tu negocio tiene un flujo donde las aprobaciones y la trazabilidad hoy viven en chats y planillas, conversemos sobre un <b>piloto a medida</b>.</p>
<div class="btns"><a class="btn p" href="{E(cat['contacto']['linkedin'])}">LinkedIn</a><a class="btn" href="{E(cat['contacto']['portafolio'])}">Portafolio</a></div>
</main>
<footer>Código visible, todos los derechos reservados (ver <a href="{E(repo)}/blob/main/LICENSE">licencia</a>). Datos ficticios; el software se entrega «tal cual».</footer>
</div></body></html>
"""


if __name__ == "__main__":
    out = HERE / "index.html"
    out.write_text(build())
    print(f"escrito {out.relative_to(F.ROOT)} ({out.stat().st_size} bytes)")
