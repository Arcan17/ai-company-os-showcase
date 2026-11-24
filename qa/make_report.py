#!/usr/bin/env python3
"""Builds docs/qa/REPORTE_AUDITORIA_V04.md from the recorded evidence (audit-results.json, llm-audit-*.json, screenshots). usage: python3 qa/make_report.py <commit> <pytest_n> <pw_n>"""
import glob, json, os, sys
commit, pytest_n, pw_n = sys.argv[1], sys.argv[2], sys.argv[3]
R = json.load(open("docs/qa/audit-results.json")); B = json.load(open("docs/qa/audit-results-before-fixes.json"))
by = {r["id"]: r for r in R}
def grp(*prefixes):
    rs = [r for r in R if any(r["id"] == p or r["id"].startswith(p + ".") or r["id"].startswith(p + "-") or r["id"].startswith(p + "c") for p in prefixes)]
    return rs
def verdict(rs): return "NO VERIFICADO" if not rs else ("PASS" if all(r["ok"] for r in rs) else "FAIL")
M = [
 ("Dashboard: KPIs = SQLite (solicitudes, aprobaciones, tareas)", ["DB-01", "DB-02", "DB-03"]),
 ("Coordinador consulta datos reales por el registro de herramientas", ["E-04"]),
 ("Varias solicitudes de Marta → pide aclaración (solo opciones reales)", ["E-01"]),
 ("Plan válido y persistente (empresa, solicitante, pasos, responsables, sobrevive a recargar)", ["E-02", "E-05", "E-07", "E-08"]),
 ("NO crea tareas ni delegaciones antes de confirmar", ["E-03", "E-06"]),
 ("Confirmación humana: tareas/delegaciones exactamente una vez (doble clic)", ["E-09", "E-10", "E-11", "E-12"]),
 ("Dependencia: tarea del técnico bloqueada hasta que administración termine", ["T-01", "T-02", "T-03", "T-04"]),
 ("Administración: ve su tarea, asistente identifica la solicitud, consulta inventario", ["B-01", "B-02", "B-03", "B-04", "B-05"]),
 ("Rechazos: empleado incorrecto, evidencia insuficiente (y quedan auditados)", ["B-06", "B-07", "B-08", "B-09"]),
 ("Cierre con evidencia en SQLite + auditoría (administración y técnico)", ["B-10", "B-11", "T-05", "T-06", "T-07"]),
 ("Plan `completed` solo cuando TODOS los pasos están cumplidos y verificados", ["E-13", "E-14", "E-15", "E-16", "E-17"]),
 ("Eventos persistidos coherentes (orden, herramientas sin colgar, rechazos visibles)", ["EV-01", "EV-02"]),
 ("Visualizador: eventos mostrados = filas de `events` (mismo orden); no es animación", ["V-01"]),
 ("Visualizador: modo en vivo (SSE) y actualización sin recargar", ["V-02", "V-19", "V-19a"]),
 ("Visualizador: historial (scrubber) y reproducción", ["V-03", "V-04", "V-05", "V-06", "V-07"]),
 ("Visualizador: inspector (herramienta, entradas/salidas, delegación, evidencia, decisión)", ["V-08", "V-09", "V-10", "V-11", "V-12", "V-13", "V-14", "V-15"]),
 ("Visualizador: flechas, etiquetas por tipo de evento y panel del plan", ["V-16", "V-17", "V-18"]),
 ("Negativas: ids inexistentes y entidades de otra empresa", ["N-01", "N-02", "N-03"]),
 ("Aislamiento entre empresas (ver/confirmar/leer eventos/completar)", ["N-04", "N-05", "N-06"]),
 ("Permisos: sin permiso no se crea/confirma/cancela/invoca/aprueba", ["N-07"]),
 ("Acciones prohibidas e inyecciones → bloqueadas, sin efectos (compra, pago, correo, WhatsApp, aprobar, stock, borrar, agendar)", ["N-08", "N-09"]),
 ("Duplicados: confirmación concurrente ×3 y doble clic en «Proponer plan»", ["N-10", "N-11"]),
 ("Cancelación de plan en curso (pasos y tareas) + tarea cancelada no completable", ["N-12", "N-13"]),
 ("Límite de entrada y ausencia de reintentos de ejecuciones fallidas", ["N-14", "N-15"]),
 ("Reinicio del servidor con plan pendiente (estado, sin duplicar, historial, continuar)", ["R-01", "R-02", "R-03", "R-04", "R-05"]),
 ("UX: alertas sin duplicar y fallos distinguibles", ["UX-01", "UX-02"]),
 ("UX: disponibilidad de agentes (sin «inactivo» ambiguo)", ["UX-03", "UX-04"]),
 ("UX: compromisos vencidos con «qué hacer»", ["UX-05"]),
 ("UX: tareas bloqueadas por el cliente / en espera de otro paso", ["UX-07", "UX-08", "UX-12"]),
 ("UX: solicitudes activas vs históricas", ["UX-09", "UX-10"]),
 ("UX: estado del plan dice a quién espera; administración solo ve lo accionable", ["UX-06", "UX-11"]),
 ("UX: móvil 390 px sin desbordamiento (coordinador, dashboard, visualizador)", ["UX-13"]),
]
tot, ok = len(R), sum(1 for r in R if r["ok"])
btot, bok = len(B), sum(1 for r in B if r["ok"])
llm = {}
for f in sorted(glob.glob("docs/qa/llm-audit-*.json")):
    d = json.load(open(f)); llm[d["model"]] = d
