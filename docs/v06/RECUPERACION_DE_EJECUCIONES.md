# V0.6 · Recuperación segura de ejecuciones

**Fecha:** 2026-11-22 · **Rama:** `feat/v06-run-recovery` (apilada sobre el PR #9) · **Issue:** #8 · **Gate M1:** «ningún retry puede duplicar un movimiento».
Código: `backend/app/lease.py`, `Orchestrator.recover_runs`, cableado en `create_app`. Pruebas: `backend/tests/test_v06_recovery.py` (14, con **SIGKILL real** y **varios procesos servidor** sobre la misma base).

## 1. El problema (verificado en el código)

El motor ejecuta cada ejecución (`runs`) en **un hilo del proceso**. Si el proceso muere con la ejecución en `running`, el hilo muere con él y la fila queda `running` **para siempre**: el panel muestra «en ejecución» sin que nadie la ejecute. Solo existía reanudación para `plans` (`PlanEngine.recover`, reconciliador idempotente); para `runs`, ninguna (`ARCHITECTURE.md`: «no hay reanudador automático»).

## 2. Qué hace ahora

| Estado persistido al morir el proceso | Tras el reinicio |
|---|---|
| `runs.status = running` (había un hilo en vuelo) | → `failed` por *compare-and-set*, **un** evento `run.failed` con `reason: interrupted` y la fase, auditoría `runs.interrupted`, la solicitud pasa a `ejecucion_fallida` («requiere revisión»). **No se reintenta ni se repite nada.** |
| `waiting_approval` / `waiting_technician` / `waiting_task` / `blocked` | Intacto: esperar a una persona **no** es una interrupción. |
| `plans` en `running` / `waiting_human` | Igual que antes: el reconciliador idempotente se vuelve a ejecutar (con el *lease*, ver abajo). |
| Reiniciar en bucle | Idempotente: la 2.ª, 3.ª… vez no cambian filas, eventos ni auditoría. |
| El cliente reenvía el mismo mensaje | `client_msg_id` ya existente → **la misma ejecución** (`deduplicated: true`), cero solicitudes nuevas. |

### Una instancia ACTIVA por base: lease, espera (*standby*) y relevo
`instance_lease` (1 fila, sin dependencias) + `LeaseKeeper` (`lease.py`). Evita el error inverso —que otro proceso que solo abre la misma base declare muertas las ejecuciones **vivas** del primero, o corra un segundo reconciliador de planes a su lado— y, tras la revisión, también que ese proceso **ejecute trabajo sin supervisión**:
- **ACTIVA** = tiene el lease: lo renueva cada `AICOS_LEASE_HEARTBEAT` s (10 por defecto, TTL 30 s), ejecuta runs/planes y, **cada vez que lo gana** (arranque o relevo), recupera (`recover_runs` + `plans.recover`).
- **EN ESPERA** = no lo tiene: **solo lectura**. `gate.py` rechaza con `503` + `Retry-After` toda escritura de `/api/` (lanzar, continuar o cerrar trabajo) **antes de tocar estado**; siguen funcionando lecturas, inicio de sesión y la confirmación de visita del cliente (escritura simple, sin hilos). `GET /api/health` → `{"ok":true,"active":bool}`.
- **Relevo**: la instancia en espera reintenta tomar el lease en cada latido; si el dueño **murió** (pid inexistente en el mismo equipo) lo toma **al instante**, si está colgado (latido > 30 s) cuando vence. Cambio de dueño por *compare-and-set*. El mismo pid nunca es «otra instancia».
- **Pérdida del lease durante la ejecución**: sube la *época*; cada hilo de run/plan verifica antes de **cada paso** (`pace/emit/call/save`, `_call/_ev` del planificador) que sigue en su época **y** que la fila del run sigue `running` (el veredicto de quien lo declaró interrumpido es definitivo) y, si no, se detiene (`LeaseLost`) sin tocar estado. Los cambios de estado del run llevan además un *compare-and-set* (`AND status='running'`). Una instancia que recupera el lease vuelve a recuperar (sus propios hilos de la época anterior ya están detenidos).
- **Respaldo (`_launch`)**: si el lease se pierde justo entre la comprobación HTTP y el lanzamiento, la ejecución se declara interrumpida (nunca queda `running` sin hilo); un plan no se lanza y lo retoma quien tenga el lease.
- Apagado limpio (lifespan) libera el lease.

### Interrupción atómica
`Orchestrator._fail_run` es el **único** lugar donde un run `running` pasa a `failed`: estado (CAS) + auditoría + evento `run.failed` (`events.emit_in`, misma transacción) + solicitud `ejecucion_fallida` + su auditoría, **en una sola transacción**. Un proceso que muere en cualquier punto deja **nada o todo**; el CAS lo hace idempotente y seguro entre instancias. (Antes: cuatro commits separados; un SIGKILL entre ellos dejaba `failed` sin evento o con la solicitud «sana», y ningún arranque lo reparaba, porque solo se buscan runs `running`.) Mismo camino para fallos por excepción.

## 3. Por qué NO se reanuda ni se reintenta automáticamente

Una fase del motor es una cadena de efectos **fuera de una única transacción** (eventos, tareas delegadas, estados de solicitud). Muchos efectos de dominio son idempotentes por clave única (`requests`, `quotes`, `approvals`, `visits`, `tasks`/`delegations`/`materials` con `idem_key`), pero **eso no está demostrado fase por fase** y los eventos se numeran con `MAX(seq)+1` (reanudar repetiría eventos visibles). Reanudar sin esa prueba es exactamente el riesgo «un retry duplica un movimiento».

**Condición para habilitar una reanudación manual (siguiente PR, no incluido):** prueba de *inyección de fallos*: para cada escenario A–D, interrumpir tras la N-ésima llamada de herramienta, reanudar y comprobar que el estado final de la BD es **idéntico** al de una ejecución sin interrupción (mismos conteos y claves). Hasta entonces, la acción segura es «requiere revisión humana».

## 4. Herramientas existentes evaluadas (PyPI/GitHub, 2026-11-22; propiedades según su documentación, no ejecutadas aquí)

| Herramienta | Verificado | Encaje |
|---|---|---|
| `huey` 3.5.4 (MIT, 2026-11-01) con backend SQLite | activo | Cola de tareas independientes con reintento *al menos una vez*: **no resuelve** que cada efecto sea idempotente (el mismo prerrequisito) y añade un segundo almacén/planificador |
| `APScheduler` 3.11.3 (MIT, 2026-06-28), jobstore SQLAlchemy | activo | Planificador de disparos; no recupera ejecuciones con espera humana ni reconcilia efectos |
| `procrastinate` 3.12.0 (MIT, 2026-11-19) | activo | Cola sobre **PostgreSQL** (`SKIP LOCKED`): candidata **si** se migra a PostgreSQL (V0.7) para *outbox* de acciones externas |
| Colas con broker (Celery, RQ, Dramatiq, arq) | no evaluadas hoy | Exigen Redis/RabbitMQ: costo operativo injustificado para un único proceso |

**Decisión:** el problema de V0.6 no es *despachar* trabajo sino *no repetir efectos* tras una muerte. Una cola no lo resuelve; el *lease* + estado explícito sí, con ~100 líneas y 0 dependencias. Se reevalúa `procrastinate`/outbox en M3 (primer canal real: webhooks y envíos con reintento sí exigen cola e idempotencia extremo a extremo).

## 5. Lo que NO cubre
1. **No hay reanudación**: una ejecución interrumpida queda `failed` y un administrador decide. Ver condición en §3.
2. El lease protege contra **varios procesos en el mismo equipo / sistema de archivos compartido**; no es elección de líder multi-servidor (V0.7 con PostgreSQL).
3. Un proceso **colgado** > 30 s pierde el lease y sus ejecuciones se declaran interrumpidas aunque su hilo siga vivo; el hilo se detiene en su próximo paso (fencing), pero un efecto **en curso en ese instante** (una herramienta ya dentro de su transacción) puede completarse una vez. Los efectos de dominio son idempotentes por clave única; no se ha demostrado fase por fase.
4. Una instancia en espera **no atiende escrituras**: con dos servidores abiertos a propósito sobre la misma base, uno solo trabaja. Por diseño; no hay balanceo.
5. Los planes lanzados justo en la carrera de pérdida del lease se retoman al ganar el lease, no antes (no hay barrido periódico).
6. Fallos por excepción (`error`) y por interrupción comparten `_fail_run`; las ejecuciones fallidas **antiguas** (de versiones previas, ya en una base) no se reparan retroactivamente.

## 6. Correcciones de la revisión del PR #12 (reproducidas en rojo antes de corregir)
| Hallazgo | Reproducción (código anterior) | Corrección | Prueba |
|---|---|---|---|
| **P1** instancia sin lease aceptaba y ejecutaba trabajo | tests nuevos fallaban: sin `/api/health`, sin 503, sin `keeper` | Gate de espera (503 antes de tocar estado) + relevo + fencing por época y por fila del run + CAS de estado + respaldo en `_launch` | `test_a_standby_instance_refuses_every_write…` (2 procesos reales), `test_failover_promotes_the_standby…` (A muere → B promovido → recupera 1 vez → B ejecuta R2 → un 3.er proceso arranca en espera sin tocar R2 ni duplicar solicitudes/aprobaciones), `test_losing_the_lease_stops…`, `test_a_run_declared_failed_by_someone_else_stops…`, `test_gate_and_backstop…` |
| **P2** estado, evento y solicitud en commits separados | SIGKILL antes del `INSERT INTO events` → `('failed', 0 eventos, solicitud 'nueva', 1 auditoría)`; antes del `UPDATE requests` → `('failed', 1 evento, solicitud 'nueva')` | `_fail_run` atómico (una transacción) | `test_interruption_is_all_or_nothing_even_if_the_process_dies_midway[events/requests/commit]`: **SIGKILL real** inyectado dentro de la recuperación (hook SQL, independiente de la implementación) → invariante «intacto o completo»; un reinicio normal lo completa **una** vez y otro no cambia nada |

Verificado por mutación: sin el chequeo de época falla `test_losing_the_lease…` (21 eventos > 8); sin el chequeo de fila ni el CAS falla `test_a_run_declared_failed…`.

## 7. Integración con #10 (acceso/CSRF) y #11 (copias)
Cada PR tenía su CI verde por separado. Al combinarlos (`main` `6becc00` + esta rama) aparecieron dos cosas que solo existen juntas; ambas están fijadas por `backend/tests/test_v06_integration.py`.

**Precedencia de middlewares: `CsrfMiddleware` (externo) → `ActiveGate` → `PublicRateLimit` (#13, solo `/api/public/*`) → rutas.** Starlette envuelve en orden inverso al de registro, por eso `ActiveGate` se registra primero (`api.py`). Una petición inválida recibe **el mismo 403 en la instancia activa y en la standby**: nunca llega a la comprobación de rol, de modo que su respuesta no revela quién tiene el lease y jamás se le envía un `503 + Retry-After` que invitaría a reintentar algo que ningún reintento puede volver válido. Solo una petición que ya pasó CSRF recibe 503 en una standby, siempre antes de que el código de la ruta toque estado.

| Petición de escritura (`POST /api/plans`) | Activa | Standby |
|---|---|---|
| sesión + token válidos | 200 (muta) | **503** + `Retry-After`, sin mutar |
| sin token · token erróneo · token vacío | 403 | **403** (no 503) |
| `Origin` ajeno o `Sec-Fetch-Site: cross-site` (con token válido) | 403 | **403** (no 503) |
| sin cookie (anónimo) | 401 | 503 (no hay nada que falsificar: CSRF lo deja pasar; la standby rechaza antes de autenticar. El rol ya es público en `/api/health`) |
| canal público `/api/public/*` (cliente; sin cookie ni token por diseño) | 200, o **429** si excede su presupuesto (`LIMITES_CANAL_PUBLICO.md`) | 503, nunca 403 ni 429: la standby rechaza antes de gastar presupuesto |

En standby siguen disponibles: lecturas con sesión, `GET /api/health`, inicio y cierre de sesión (una sesión es identidad, no trabajo: no crea runs, planes, solicitudes ni aprobaciones; el *login CSRF* se rechaza igual con 403) y la confirmación de visita del cliente. La sesión y el token CSRF viven en la base: una sesión creada en la standby, o emitida por la instancia que luego muere por SIGKILL, vale con el **mismo token** en la instancia promovida, que sigue exigiéndolo.

**El lease no viaja en las copias (#11 × #12).** `instance_lease` es estado de ejecución («quién corre esta base ahora»), no dato. Una copia tomada de un servidor vivo trae un dueño vivo con latido fresco; la app restaurada arrancaba en espera detrás de un servidor que ni siquiera usa ese archivo, y el `drill` no lo notaba (solo leía filas). Ahora `restore` **siempre** vacía `instance_lease` (también con `--keep-sessions`; el archivo de la copia sigue siendo exacto), `boot_check` exige que la copia restaurada arranque **activa**, informa `active`/`sessions` y detiene su `LeaseKeeper` (antes dejaba un hilo latiendo sobre una base ya borrada).

## 8. Evidencia
**Árbol combinado (`main` `6becc00` + esta rama), local, 22-nov-2026:** `pytest backend/tests` → **244 passed** en 103 s (224 de las tres ramas + 20 de `test_v06_integration.py`); build del frontend OK; Playwright regresión **12/12** (82 s); demo comercial **4/4** (11 s). `test_v06_recovery.py` (14, multiproceso con SIGKILL real, promoción, pérdida de lease y recuperación atómica) y `test_v06_qa_scripts.py` (`qa/evidence.py` y `qa/llm_audit.py` como subprocesos con CSRF) pasan sobre el árbol combinado sin cambios.

`python -m app.backup drill` por CLI sobre una base **temporal** abierta por un servidor real (lease vigente, escrituras con CSRF y un chat público en curso): `PASS … {'users': 6, 'companies': 2, 'sessions': 0, 'active': True}`, el lease del origen quedó intacto y el servidor siguió activo.

**Verificado por mutación:** invertir el orden de los middlewares hace fallar 5 casos de la matriz 403/503 y la prueba de dos procesos; sin el chequeo de época falla `test_losing_the_lease…`; sin el chequeo de fila ni el CAS falla `test_a_run_declared_failed…`. Variables: `AICOS_LEASE_HEARTBEAT` (s).
