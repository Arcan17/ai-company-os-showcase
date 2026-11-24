# V0.7 · P0 · Auditoría de identidad, permisos y aislamiento multiempresa

**Fecha:** 2026-11-23 · **Código auditado:** `main` `97b8194` (rama `feat/v07-auth-pilot-architecture`) · **Issue:** #17 · **Método:** lectura del código + análisis estático de solo lectura (`qa/audit_identity_tenancy.py`). **No** se arrancó ningún servidor, **no** se abrió `aicos.db` ni `demo_comercial.sqlite3`, no se usó red salvo para consultar documentación pública.
Este documento separa **hechos verificados en `main`** (con archivo y línea) de **capacidades que todavía NO existen**. Nada aquí es una certificación ni una afirmación de cumplimiento legal.

> **Estado de CI de `main` `97b8194`:** run [45209323352](https://github.com/Arcan17/ai-company-os/actions/runs/45209323352) **success** — pytest 374 passed + 1 omitida, Playwright 12 + 4 (verificado en GitHub el 2026-11-23).

## 1. Identidad hoy (hechos verificados)

| Tema | Qué hay en `main` | Dónde |
|---|---|---|
| Credencial | contraseña con **argon2id** (m = 19 MiB, t = 2, p = 1), re-hash transparente de los hashes scrypt antiguos | `security.py:23-51` |
| Cuenta | tabla `users`: **`email` único en toda la base** (una cuenta pertenece a **una** empresa), `role` ∈ {owner, admin, technician}, `active`, `is_demo` | `db.py:47-49` |
| Alta de empresa | solo local: CLI `python -m app.company_admin create` o `POST /api/setup/companies` (404 salvo `AICOS_ALLOW_COMPANY_CREATION=1` **y** loopback sin cabeceras de reenvío) | `company_admin.py`, `api.py:700-705` |
| Alta de empleado | el **dueño** lo crea y **fija o recibe la contraseña** (si no la fija, se genera y se le muestra una vez) | `company_admin.py:126-142` |
| Sesión | cookie opaca `aicos_session` (HttpOnly, SameSite=Lax, `Secure` si https); en la base solo el **SHA-256** del token; 12 h absolutas + 120 min de inactividad; el nuevo login reemplaza la sesión presentada | `security.py:54-118`, `api.py:136-138` |
| CSRF | token sincronizado por sesión + verificación de origen (`Sec-Fetch-Site`/`Origin`) en todo `POST/PUT/PATCH/DELETE` de `/api/` salvo `/api/public/*` | `csrf.py` |
| Intentos de login | 3 contadores persistentes (par correo+IP: 5, correo: 20, IP: 50 por 15 min); mismo mensaje y mismo trabajo de hash para cuenta conocida y desconocida | `security.py:126-147` |
| Revocación | `logout`, `logout-all`, y **desactivar un empleado borra sus sesiones** al instante; un cambio de rol se aplica en la siguiente petición (el rol se lee de `users` cada vez) | `api.py:230-247`, `company_admin.py:201` |
| Auditoría | `auth.login` ok/denied, `auth.locked`, `auth.logout*`, cambios de configuración y denegaciones de permiso, con la IP **en claro** | `api.py:213-245`, `events.py:audit` |
| Canal público | `/api/public/*` sin cookie, con límites persistentes por empresa y por fuente (#13) | `ratelimit.py` |
| Operación | un proceso `uvicorn` (`--no-proxy-headers`), SQLite en WAL, **una instancia activa por base** (lease) | `lease.py`, `gate.py` |

### Lo que NO existe (no se esconde)
Invitaciones · recuperación y **cambio** de contraseña (no hay ningún endpoint ni función: `grep` de `reset|change.*password` sobre `backend/app` no encuentra nada) · verificación de correo · MFA · envío de correo (las integraciones son `simulado`) · TLS en este repositorio · cabeceras de seguridad (HSTS, CSP, `X-Content-Type-Options`, `Referrer-Policy`) · prefijo `__Host-` de la cookie · una cuenta con más de una empresa · lista de sesiones activas por usuario · exportación y borrado de los datos de una persona.

## 2. Permisos

* **Modelo:** tres roles con conjuntos de permisos fijos en código (`permissions.py`); cada asistente personal tiene los suyos y los **efectivos son la intersección** con los del humano al que sirve; `approvals.decide` no lo tiene ningún agente. Las denegaciones quedan en `audit_log`.
* **Aplicación:** de **58 rutas**, **45** exigen sesión y **13** son anónimas (ver §3, H1). De las 45, **31** comprueban un permiso con `need(user, …)`, **5** un rol en el cuerpo de la función y **9** dependen solo de la sesión: `logout-all`, `me`, `dashboard`, `integrations`, `attention`, `assistant/chat`, `assistant/history`, `tools/{name}/invoke` (el registro de herramientas comprueba el permiso) y `tasks/{tid}/complete` (comprueba asignación y empresa dentro del motor).
* **Observación:** hay **tres mecanismos** de autorización (`need()`, comparación de rol en línea, registro de herramientas). Funciona, pero una sola capa sería más fácil de auditar y de cubrir con una prueba generada a partir de la tabla de rutas (V0.7 P1).

## 3. Hallazgos

| # | Hallazgo | Gravedad para el piloto | Evidencia | Qué lo cierra |
|---|---|---|---|---|
| **H1** | **`GET /api/auth/demo-accounts` es anónima, no depende del modo demo y devuelve la contraseña de demostración** (`demo1234`); la pantalla de entrada la muestra | **Alta** en cualquier entorno que no sea local | `api.py:190-195`, `seed.py:12`, `frontend/src/pages/Login.tsx:8` | modo `pilot` que la apaga (P1) |
| **H2** | **`seed()` crea las tres empresas de demostración con esa contraseña en toda base vacía** (la condición es «no hay ninguna empresa»): un staging recién arrancado nace con tres empresas accesibles a cualquiera que conozca el endpoint anterior | **Alta** | `seed.py:53-66`, `api.py:64` (se llama en cada `create_app`) | en `pilot`: base vacía sin demostración; el alta es un acto del operador (P1) |
| H3 | `POST /api/demo/reset-scenarios` existe fuera del modo demo (solo dueño, exige escribir `REINICIAR`, respalda la base; 400 para empresas no sembradas) | Media: superficie inútil en producción | `api.py:577-592` | apagarla en `pilot` (P1) |
| H4 | **El dueño conoce o fija las contraseñas de sus empleados** | Media: contradice «identidad real» y la responsabilidad individual | `company_admin.py:126-142` | credenciales delegadas a un proveedor de identidad (P2) |
| H5 | **Sin recuperación ni cambio de contraseña, sin verificación de correo, sin MFA** | **Alta** para un piloto real | §1 «No existe» | proveedor de identidad (P2–P3) |
| H6 | Sin cabeceras de seguridad ni `__Host-` | Media | `docs/v06/ACCESO_Y_SESIONES.md` §5.1, §5.6 | modo `pilot` (P1) |
| H7 | IP **en claro** en `login_attempts` y en el detalle de `auth.login` | Media (minimización; Ley 21.719) | `security.py:146`, `api.py:213-220` | truncar/derivar con HMAC (backlog) |
| H8 | Alta de empresa solo local | Informativo: es **correcto** para un piloto privado, pero exige un procedimiento de aprovisionamiento del operador en el servidor | `api.py:700-705` | procedimiento documentado (P5) |
| H9 | Aislamiento **solo en la capa de consultas**: SQLite no tiene seguridad por filas, y las pruebas de cruce entre empresas son puntuales | Media | §5 | prueba generada de cruce + guardia estática (P1) |
| H10 | **«PostgreSQL cambiando solo `AICOS_DB_URL`» (docstring de `db.py`) no es cierto hoy** | Informativo | §6 | decisión de aplazar PostgreSQL con disparadores (documento de arquitectura) |

## 4. Qué cubren ya las pruebas (verificado)

374 pytest + 1 omitida y Playwright 12 + 4 en CI. Lo relevante para identidad y aislamiento:
`test_v06_access.py` (login, CSRF, bloqueo, sesiones, argon2id, loopback), `test_v06_ratelimit.py` (canal público), `test_v06_backup_*.py`, `test_security.py::test_tenant_isolation_between_companies`, `test_plans.py::test_tenant_and_employee_isolation`, `test_scenario_d.py::test_isolation_and_personal_scoping_for_v03`, `test_v05_config.py` (bloque «isolation between configured companies»), y las tres empresas de demostración (Clima Sur, Motor Norte, Aire Austral) en la suite A–E y en los E2E.

## 5. Aislamiento multiempresa: la medición

> **Actualización V0.7 P1:** tras añadir `pilot.py` la guardia cubre **39 módulos / 387 consultas / 33 sin `company_id` justificadas** (la consulta nueva es la comprobación de arranque del modo piloto, solo lectura). La matriz **dinámica** de cruce entre empresas y su hallazgo O2 están en `P1_MODO_PILOTO.md` §4–§5.

`qa/audit_identity_tenancy.py` lee el esquema real (`db.md`) y **todas** las consultas de **todos** los módulos de `backend/app/`, recursivamente (literales **y** f-strings):

* **27 tablas** llevan `company_id` (el script lo deduce del esquema, no de una lista escrita a mano).
* **38 módulos** leídos: los 25 del primer nivel y los de `engine/`, `tools/` y `providers/`. `providers/` no contiene SQL.
* **386 consultas** tocan una de esas tablas: **354 filtran por `company_id`** y **32 no**. Las 32 están justificadas una a una (28 entradas distintas) en `ALLOWED_GLOBAL`.

| Zona | Consultas | Sin `company_id` |
|---|---:|---:|
| Primer nivel (`api.py`, `security.py`, `db.py`, …) | 183 | 22 |
| `engine/` (orquestador y planificador) | 69 | 8 |
| `tools/` (herramientas del registro) | 134 | 2 |
| **Total** | **386** | **32** |

> **Corrección (revisión de revisión automatizada sobre el PR #18, `c0eb971`).** La primera versión de esta sección decía «183 consultas, 22 sin filtrar»: leía solo `backend/app/*.py` y **omitía el motor de ejecución y las herramientas** (203 consultas y 10 sin `company_id`). Un `AND company_id=:c` borrado de `engine/planner.py` no se habría detectado. Corregido: el auditor recorre `backend/app/**/*.py`, y la prueba lo comprueba contra un recorrido independiente del disco.

**Cómo se justifica una consulta global.** La clave es **(ruta del módulo relativa a `backend/app/`, SQL completo normalizado)** con coincidencia **exacta**, no por prefijo ni por nombre de archivo: una variante de la consulta (otra cláusula, un `OR`) o un módulo homónimo en otra carpeta es otra consulta y exige su propia justificación. Si una entrada cubre varias consultas idénticas del mismo módulo, declara cuántas (`ALLOWED_COUNT`); una copia de más se marca. Las 10 consultas nuevas del motor se revisaron **una a una contra sus llamadores** (no se autorizaron por su forma):

| Consulta (módulo) | Por qué es global | Origen del id (verificado) |
|---|---|---|
| `SELECT * FROM runs WHERE id=:i` ×2 (`engine/orchestrator.py`: `_execute`, `_fail_run`) | el ciclo de vida de un run carga la fila y **deriva** `company_id` de ella | `run_id` lo genera el servidor (`uid("run_")`) o sale de filas ya filtradas por empresa (`decide`, `resolve`, `continue_collab`, `complete_task`); el `plan_id` del planificador, de `confirm`, `_auto_read_only` y `on_task_event`, igual |
| `SELECT status FROM runs WHERE id=:i` (`Flow.alive`) | cerco antes de cada paso: solo lee `status` | el PROPIO run (`self.rid`) |
| `UPDATE runs SET {…} WHERE id=:_i{…}` (`_set`) | transición del PROPIO run con compare-and-set | `self.rid`; las columnas son literales internos |
| `UPDATE runs SET status='failed' … AND status='running'` (`_fail_run`) | compare-and-set sobre el run leído en la misma transacción | idem |
| `SELECT id,phase FROM runs WHERE status='running'` (`recover_runs`) | recuperación **global a propósito** al ganar el lease (`api.py:75`) | recorre todo lo que quedó en curso; cada run se cierra con `_fail_run` |
| `SELECT id FROM plans WHERE status IN (…)` (`engine/planner.py`, `recover`) | ídem para planes (`api.py:76`); `advance` deriva la empresa de la fila y vuelve a filtrar | recorre todo lo que quedó en curso |
| `SELECT status FROM plan_steps WHERE plan_id=:p AND seq=:s` ×2 (`tools/plans.py`) | el paso de dependencia del MISMO plan | `p['id']` sale de `_plan(ctx, …)`, que filtra por id **y** `company_id=ctx.company_id` |
| `SELECT token FROM conversations WHERE id=:i` (`engine/orchestrator.py:104`, `start_chat`) | lectura del token tras un reintento idempotente | id leído del `state` de un run ya filtrado por `company_id`+`idem_key` (:101-103); ver **O1** |

* Una consulta nueva sin `company_id`, **o una copia de más de una justificada**, **hace fallar el CI** (`backend/tests/test_v07_audit_tool.py`): hay que justificarla por escrito. Lo mismo para una ruta anónima nueva. La prueba incluye mutaciones sobre una **copia** del código real (quitar `company_id` de `planner.py`, de `orchestrator.py` y de `tools/plans.py`), y el auditor mismo se sometió a 4 mutaciones (no recursivo, clave solo por nombre de archivo, sin límite de copias, coincidencia por prefijo) que sus pruebas detectan.
* **Rutas:** todas las rutas HTTP están declaradas en `api.py` (no hay `APIRouter` ni rutas en otros módulos; comprobado con `grep`), así que el inventario de rutas no tiene el mismo hueco.
* **Límite honesto 1:** es análisis estático. Una consulta que filtra por `company_id` puede seguir siendo incorrecta (un `JOIN` que olvida el filtro de la segunda tabla, un id de otra empresa aceptado por el motor). Lo que lo cubre de verdad son las **pruebas de cruce**, hoy puntuales: falta la prueba **generada desde la tabla de rutas** (cada ruta con identificador, llamada como la empresa B con ids de la A) y los flujos SSE. Es la primera pieza del incremento P1.
* **Límite honesto 2 (medido):** el heurístico da por «filtrada» cualquier consulta que contenga `company_id` **en cualquier parte**. Hay **4** `SELECT` donde aparece solo como columna o en un `JOIN`, no como predicado del `WHERE`: `api.py:177` y `api.py:193` (herramientas de demostración, H1), `api.py:522` (canal público: el alcance lo da `co.slug` en el `JOIN`) y `engine/planner.py:508` (`SELECT company_id,status FROM plans WHERE id=:i`: la derivación de empresa de `advance`, global por id interno como las de arriba). Se leyeron a mano y no son un cruce; exigir el predicado en el `WHERE` obligaría a justificarlas también y es candidato para P1.
* **O1 (observación, no hallazgo confirmado).** `start_chat` devuelve el `conversation_token` a quien repita el `client_msg_id` de un mensaje ya recibido en la misma empresa. La consulta del token (`orchestrator.py:104`) no lleva `company_id`, aunque el id viene de un run que sí se filtró; añadirlo es gratis y es defensa en profundidad. Además, el `client_msg_id` del widget del cliente (`frontend/src/pages/CustomerChat.tsx:26`) sale de `Math.random()` (8 caracteres base36, ≈41 bits, no criptográfico), y el servidor solo exige 6–80 caracteres alfanuméricos. **No intenté explotarlo** y los límites de `/api/public/*` (#13) acotan la adivinación; queda como candidato de P1 (token de reintento con ≥128 bits de un generador criptográfico, o ligar el reintento a un secreto de la conversación).

## 6. PostgreSQL: lo que impediría «cambiar solo la URL»

Puntos específicos de SQLite hoy en el código: índices parciales declarados con `sqlite_where` (`db.py:68-69`; en PostgreSQL haría falta `postgresql_where`); `PRAGMA journal_mode/foreign_keys/busy_timeout` (`db.py:161-163`, protegidos por dialecto o no, **sin probar**); `INSERT OR IGNORE` (`ratelimit.py:202`); `backup.py` entero usa la API `sqlite3` (copia en línea, restauración, `boot_check`); `demo_preflight.py` y `demo_seed.py` abren `sqlite3` directamente; la semántica de `rowcount` del *lease* y del limitador está probada solo sobre SQLite. **Conclusión:** pasar a PostgreSQL es un proyecto (reescribir copias/restauración y probar todo contra el motor nuevo), no un cambio de configuración. Ver la decisión de aplazarlo, con disparadores, en `P0_ARQUITECTURA_Y_PLAN.md`.

## 7. OWASP ASVS 5.0 como guía por tema (no es una certificación)

[ASVS 5.0.0](https://github.com/OWASP/ASVS) (publicada el 2025-05-30; el repositorio marca la última versión estable). Capítulos relevantes confirmados: **V6 Autenticación** (incluye contraseñas, ciclo de vida y recuperación de factores, MFA), **V7 Gestión de sesiones**, **V8 Autorización**, **V9 Tokens autocontenidos** y **V10 OAuth y OIDC**. Estado **por tema** (no por número de requisito: no se verificaron uno a uno):

| Tema | Estado en `main` |
|---|---|
| Almacenamiento de contraseñas (V6) | **Cubierto**: argon2id con los mínimos de OWASP |
| Límite de intentos y enumeración (V6) | **Cubierto** (contadores persistentes; mismo mensaje y mismo trabajo) |
| Recuperación y cambio de credenciales (V6) | **No existe** (H5) |
| MFA (V6) | **No existe** (H5) |
| Sesiones opacas, expiración, inactividad, cierre global, revocación (V7) | **Cubierto**; falta lista de sesiones activas y `__Host-` (H6) |
| Autorización por rol y por empresa (V8) | **Parcial**: aplicada y probada, pero repartida en 3 mecanismos y con pruebas de cruce puntuales (H9) |
| OAuth/OIDC (V10), tokens (V9) | **No aplica todavía**: se hará con un proveedor (P2); la validación de `iss/aud/exp/nonce/state/PKCE` será parte de las pruebas |
| TLS, cabeceras y configuración de despliegue | **No existe** en el repositorio (H6) |

## 8. Cómo reproducir

```bash
backend/.venv/bin/python qa/audit_identity_tenancy.py            # informe legible
backend/.venv/bin/python qa/audit_identity_tenancy.py --json     # el mismo, en JSON
backend/.venv/bin/python qa/audit_identity_tenancy.py --check    # sale con 1 si hay una ruta anónima o una consulta sin justificar
backend/.venv/bin/python -m pytest -q backend/tests/test_v07_audit_tool.py
```
