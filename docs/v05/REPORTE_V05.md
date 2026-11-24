# V0.5 — base configurable: informe

**Fecha:** 2026-11-22 · **Rama:** `feat/base-v05-configurable` (sobre `main` `f3f0079`) · **Issues:** [#5](https://github.com/Arcan17/ai-company-os/issues/5) (esta rama) y [#4](https://github.com/Arcan17/ai-company-os/issues/4) (seguridad Safari, resuelto primero) · **No fusionado**: pendiente de tu revisión del PR.

## 1. Qué permite ahora (sin editar código)
| Capacidad | Dónde |
|---|---|
| Crear una **empresa ficticia** (dueño/a + agentes compartidos + asistente del dueño + catálogo de industria) | Login › «Crear una empresa ficticia» · `python -m app.company_admin create` · API `POST /api/setup/companies` (**apagada por defecto**, solo loopback) |
| Incorporar **empleados con rol** (administración/técnico), 0..40 | «Empresa y equipo» (solo el dueño) · `POST /api/config/employees` |
| **Asignar asistentes personales desde plantillas**, activar/desactivar asignaciones y empleados, cambiar rol, con **auditoría** | «Empresa y equipo» · `/api/config/*` |
| El coordinador **delega entre empleados elegibles**: varios → pregunta (con carga de trabajo); ninguno → bloquea con explicación; nombres repetidos → pregunta | planificador V0.4 sobre `directory.py` |
| Flujos A–D en empresas configuradas: tareas a **sus** empleados, el menos cargado | `Orchestrator.principal`, `pick_assignee` |
| Registrar una **solicitud manual** (una empresa recién creada no tiene) | «Clientes y operaciones › Nueva solicitud» |
| Laboratorio V0.5 aislado | `./run_v05_lab.sh` (BD propia, puerto libre desde 8750) |

Reglas de seguridad: solo el dueño administra; **sin autoelevación** (nadie cambia su propio rol ni se desactiva; no hay segundo dueño); sin acceso entre empresas («no encontrado», sin filtrar existencia); un asistente actúa con **plantilla ∩ rol** (configurarlo no da permisos); no se desactiva ni cambia de rol a quien tiene tareas abiertas; la contraseña generada se muestra una vez y nunca se guarda en auditoría.

## 2. Issue #4 — Safari (primero)
- Se **eliminó** `qa/safari_check.py` (AppleScript que creaba/redimensionaba/cerraba ventanas). Sustituto: `qa/safari_manual_checklist.py` (solo HTTP + checklist impresa; **no toca navegadores**).
- Los lanzadores ya no abren navegadores (`run_demo.sh` solo con `AICOS_OPEN_BROWSER=1`; `run_demo_comercial.sh` sin `--open`); `run_demo_comercial.sh test` no llama a Safari ni habilita automatización.
- `backend/tests/test_qa_safety.py` (7 tests): ningún artefacto del repo puede usar AppleScript/safaridriver/captura de pantalla/System Events/crear-redimensionar-cerrar ventanas; Playwright solo lanza Chromium headless explícito; el checklist corre con **shims** de `osascript`/`open`/`safaridriver`/`screencapture` que fallan si se invocan (no se invocan) y se valida contra un servidor local real.
- Docs actualizados: los «13/13 en Safari» de V0.4 validaban **renderizado/cookies/SSE**, no interacción ni seguridad de ventanas. No se afirma recuperación de pestañas.
- Este trabajo **no volvió a automatizar Safari** ni tocó tus ventanas.

## 3. Pruebas (cantidades reales, ejecutadas en el Mac sobre esta rama)
| Suite | Resultado |
|---|---|
| pytest | **145/145 PASS** (98 de V0.4 + **7** seguridad QA + **40** V0.5 (35 + **5** de la revisión del PR #6): creación y validación de empresas, CLI, empleados/roles/plantillas, RBAC, sin autoelevación, elegibilidad, desactivación, cambio de rol, varios administradores, nombres repetidos, bloqueo sin elegibles, **tercera empresa con plan completo**, aislamiento entre 2 empresas configuradas, solicitudes manuales, empresas sembradas intactas, reinicio, **migración V0.4→V0.5**, LLM no elige personas) |
| Playwright regresión | **12/12 PASS** (los 11 anteriores A, B, C, D, E1, E2… + **1 nuevo** de la tercera empresa con 4 sesiones de navegador) |
| Auditoría V0.4 (sobre una **copia de la base real V0.4 migrada**) | **119/119 PASS** |
| Guion comercial V0.4 | **4 pruebas / 29 comprobaciones PASS** |
| **E2E V0.5** (`v05_third_company.spec.ts`) | **16/16 comprobaciones PASS** (abajo; V5-16 añadida por la revisión) |

Un test de V0.4 (`test_commitments_process_due_is_idempotent_and_cancellable`) dependía de la hora (fallaba después de las 09:00 UTC); se corrigió con un reloj fijo.

### 3.1 Tercera empresa creada y configurada desde la UI — comprobaciones
| ID | Qué se comprobó | Veredicto |
|---|---|---|
| V5-01 | Empresa «Aire Austral» creada desde la UI: owner + agentes compartidos + asistente del dueño + catálogo ficticio, sin tocar código | **PASS** |
| V5-02 | Una persona sin asistente aparece NO elegible; 5 empleados con rol y plantillas visibles | **PASS** |
| V5-03 | Activar/desactivar asignaciones y asignar plantillas quedan AUDITADOS en SQLite | **PASS** |
| V5-04 | Las plantillas muestran capacidades derivadas del registro de herramientas (p. ej. tasks.complete), no un editor de código | **PASS** |
| V5-05 | Con 2+ administradores elegibles el coordinador PREGUNTA (muestra carga de trabajo) y no toma «el primero»; la persona sin asistente reactivada también aparece | **PASS** |
| V5-06 | Plan propuesto con los responsables ELEGIDOS por la dueña (Rodrigo y Camila), 0 tareas antes de confirmar | **PASS** |
| V5-07 | Confirmar (doble clic): exactamente 2 tareas y 2 delegaciones, a Rodrigo (pendiente) y Camila (en espera) | **PASS** |
| V5-08 | La OTRA administradora (Javiera) NO recibe la tarea; el técnico elegido la ve «en espera» | **PASS** |
| V5-09 | Otra administradora no puede cerrar la tarea de Rodrigo (400); nada cambia | **PASS** |
| V5-10 | El asistente de Rodrigo responde el stock del catálogo de SU empresa (GAS-R410: 12 u.) | **PASS** |
| V5-11 | Plan `completed`: ambas tareas `hecha`, cerradas por SUS responsables con la evidencia escrita | **PASS** |
| V5-12 | Visualizador: los 51 eventos mostrados = filas de `events` (mismo orden); 2 delegaciones y 2 cierres con evidencia | **PASS** |
| V5-13 | Acción prohibida en la empresa nueva: plan BLOQUEADO, 0 pasos, 0 tareas | **PASS** |
| V5-14 | Clima Sur no ve el plan de Aire Austral (404) ni sus empleados; sus 3 empleados y 5 agentes siguen intactos | **PASS** |
| V5-15 | Una administradora no puede abrir la configuración de la empresa (403) | **PASS** |

Capturas: `docs/v05/screenshots/01…09` · resultados: `docs/v05/v05-results.json`.

### 3.2 Revisión del PR #6 (revisión automatizada + comentario del propietario): 3 defectos corregidos
Para cada uno se escribió primero la prueba de regresión y **se comprobó que fallaba** con el código de `341bd54` (salida pegada en los hilos del PR), luego se corrigió.

| # | Defecto | Prueba que fallaba antes | Corrección |
|---|---|---|---|
| 1 | `sync_agents()` (en cada arranque) creaba y habilitaba el asistente del **único** empleado de un rol aunque el dueño lo hubiera configurado **sin** asistente | `test_restart_never_assigns_an_assistant_to_the_only_employee_of_a_role` (`assert 1 == 0`: se creó la asignación) | La reparación de asistentes personales solo ocurre en empresas **claramente heredadas**: sin ninguna auditoría `config.*` y sin ningún asistente personal. Una empresa configurada conserva exactamente lo que asignó su dueño. Guarda del caso legado: `test_restart_still_repairs_a_legacy_company_…` |
| 2 | `set_agent_enabled()` reactivaba la plantilla del **rol anterior** (técnico→admin) y `availability.technicians` solo miraba el asistente, no `u.role`: las franjas retenidas hacían que el administrador pudiera ser elegido para una visita técnica | `test_cannot_reenable_the_assistant_template_of_a_previous_role` (devolvía 200) y `test_availability_never_offers_slots_of_someone_who_is_no_longer_a_technician` (Camila, ya administradora, aparecía en las franjas) | `set_agent_enabled` aplica la misma regla plantilla↔rol que `assign_template` (400); `availability.technicians` exige `u.role='technician'`; `visits.propose` rechaza franjas de quien ya no es técnico (defensa en profundidad); el overview marca `compatible` y la UI muestra «plantilla de su rol anterior» sin botón |
| 3 | `reset_company_scenarios()` solo seleccionaba solicitudes `chat:%` y conversaciones `chat_web`, pero borraba **todos** los clientes no sembrados: una solicitud manual (`manual:%`, conversación `interno`) de un cliente nuevo quedaba **huérfana**; además los planes del coordinador (eventos/tareas/delegaciones/resultados bajo el id del plan) no se limpiaban | `test_scenario_reset_leaves_no_dangling_references_after_manual_requests_and_plans` (`requests→customers: 1`, `conversations→customers: 1`) | El reset trata como datos de escenario las solicitudes `chat:%`, `manual:%` y las de clientes no sembrados, sus conversaciones (`chat_web`/`interno`) y mensajes, **todos** los planes de la empresa y lo que cuelga de ellos; el test comprueba 13 relaciones sin referencias colgantes, que lo sembrado sobrevive y que **otra empresa queda intacta** |

## 4. Migración V0.4 → V0.5
- Cambia **una** cosa no aditiva: la tabla `agents` (SQLite no puede quitar un `UNIQUE`): se **copia el archivo** (`<db>.pre-v05-<fecha>.bak`, API de backup) y se reconstruye `agents` en **una transacción**, con índices parciales `uq_agents_shared/personal`. `users.active` es aditivo (relleno a 1). Idempotente (la 2.ª vez no hace nada ni copia).
- **Probada sobre copias** de `aicos.db` (laboratorio) y `demo_comercial.sqlite3`: mismos 10 agentes/6 usuarios, `integrity_check = ok`, login, dashboard y planes históricos visibles, y al añadir un 2.º administrador el coordinador pregunta. Tests con un esquema V0.4 sintético (prueban que la restricción vieja existía y que la copia previa es del esquema viejo).
- **No se tocaron** `aicos.db`, `demo_comercial.sqlite3` ni los procesos :8731/:8743 (siguen con el código V0.4 que cargaron). Al arrancar **código V0.5 sobre esas bases** se aplicará esta migración (con copia previa automática). Se hicieron copias de seguridad adicionales en `backend/data/backups/pre-v05-*`.
- La UI V0.5 se compila en `frontend/dist-v05` (variable `BUILD_DIR`); `frontend/dist` no se modificó para no alterar lo que sirven :8731/:8743.

## 5. Limitaciones y riesgos (explícitos)
1. **No es un SaaS**: crear empresas es una acción **local/demo** sin identidad de plataforma (por eso está apagada por defecto, 404 fuera de loopback y limitada a 25 empresas). No hay registro público, usuarios reales, MFA, CSRF, TLS, recuperación de contraseña ni PostgreSQL.
2. Agentes **solo por plantilla y asignación**: no hay editor de prompts, herramientas ni permisos; las 5 plantillas están en código.
3. El **visualizador** tiene nodos fijos por rol: con varios administradores/técnicos un nodo representa al *rol*; qué persona actuó se ve en los títulos de eventos, el inspector («Evidencia: …») y SQLite.
4. «Elegible» exige asistente **activo**: una persona sin asistente no recibe trabajo del coordinador (se explica al dueño). Los flujos A–D eligen al elegible **menos cargado** (balanceo simple, no una política de negocio configurable).
5. Reasignar tareas abiertas de una persona al desactivarla **no existe**: la operación se rechaza hasta cerrarlas o cancelar el plan.
6. Catálogo inicial por industria: 3 presets en código (hvac, taller, genérico); no hay gestión de catálogo/precios por empresa en la UI.
7. Canales externos (WhatsApp, correo, teléfono, pagos): **no existen**; Ollama sigue siendo opcional y solo propone (probado: no elige personas).
8. `/api/demo/reset-scenarios` no aplica a empresas configuradas (responde 400).

## 6. Cómo comprobar la tercera empresa
[`GUIA_NUEVA_EMPRESA.md`](GUIA_NUEVA_EMPRESA.md): `./run_v05_lab.sh` → crear «Aire Austral» → 4 empleados + 1 sin asistente → plan con elección explícita de administración y técnico → evidencias → visualizador y SQLite.