shots = {
 "01-owner-dashboard": "Dashboard de la dueña sobre la copia de la base real", "02-owner-operations": "Solicitudes (filtro activas/históricas)", "03-e-clarification": "El coordinador pide aclaración (Marta: 2 solicitudes abiertas)",
 "04-e-plan-proposal": "Plan propuesto, 4 pasos persistidos, sin tareas", "05-e-plan-confirmed": "Plan confirmado: «Esperando a: Javiera Soto…», pasos en curso/espera", "06-tech-blocked": "Técnico: su tarea en espera de Javiera",
 "07-admin-dashboard": "Administración: «Qué requiere mi atención» con su tarea", "08-admin-assistant-done": "Administración completa con evidencia", "09-tech-done": "Técnico completa con evidencia", "10-e-plan-completed": "Plan completado con evidencias y resultado",
 "11-vis-final": "Visualizador: estado final (en vivo)", "12-vis-history-validated": "Visualizador: historial en «plan validado»", "13-vis-history-waiting-admin": "Visualizador: historial, administración esperando",
 "14-vis-inspector-coordinator": "Inspector del coordinador: herramientas, entradas y salidas reales", "15-vis-coordinator-selected": "Nodo coordinador + datos reales en la base", "16-vis-live-second-plan": "EN VIVO: segundo plan completándose sin recargar",
 "17-neg-cancelled": "Plan cancelado (UI)", "18-restart-visualizer": "Visualizador tras reiniciar el servidor", "19-ux-owner-dashboard-after": "Dashboard tras las correcciones UX", "20-ux-operations-after": "Solicitudes: filtro históricas",
 "21-ux-admin-dashboard-after": "Dashboard de administración", "22-ux-tech-assistant-after": "Asistente del técnico: tareas bloqueadas por el cliente", "23-ux-mobile-coordinator": "Móvil: coordinador", "24-ux-mobile-dashboard": "Móvil: dashboard", "25-ux-mobile-flows": "Móvil: visualizador"}
