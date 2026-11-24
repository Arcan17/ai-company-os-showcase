# Arquitectura (v0.5)

```
frontend (Vite+React+TS+Tailwind+React Flow)  ──HTTP/SSE──▶  backend FastAPI
                                                              ├─ api.py          sesión → empresa; permisos de rol (403/404)
                                                              ├─ engine/         Orchestrator + Flow: máquina de estados por fases
                                                              ├─ tools/          registro: permiso → validación → transacción → auditoría
                                                              ├─ providers/      determinista (defecto) · Ollama local (opcional)
                                                              ├─ events.py       log de eventos persistente (fuente del visualizador)
                                                              └─ db.py           SQLAlchemy Core (SQLite → PostgreSQL cambiando AICOS_DB_URL)
```
El backend sirve el frontend compilado: **un solo proceso**, mismo origen (cookie de sesión y SSE sin proxys). Sin Redis, Celery ni colas: el motor corre en un hilo por ejecución y SQLite en modo WAL; cuando haga falta escalar, el motor es el único componente que habría que sacar a un worker.

## Decisiones clave
- **Un orquestador, roles lógicos.** Los 5 «agentes» son roles con permisos y herramientas, no 5 modelos. El flujo de negocio es código determinista; el LLM, si se activa, solo *propone* una clasificación (enum cerrado + SKUs validados contra el catálogo + regla semántica «solo reparaciones llevan repuestos»; cualquier fallo → reglas, y queda registrado). No recibe herramientas ni puede escribir.
- **Permisos.** Humano = permisos de su rol. Asistente personal = permisos del agente **∩** permisos del rol del empleado (nunca excede a su humano). `approvals.decide` solo lo tienen humanos con rol dueño: ningún agente puede aprobar gasto.
- **Aislamiento.** La empresa sale **solo** de la sesión; cada consulta de dominio filtra por `company_id`; un id ajeno responde igual que uno inexistente (404, sin filtrar existencia).
- **Idempotencia.** Claves únicas a nivel de BD: `requests(company, idem_key)`, `quotes(request, kind)`, `approvals(run, subject)`, `visits(request)`, `tasks/delegations/materials(idem_key)`; reintentar el mismo mensaje devuelve la misma ejecución; decidir dos veces igual es no-op, distinto → 409; reanudar una ejecución usa compare-and-set.
- **Eventos.** `events(run_id, seq)` inmutable. El grafo se *deriva* reproduciendo eventos (`derive()` en `Flows.tsx`), así el modo en vivo (SSE, con `Last-Event-ID`), el historial y el paso a paso usan la misma lógica. Nada se anima sin evento.
- **Confirmación real.** El motor solo llega a `propuesta`; `confirmada` exige que el cliente responda con un código (endpoint público), registrado en auditoría y como evento.
- **MCP-ready.** `GET /api/tools` expone el registro con forma MCP (`name/description/inputSchema`). Un servidor MCP sería una fachada sobre `invoke()`; no sustituye a la BD.

## Fases del motor
`intake` (recepción → coordinador → administración → solicitud de aprobación) → `waiting_approval` | `blocked` → `decided` (aprobada: visita **propuesta**, orden, tareas, mensaje preparado; rechazada: alternativa) → `completed`. `blocked --resolve--> recover → waiting_approval`.

## Límites conocidos
Un hilo por ejecución dentro del proceso (se pierde una ejecución en vuelo si el proceso muere; el estado persistido permite reanudarla pero no hay reanudador automático) · SQLite no es seguridad multiempresa por sí sola (el aislamiento es de la capa de consultas; con PostgreSQL convendría añadir RLS) · cuentas demo con contraseña conocida · sin límites de tasa · el canal de cliente es una página de prueba.


## V0.3 — colaboración entre personas, compromisos y seguimiento
**Escenario D** (mantención): `intake → aprobación del presupuesto → waiting_technician → (técnico pide material por su asistente) → collab_admin → waiting_approval (gasto) → waiting_task (administración) → waiting_task (técnico) → completed`.
- **Estados de espera humana** (`waiting_technician`, `waiting_task`, `waiting_approval`, `blocked`): la ejecución no avanza hasta que una *persona* actúa desde su sesión. Cada transición usa compare-and-set (`UPDATE … WHERE status=…`), por lo que repetir una acción no la ejecuta dos veces.
- **Objetos persistidos con `run_id`**: `delegations`, `tasks(kind, evidence, commitment_id)`, `commitments`, `material_requests(run_id, cost)`, `approvals`, `results`, `messages(commitment_id)`. Todo cambio pasa por el registro de herramientas (permiso → validación → transacción → auditoría); los eventos de herramientas se emiten **después** del commit (`ctx.event`).
- **Cierre con evidencia (`tasks.complete`)**: solo el asignado; evidencia ≥ 10 caracteres; tarea de material exige solicitud `aprobado` y stock suficiente, y descuenta stock **una sola vez**; la recepción exige `entregado`. Un rechazo no modifica nada.
- **Compromisos (`commitments.*`)**: responsable, plazo, estado, origen, última evidencia. `process_due` (idempotente por CAS `reminder_sent` + claves únicas) genera recordatorio interno y, si el compromiso es de cara al cliente y la visita sigue sin confirmar, **un** borrador `preparado_no_enviado`. Cerrar, cancelar o reasignar cancela los recordatorios inaplicables; la confirmación real del cliente cancela borradores pendientes.
- **LLM (Ollama) con verificación en 3 capas**: esquema JSON cerrado (con opción `NINGUNO`) → validación del motor (SKU del catálogo, cantidad 1–10, orden propia del técnico) → **anclaje**: el SKU debe estar respaldado por las palabras del técnico. Motivo: en una prueba real, «necesito una pizza» hizo que qwen3.5 (4B y 9B) eligiera `GAS-R410` porque el esquema forzaba elegir; la verificación lo rechaza. Si falla, cae al parser determinista y queda registrado. El LLM nunca aprueba ni escribe.
- **Permisos de agentes sincronizados desde código al arrancar** (`seed.sync_agents`): hallazgo en vivo — una base sembrada por V0.2 no tenía `commitments.write` y D fallaba con `Denied`; los tests con base nueva no lo veían (ahora hay test de actualización). Mientras los permisos no sean editables por empresa, el código es la fuente de verdad.
- **Migración aditiva** (`db.migrate`): agrega tablas/columnas nuevas sin borrar datos; probada sobre una copia de la base real y en `test_schema_migration_keeps_data`.
- **Fallos**: `run.failed` guarda solo clase de error, fase y resumen seguro; el traceback completo va al log del servidor. La solicitud pasa a `ejecucion_fallida` y el panel muestra «requiere revisión». **No existe reintento** (manual ni automático): una ejecución es una cadena de efectos externos a la transacción (eventos, hilos), y reintentar una fase sin reconciliar sus efectos podría duplicarlos. Lo que sí está probado es que el estado de espera sobrevive a reinicios (`test_state_survives_server_restart_mid_collaboration`). Habilitar reintentos exige primero reconciliación de efectos por fase.

