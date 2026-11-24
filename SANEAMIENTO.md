# Saneamiento — qué se revisó antes de publicar y qué se hará con cada hallazgo

**Fecha:** 2026-11-24 · **Origen:** repositorio privado `Arcan17/ai-company-os` (244 archivos rastreados) · **Alcance:** solo lectura sobre lo que Git rastrea (lo que una copia incluiría). El repositorio privado no se modificó.

## 1. Resultado de la auditoría (comandos reproducibles)

| Categoría | Cómo se buscó | Hallazgo | Acción en la vitrina |
|---|---|---|---|
| Rutas absolutas del Mac (`/Users/<usuario>/…`) | `git grep -I -l "/Users/<usuario>"` | **0 archivos** | La comprobación automática lo seguirá exigiendo |
| Correos reales (gmail/hotmail/outlook/yahoo/icloud) | `git grep -I -n -E "…@(gmail\|hotmail\|…)"` | **0** (los correos de la demo son `*.test`) | Ídem; el contacto público lo define Bastian |
| Secretos (claves, tokens, contraseñas literales) | `git grep -I -n -i -E "(api_key\|secret\|token\|password)\s*[:=]\s*'…16+"` | **0 reales.** 4 coincidencias son `retry_secret` de **pruebas** con valores ficticios | Se conservan (son datos de prueba); la comprobación las excluye por ruta `backend/tests/` |
| Archivos sensibles rastreados (`.env`, `.pem`, `.key`, `.sqlite`, `.db`, `.log`, `.bak`) | `git ls-files \| grep …` | **0** (`backend/data/` está en `.gitignore`) | No se copia `backend/data/` |
| Contraseña de la demo (`demo1234`) | lectura | Aparece **a propósito** (cuentas ficticias de demostración, rotuladas como tales) | Se conserva con el aviso «cuentas FICTICIAS» |
| Capturas (53 PNG publicados) | revisión visual de `safari-02-login.png` y `safari-13-visualizador-en-vivo.png` + búsqueda de texto personal en los **metadatos** de los 53 | Recortes de la ventana del navegador con **datos ficticios** y `localhost`; **sin pestañas, marcadores ni datos personales**; metadatos sin coincidencias | Se conservan |
| Nombres de herramientas / flujo de trabajo interno | `git grep` de `Codex`, `ChatGPT`, `Claude Code`, `NEXT_EXACT_STEP`, `checkpoint` | **Sí**: `Codex` (14 archivos, casi todo comentarios de pruebas: «revisión del PR… (Codex)»), `ChatGPT` y `NEXT_EXACT_STEP` en 3 documentos de `docs/v07` y `docs/ROADMAP_V1_PRODUCTO.md`, `Claude Code` y «checkpoint» en `docs/demo/DEMO_COMERCIAL_V04.md` | **Reescribir en la copia** (no en el privado): comentarios → «revisión automatizada del PR»; quitar las secciones `NEXT_EXACT_STEP`/«siguiente implementación para Claude Code» de los documentos; mantener las **historias de ingeniería** (son el valor) sin nombrar herramientas |
| Falso positivo a no tocar | `handoff` en un docstring de `orchestrator.py` | Es lenguaje de dominio («entrega entre personas»), no el flujo interno | La comprobación usa patrones **específicos**, no palabras sueltas |

## 2. Lo que NO se copia
`backend/data/`, `backups/`, `.venv`, `node_modules`, `frontend/dist*`, `graphify-out/`, `docs/qa/` (auditorías con modelos locales y capturas de QA), `docs/demo/DEMO_COMERCIAL_V04.md` (documento de planificación con el flujo interno; sus enlaces se reescriben) y **todo el historial de Git** (la vitrina empieza con commits propios: 1 solo autor, correo `noreply`). Los lanzadores `run_demo.sh`, `run_demo_comercial.sh` y `run_v05_lab.sh` **sí** se copian (las pruebas de seguridad del propio repositorio los exigen); no contienen rutas ni credenciales propias.

## 3. Lo que debe vigilar la comprobación automática (FR-007)
Patrones: rutas `/Users/<usuario>/…`; correos de proveedores públicos; `NEXT_EXACT_STEP`, `ChatGPT`, `Codex`, `Claude Code`, `memoria-conversaciones`; formas de clave (`sk-…`, `ghp_…`, `AKIA…`, bloques `-----BEGIN … PRIVATE KEY-----`); extensiones `.env`, `.pem`, `.key`, `.sqlite`, `.db`. Cada categoría tendrá su **mutación** (se planta un caso y la comprobación debe fallar), como en el resto del proyecto.

## 4. Decisiones y publicación
Licencia (código visible, todos los derechos reservados), contacto (LinkedIn y portafolio, no el correo) y política de precio (sin precio público) **aprobadas por el autor el 2026-11-24**, igual que la creación del repositorio público. Ver `specs/001-showcase-portfolio-offer/spec.md` §7.

## 5. Cómo repetirlo
`python tools/export_showcase.py --src ../ai-company-os` (copia y reescribe) y `python tools/check_showcase.py` (debe dar OK); el CI lo ejecuta en cada cambio.