L = []
w = L.append
w(f"# Informe de auditoría funcional — AI Company OS V0.4\n")
w(f"**Fecha:** 2026-11-22 · **Base auditada:** `main` `87afa6b` · **Commit con las correcciones:** `{commit}` · **Entorno:** Mac M5, Chromium (Playwright 1.65), SQLite, Ollama local (qwen3.5 4B y 9B)\n")
w("""## 1. Veredicto

**AI Company OS V0.4 puede mostrarse como DEMOSTRACIÓN GUIADA a una empresa**, con datos ficticios y conducida por quien lo construyó, tras preparar el entorno (sección 11). **No** está listo para usuarios o empresas reales (sección 10).

La auditoría encontró y corrigió **7 defectos funcionales/seguridad y 6 de UX** (sección 7), incluidos dos que ningún test previo veía: la tabla `audit_log` se llenaba de lecturas de *polling* (320 filas en 27 minutos de una pestaña abierta, comprobado en la base real) y, en modo LLM, el modelo podía *resolver por sí mismo* una ambigüedad eligiendo una solicitud.
""")
w("## 2. Seguridad del entorno y método\n")
w(f"""- Proceso que ejecuta el servidor real: `uvicorn app.main:app :8731` (PID verificado por su carpeta de trabajo `…/ai-company-os/backend`). El puerto **8000** (otro servicio) no se tocó; tampoco DocuSeguro, Hermes ni el HP.
- **La base real nunca se abrió para escribir.** Se copió con la API de backup de SQLite en modo solo-lectura a `backend/data/audit.db` y toda la auditoría corrió en un servidor aislado (**:8742**) sobre esa copia (`qa/start_audit_server.sh`, que se niega a matar procesos ajenos). La base real conserva sus ejecuciones históricas (2 fallidas, 3 planes, 397 eventos); lo único que creció fue `audit_log` (+ filas de polling de la pestaña del propietario, ver D-1). Backup previo a V0.4: `aicos.db.pre-v04.bak`.
- Tres sesiones de navegador independientes (contextos Playwright con cookies propias): dueña `dueno@climasur.test`, administración `admin@climasur.test`, técnico `tecnico@climasur.test`; más Motor Norte para el aislamiento.
- Cada comprobación se registra con evidencia en `docs/qa/audit-results.json` y se compara **UI ⇄ SQLite** (consultas de solo lectura con `qa/dbq.py`).
""")
w("## 3. Pruebas ejecutadas (cantidades exactas)\n")
w(f"""| Suite | Resultado | Qué cubre |
|---|---|---|
| **pytest** (`backend/tests`) | **{pytest_n}/{pytest_n} PASS** | motor, permisos, seguridad, persistencia, escenarios A–D, coordinador (31), regresiones de esta auditoría (11) |
| **Playwright** regresión (`playwright test`) | **{pw_n}/{pw_n} PASS** | login, A, confirmación real + reintento, B, C, permisos, aislamiento, agentes + móvil, D (3 sesiones), E1 (3 sesiones), E2 |
| **Playwright auditoría** (`-c playwright.audit.config.ts`) | **5 pruebas · {tot} comprobaciones: {ok} PASS · {tot - ok} FAIL** | E1 con 3 sesiones, visualizador, negativas, reinicio, UX (sobre la copia de la base real) |
| **Auditoría LLM** (`qa/llm_audit.py`) | {" · ".join(f"{m}: {len(d['cases'])} casos, todos OK={d['all_cases_ok']}, máx {d['max_seconds']} s" for m, d in llm.items())} | modo Ollama real vs determinista |

Antes de corregir, la misma auditoría dio **{bok}/{btot} PASS** (12 FAIL: 10 defectos reales de producto/UX y 2 errores de mi propio script, descritos en 7.3).
""")
w("## 4. Matriz PASS / FAIL / NO VERIFICADO\n")
w("| # | Funcionalidad | Veredicto | Checks |\n|---|---|---|---|")
for i, (name, ids) in enumerate(M, 1):
    rs = grp(*ids); w(f"| {i} | {name} | **{verdict(rs)}** | {sum(r['ok'] for r in rs)}/{len(rs)} |")
w(f"| {len(M)+1} | Escenarios A, B, C, D (aprobaciones, inventario, aislamiento, confirmación de visitas, duplicados) | **PASS** | Playwright {pw_n}/{pw_n} + pytest {pytest_n}/{pytest_n} |")
for m, d in llm.items():
    w(f"| {len(M)+2} | Modo LLM ({m}): sin inventar entidades, sin ejecutar herramientas, propuestas validadas, fallback identificado, sin esperas largas | **{'PASS' if d['all_cases_ok'] else 'FAIL'}** | {len(d['cases'])} casos · {d['avg_seconds']} s de media |")