## Límites conocidos (V0.3)
Sin scheduler en segundo plano (los vencimientos se procesan a demanda) · una persona de administración por empresa recibe las tareas de material (la primera por id) · la ejecución sigue en un hilo del proceso · cuentas demo con contraseña conocida · sin límites de tasa · canal de cliente simulado · SQLite (PostgreSQL/RLS no probados).


## V0.4 — coordinador conversacional
```
dueño (humano) ──texto──► asistente del dueño / pantalla «Coordinador»
        │                          │ PlanEngine.create  (plans: draft)
        │                          ▼
        │            plans.lookup (lectura, principal coordinador)  ── datos reales de la empresa
        │                          ▼
        │      propuesta: reglas deterministas  |  LLM local (banderas cerradas)  ──► validación del motor
        │                          ▼
        │        plans + plan_steps persistidos  →  awaiting_user_confirmation | needs_clarification | blocked
        ▼
  CONFIRMA (acción humana, CAS)  ──►  PlanEngine.advance (reconciliador idempotente, con lock por plan)
                                       ├ plans.review / plans.lookup      (lectura)
                                       ├ plans.assign_task   → tasks + delegations (coordinator → *_assistant)
                                       ├ plans.activate_step (dependencia cumplida: en_espera → pendiente)
                                       ├ plans.verify_step   (lee el registro: tarea hecha + evidencia + stock real)
                                       └ plans.finish        (todos los pasos hechos → completed + results)
empleado ──Mi asistente──► tasks.complete (evidencia) ──► Orchestrator.complete_task ──► PlanEngine.on_task_event
```
Tablas nuevas (migración aditiva): `plans`, `plan_steps`. Las tareas del plan son `tasks(kind='plan_step', run_id=plan_id)`; las delegaciones, `delegations(run_id=plan_id)`. No se mezcla con `runs` (escenarios A–D).
Separación de identidades: **dueño** (crea/confirma/cancela, `plans.*` de rol) · **coordinador** (agente con `plans.assign/verify/finish`; sin `approvals.decide`, `tasks.complete` ni `plans.confirm`) · **asistentes personales** (permisos del agente ∩ rol del empleado).


## V0.5 — base configurable
- **Plantillas (código) + asignaciones (BD)**: `templates.py` expone las 5 plantillas (`reception`, `coordinator`, `owner_/admin_/tech_assistant`) con capacidades derivadas de `REGISTRY`. Una *asignación* es una fila `agents(company_id, role=plantilla, user_id)`: **un asistente personal por empleado**; los agentes compartidos (`user_id NULL`) siguen siendo uno por empresa. Unicidad: índices parciales `uq_agents_shared` / `uq_agents_personal`.
- **Elegibilidad** (`directory.py`): empleado `active` ∧ rol ∧ asistente de su plantilla `enabled`. `eligible_staff` ordena por carga de trabajo; `pick_assignee` (flujos A–D, pedidos de material, franjas de visita) usa el menos cargado, no el primero por id. El planificador (V0.4) pregunta cuando hay varios y bloquea cuando no hay ninguno.
- **Identidad del agente**: `Orchestrator.principal(company, rol, user_id)` → asistente DE ESE empleado; permisos efectivos = plantilla ∩ rol del humano. Configurar una asignación nunca otorga permisos (la configuración la hace el dueño con `config.manage`, permiso humano, no de agentes).
- **Configuración** (`company_admin.py`, API `/api/config/*`, `/api/setup/*`, CLI): crear empresa (preset de catálogo), alta de empleados con rol, asignar/activar/desactivar, cambio de rol, desactivar empleado (cierra sus sesiones). Todo en transacción, validado, auditado (`config.*`) y acotado a la empresa del dueño. Rechaza desactivar/cambiar a quien tiene tareas abiertas.
- **Migración**: `db._rebuild_agents_if_v04` (única migración no aditiva): copia previa del archivo + reconstrucción de `agents` en una transacción; `users.active` aditivo con relleno. Probada sobre copias de las bases V0.4 reales y en tests con un esquema V0.4 sintético.