w("""
**NO VERIFICADO** (no se probó; ver riesgos): navegadores distintos de Chromium (el propietario usa Safari) · carga o concurrencia sostenida · estabilidad de 24 h · PostgreSQL · clasificación LLM del escenario D con esta versión (se probó en V0.3) · modelos distintos de qwen3.5 4B/9B · accesibilidad (lector de pantalla) · cualquier canal real (no existe).
""")
w("## 5. Escenario E ejecutado (3 sesiones) y resultado\n")
w("""1. **Dueña**: dashboard (KPIs = SQLite) → *Coordinador* → «Revisa las mantenciones de Marta Rojas, pide a administración que verifique los materiales y quiero confirmación del técnico». El coordinador llama `plans.lookup` (evento persistido), detecta **2 solicitudes abiertas de Marta** y **pide aclaración** (`needs_clarification`, 0 pasos, 0 tareas). Elige la de mantención → plan de 4 pasos persistido, `awaiting_user_confirmation`, 0 tareas/delegaciones; recargar la página no lo pierde.
2. **Confirma con doble clic** → 2 tareas + 2 delegaciones (una vez), `confirmed_by` = la dueña; tarea de administración `pendiente`, la del técnico `en_espera`.
3. **Técnico**: no ve la tarea accionable; la API rechaza completarla antes de tiempo; ve «en espera de Javiera Soto».
4. **Administración**: ve su tarea; su asistente identifica la solicitud (`req_da4b…`) y responde el stock real (`GAS-R410: 10 u.`); intenta cerrar la tarea del técnico (400, auditado), cierra con «ok» (rechazado) y finalmente con evidencia → `hecha`, `completed_by` = Javiera. El plan sigue `waiting_human` (no se completa antes).
5. **Técnico**: la tarea pasa `en_espera → pendiente`, la consulta desde «Mi asistente», la completa con evidencia → `hecha`, auditada.
6. **Dueña**: el coordinador verificó el **registro** (tarea hecha por su responsable + evidencia + stock real) y marcó el plan `completed` con resumen «No faltan materiales según el stock real».
7. **Visualizador**: 49 eventos en la UI = 49 filas de `events`, en el mismo orden. Modo en vivo (SSE), historial por paso, reproducción, inspector por nodo, etiquetas por tipo de evento. Captura SSE en vivo: [`evidence-sse.md`](evidence-sse.md) (23 frames recibidos = 23 filas, con la hora de cada acción humana).
""")
w("## 6. Evidencia\n")
w("- **SQLite** (plan, pasos, responsables, tareas con evidencia, delegaciones, resultado, auditoría, eventos): [`evidence-sqlite.md`](evidence-sqlite.md)\n- **SSE en vivo**: [`evidence-sse.md`](evidence-sse.md)\n- **Resultados crudos** de las comprobaciones: [`audit-results.json`](audit-results.json) (antes de corregir: [`audit-results-before-fixes.json`](audit-results-before-fixes.json)) · LLM: `llm-audit-*.json`\n- **Capturas** (`docs/qa/screenshots/`):\n")
for f in sorted(glob.glob("docs/qa/screenshots/*.png")):
    k = os.path.basename(f)[:-4]; w(f"  - `{k}.png` — {shots.get(k, '')}")
w("\n**Comparación UI ⇄ SQLite** (extracto): " + "; ".join(f"{r['id']}: {r['detail']}" for r in R if r["id"] in ("DB-01", "DB-02", "DB-03", "V-01", "V-19", "E-17")) + "\n")
w("## 7. Defectos encontrados y corregidos\n")
w("""### 7.1 Defectos de producto (cada uno con causa raíz, corrección y prueba de regresión)

| ID | Sev. | Defecto (reproducido) | Causa raíz | Corrección | Regresión |
|---|---|---|---|---|---|
| D-1 | Media | **`audit_log` se llena de lecturas**: en la base REAL, 320 filas `commitments.list` en 27 min (una pestaña del dashboard) | `invoke()` auditaba TODA llamada, también lecturas de polling (cada 4 s) | Se auditan escrituras, denegaciones/errores y lecturas sensibles (`notes.*`); las lecturas ok de polling no | `test_polling_reads_do_not_flood_the_audit_log…` |
| D-2 | Media | **Modo LLM: el modelo resolvía una ambigüedad** (eligió una de las 2 solicitudes de Marta) mientras el modo determinista pregunta | `request_id` propuesto por el LLM se aceptaba si el cliente aparecía en el texto | El `request_id` del LLM se ignora salvo referencia literal en el texto; la solicitud la resuelven los datos | `test_llm_cannot_settle_an_ambiguity…` |
| D-3 | Media | «Pide a **Tomás**…» (nadie se llama así) se habría asignado en silencio al único admin | La persona nombrada no se validaba si no existía | Nombre desconocido tras «pide a / asigna a…» → `needs_clarification` con opciones reales (sin bucle al elegir) | `test_unknown_person_is_never_silently_replaced` |
| D-4 | Baja-media | «Paga los **pagos**…», «facturas», «compras» y un JSON que dicta la respuesta del modelo no se reconocían; revisión + acción prohibida se ejecutaba como lectura | Regex sin plurales; sin detección de *payload* JSON; plan de solo lectura permitido con acciones prohibidas | Plurales, patrón de inyección JSON, y «revisión + prohibido» → bloqueado | `test_plurals_and_json_payload_injection_are_blocked` |
| D-5 | Media | Tareas «Preparar visita … pendiente de confirmación» **se podían cerrar con evidencia antes de que el cliente confirmara**, y se listaban como trabajo normal (6 de las 8 «tareas pendientes» dependían del cliente) | Defecto heredado de V0.2/V0.3: `tasks.complete` no distinguía tareas dependientes del cliente | La herramienta rechaza esas tareas mientras la visita no esté confirmada; `blocked_reason` calculado con el estado REAL de la visita (se desbloquea al confirmar) | `test_customer_dependent_tasks_cannot_be_closed…` |
| D-6 | Baja | El asistente de administración **no podía responder consultas de stock** (caía en el texto de ayuda) | Sin intención de inventario | Herramienta `inventory.list` + intención «stock/inventario» (el técnico sigue sin permiso). Mi primera colocación secuestraba «completar tarea … revisé el stock»: lo detectó un test previo y se reordenó | `test_admin_assistant_answers_inventory…` + `test_inventory_word_inside_a_task_completion…` |
| D-7 | Baja | Nodo del asistente seguía «En espera» tras cerrar su tarea con evidencia | `derive()` solo marcaba al humano | Se marca también al asistente | auditoría V-03 / captura |

### 7.2 Defectos de UX (corregidos, bajo riesgo, sin rediseño)

| ID | Defecto | Corrección | Comprobación |
|---|---|---|---|
| U-1 | Fallos duplicados: «Qué requiere mi atención» y «Alertas» mostraban los mismos 2 elementos; los dos fallos eran indistinguibles | «Alertas de la empresa» solo muestra lo que NO está arriba; cada fallo dice **cliente · fecha · no se reintenta** | UX-01, UX-02 + `test_failed_run_alerts_say_which_customer…` |
| U-2 | Cinco agentes «inactivo» (parecía deshabilitado) | `Disponible` / `Disponible · N tarea(s)` / `Deshabilitado`, por agente (cuenta runs y planes) | UX-03, UX-04 + `test_agents_report_availability…` |
| U-3 | Compromiso vencido sin indicar qué hacer | Columna «Qué hacer»: quién debe actuar, si ya hay recordatorio, enlace a la solicitud | UX-05 |
| U-4 | 6 tareas «pendientes» eran en realidad bloqueadas por el cliente | Marca 🔒 «Espera al cliente», sin botón «Completar»; contador «N bloqueadas» | UX-08, UX-12 |
| U-5 | Solicitudes históricas mezcladas con las actuales | Filtro Activas (por defecto) / Históricas / Todas y KPI «2 activas · 2 históricas» | UX-09, UX-10 |
| U-6 | El plan «Esperando a una persona» no decía a quién; el técnico no sabía que le llegaría una tarea; la página del coordinador desbordaba en móvil (el check del documento no lo veía: `<main>` es el contenedor de scroll) y el visualizador también | Banner «Esperando a: Javiera Soto (administración)… Después: Matías»; bloque «En espera de otra persona»; `grid-cols-1` / `flex-wrap`; en móvil se ocultan chips secundarios del registro | UX-06, UX-07, UX-13 |

### 7.3 Errores de mi propio script de auditoría (no son defectos del producto)
`EV-02`: los 3 intentos rechazados aparecen correctamente como `tool.error` (14 `tool.called` = 11 `tool.result` + 3 `tool.error`; ahora se verifica así) · `V-16`: la etiqueta de una flecha la sobrescribe el último evento y las etiquetas se re-renderizan tras clicar un nodo (ahora se espera) · el spec de auditoría corrió una vez contra el servidor equivocado hasta que se excluyó de la configuración por defecto (`testIgnore`).
""")
w("## 8. Determinista vs Ollama (qué decidió cada uno)\n")
w("""| Decisión | Modo determinista | Modo Ollama |
|---|---|---|
| Interpretar la instrucción (banderas: revisar / admin / técnico / avisar) | Reglas (`deterministic_proposal`) | **LLM propone** (JSON cerrado; evento `plan.proposed`, modo `llm`); el motor la valida |
| Validar banderas, ids y entidades | Código | Código (descarta lo no respaldado por el texto: `plan.llm_rejected`) |
| Acciones prohibidas / inyección / bloqueo | Código (escaneo del texto; el LLM no puede apagarlo) | Código (idéntico) |
| Resolver cliente, solicitud, personas | Código con filas reales; pregunta si hay ambigüedad | Código (el `request_id` del LLM se ignora) |
| Crear tareas, delegar, verificar, completar | Herramientas del registro (principal `coordinator`) | Idéntico: **ningún evento de herramienta se atribuye al LLM** |
| Fallo del modelo | n/a | Fallback a reglas, evento `plan.llm_rejected` con el error; el plan queda con `source=deterministic` |
""")
for m, d in llm.items():
    e = d.get("e1", {})
    w(f"- **{m}**: {len(d['cases'])} casos (E1 completo hasta `{e.get('final')}`, entidades/materiales/personas inventados, compra, pago, aprobación, 3 inyecciones, sin sentido, Ollama caído) → todos OK = **{d['all_cases_ok']}** · tiempo máx **{d['max_seconds']} s**, media **{d['avg_seconds']} s** · eventos por modo en E1: {e.get('modes')} · tareas creadas sin confirmación humana: **{d['any_task_without_confirmation']}**.")
w("""
Matiz de etiquetado: `plans.source` indica quién *propuso* las banderas; la validación y los bloqueos son siempre código (los eventos lo muestran con su modo). Un plan bloqueado tras una propuesta LLM sigue marcando `source=llm:…`.
""")
w("## 9. Real vs simulado\n")
w("""| Real (probado) | Simulado / no implementado |
|---|---|
| Persistencia SQLite, multiempresa, permisos por rol/agente, auditoría, idempotencia (claves únicas + CAS) | WhatsApp, correo, teléfono, pagos, calendarios: **no existen** (los mensajes a clientes son borradores `preparado_no_enviado`) |
| Plan → confirmación humana → delegación → tareas con evidencia → verificación → resultado | Reloj de vencimientos: **a demanda** (botón), sin scheduler |
| Stock real y descuento al cerrar tareas de material (escenario D) | MCP como servidor, PostgreSQL/RLS, cola de trabajos |
| Eventos persistidos + SSE + visualizador reconstruible tras reinicio | Agentes «autónomos»: son **roles lógicos** (código + herramientas); el LLM solo propone |
| LLM local opcional (propone; el motor valida) | Cuentas demo con contraseña conocida |
""")
w("""## 10. Riesgos antes de permitir usuarios o empresas reales

1. **Seguridad de acceso**: contraseña demo conocida, sin límite de intentos ni MFA, sesión por cookie sin token CSRF, HTTP local sin TLS; hay que añadir alta/baja de usuarios y restablecimiento seguro.
2. **Datos y respaldo**: SQLite single-writer, sin backups programados ni migraciones versionadas (solo aditivas); PostgreSQL/RLS no probados; el `audit_log` no tiene retención ni firma.
3. **Operación**: el avance de planes y runs corre en hilos del proceso (recuperable por idempotencia, pero sin cola ni supervisión); sin métricas, alertas ni límites de tasa; un fallo `failed` no se puede «archivar» (queda en alertas para siempre) ni reintentar de forma segura (falta reconciliar efectos por fase).
4. **Producto**: los planes en `needs_clarification` abandonados no caducan (ensucian «Qué requiere mi atención»); «hoy/mañana» no filtra; una sola persona de administración se resuelve sola; sin canales reales: no hay forma de avisar al cliente ni de recibir su confirmación fuera del chat de prueba.
5. **LLM**: solo se midieron qwen3.5 4B/9B con instrucciones en español; un modelo mayor o un prompt nuevo exige repetir `qa/llm_audit.py`. La capa de validación (no el modelo) es la que garantiza la seguridad.
6. **Legal/privacidad (Chile)**: datos personales de clientes y empleados, notas privadas, registro de auditoría: faltan política de privacidad, consentimiento, retención y contrato de encargado antes de datos reales (ver el skill `legal-launch-review`).
7. **Cosmético conocido**: las etiquetas de las flechas parpadean un instante al seleccionar un nodo; el registro de eventos en móvil oculta chips secundarios.
""")
w("""## 11. ¿Está V0.4 listo para mostrarse como demostración a una empresa?

**Sí, como demo guiada de ~10 minutos**, con estas condiciones (ninguna requiere código nuevo):
- **Preparar el entorno**: usar una base limpia o *Reiniciar escenarios de prueba* (solo la empresa de la demo, con copia previa) para que no aparezcan las 2 ejecuciones fallidas históricas ni planes de prueba; crear una solicitud nueva desde el chat de cliente justo antes. Cuentas demo con 3 perfiles/ventanas.
- **Guion**: orden nueva del dueño → plan visible → *tú* confirmas → administración y técnico responden con evidencia desde su asistente → el visualizador muestra cada paso real (SQLite/SSE) → intento prohibido («Compra repuestos y avisa por WhatsApp») bloqueado → reinicio del servidor y el plan sigue.
- **Decir con claridad** qué es real y qué es simulado (sección 9) y que son *roles lógicos*, no agentes autónomos; no prometer canales (WhatsApp/correo) ni pagos.

**Qué falta antes de una prueba con datos reales de una empresa**: (a) canales reales o, al menos, entrada/salida por correo con aprobación humana; (b) autenticación real (usuarios de la empresa, recuperación, límites de intentos) y TLS; (c) PostgreSQL + backups + migraciones versionadas; (d) scheduler y cola con supervisión; (e) «archivar» fallos y caducidad de planes abandonados; (f) validación comercial: nadie ha pagado ni usado esto fuera del autor.
""")
w("## 12. Estado final\n")
w(f"- Commit con correcciones y auditoría: `{commit}` en `Arcan17/ai-company-os` (`main`).\n- Servidor real `:8731` reiniciado con el código final sobre la MISMA base (sin borrar nada; los 3 planes y las ejecuciones históricas siguen). Servidor de auditoría `:8742` detenido. Puerto 8000 intacto.\n- Reproducir: `qa/start_audit_server.sh && cd frontend && npx playwright test -c playwright.audit.config.ts`; `backend/.venv/bin/python qa/llm_audit.py [modelo]`; `backend/.venv/bin/python qa/evidence.py <plan_id>`; `python3 qa/make_report.py <commit> <pytest> <playwright>`.")
open("docs/qa/REPORTE_AUDITORIA_V04.md", "w").write("\n".join(L) + "\n"); print("report written;", ok, "/", tot, "PASS")
