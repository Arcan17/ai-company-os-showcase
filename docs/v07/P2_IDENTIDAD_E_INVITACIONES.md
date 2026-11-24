# V0.7 · P2 · Identidad con proveedor simulado, invitaciones y autenticación reciente

**Fecha:** 2026-11-23 · **Issue:** [#23](https://github.com/Arcan17/ai-company-os/issues/23) · **Base:** `main` `9f0ffb9` (CI [45291188346](https://github.com/Arcan17/ai-company-os/actions/runs/45291188346) SUCCESS) · **Diseño:** [`P0_ARQUITECTURA_Y_PLAN.md`](P0_ARQUITECTURA_Y_PLAN.md) §3 y §6 · **Anterior:** [`P1_MODO_PILOTO.md`](P1_MODO_PILOTO.md)

> **Qué es y qué NO es.** P2 construye y **prueba** el flujo de identidad (invitar → aceptar → iniciar sesión → reautenticar → revocar) contra un proveedor **simulado e inyectado**, sin contratar nada, sin red y sin tocar bases ni puertos del Mac.
> **No hay proveedor real, no hay MFA real y no se envía ningún correo real.** El simulado informa lo que la prueba le dice: que `auth_time` o `email_verified` salgan de él **no demuestra** nada sobre WorkOS ni sobre MFA. **El piloto sigue sin ser apto para personas reales** (§9).

## 1. Qué cambia (en una página)

| Pieza | Qué es |
|---|---|
| `app/identity.py` | La **interfaz estrecha** del proveedor: `authorize_url(state, nonce, challenge, login_hint, force_reauth)`, `exchange(code, verifier, nonce)`, `invite(email)`, `disable(subject)`, `logout_url()`; el valor `Identity` (inmutable) y los errores. El proveedor solo responde «quién es»: **rol y empresa salen siempre de nuestra base**. |
| `app/idp_fake.py` | El proveedor **simulado**, **estricto** a propósito (código de un solo uso y 60 s —un intento fallido también lo quema—, PKCE S256, nonce, caída, desactivación). Sin sockets, sin rutas HTTP, direcciones `.invalid`. |
| `app/invitations.py` | Crear / listar / revocar invitaciones (dueño, `config.manage`). |
| `app/identity_flow.py` | Inicio del flujo (state + PKCE + nonce + cookie de correlación) y *callback*; aceptación atómica; inicio posterior por `subject`; reautenticación por proveedor. |
| `app/clock.py` | Única fuente de la hora de pared (`clock.utcnow()`): las pruebas la sustituyen por un **reloj controlado**. Comportamiento idéntico fuera de las pruebas. |
| Base de datos (aditiva) | `identities`, `invitations`, `auth_flows` y `sessions.auth_time`. Ninguna fila existente se toca; una base V0.6/P1 migra sola y conserva sus sesiones. |
| `api.py` | `create_app(db_url, *, identity_provider=None)`; rutas de identidad **solo si hay proveedor**; `auth_time` y autenticación reciente en `need()`; `POST /api/auth/reauth`. |
| SPA | `ReauthDialog`: ante `401 reauth_required` pide la contraseña **una** vez y reintenta la misma operación. (No hay pantallas de invitaciones: ver §9.) |

## 2. Modelo de datos

| Tabla | Columnas relevantes | Reglas |
|---|---|---|
| `identities` | `provider`, `subject` (PK compuesta), `user_id` (FK), `company_id` (FK), `created_at`, `last_login_at` | Una identidad pertenece a **una** persona de **una** empresa en toda la base. `UNIQUE(provider, user_id)`. El inicio de sesión busca por `(provider, subject)`, **nunca por correo**. |
| `invitations` | `id`, `company_id`, `email_key`, `name`, `role`, `token_hash` (único), `status`, `created_by`, `created_at`, `expires_at`, `accepted_at`, `accepted_user_id`, `revoked_at` | El **secreto solo existe como hash SHA-256**. Índice parcial único `(company_id, email_key) WHERE status='pending'`: una vigente por correo y empresa, también ante carreras. |
| `auth_flows` | `state_hash` (PK), `cookie_hash`, `nonce`, `verifier`, `purpose` (`login`/`invite`/`reauth`), `invitation_id`, `user_id`, `session_hash`, `created_at`, `expires_at`, `used_at` | **Sin `company_id` a propósito** (como `login_attempts`: un acceso normal aún no sabe su empresa). `state` y cookie solo como hash; el verificador PKCE va en claro porque hay que presentarlo al proveedor (vida ≤ 10 min, se borra al caducar). Estado de ejecución: **una restauración lo vacía**. |
| `sessions.auth_time` | ISO UTC, `NULL` permitido | `NULL` = «sin dato» = **nunca reciente**. Ni el *touch* deslizante ni un panel abierto lo renuevan. |

## 3. Las rutas (y cuándo existen)

| Ruta | Existe | Qué hace |
|---|---|---|
| `POST /api/auth/reauth` | **siempre** | Confirma la contraseña **de la persona de la sesión** y renueva `auth_time` de **esa** sesión. Comparte el límite de intentos del inicio de sesión. Con un proveedor real en el piloto: 403. |
| `POST /api/auth/idp/start` | solo con proveedor | Crea el flujo. Único dato aceptado: `invite_token` o `reauth: true` (`extra="forbid"`: rol, empresa, correo o `auth_time` del navegador → 422). Fija la cookie de correlación. |
| `GET /api/auth/callback` | solo con proveedor | Recibe `code` y `state`; **todo** fallo = la misma redirección `303 /#/login?error=idp`; éxito `303 /#/dashboard`. Destinos fijos (sin redirección abierta). |
| `POST /api/config/invitations` | solo con proveedor | Crea la invitación (`config.manage` + autenticación reciente + CSRF + 20/hora por empresa). Devuelve el secreto **una sola vez**. |
| `GET /api/config/invitations` | solo con proveedor | Lista de la empresa (sin secretos ni hash). |
| `POST /api/config/invitations/{iid}/revoke` | solo con proveedor | Revoca; idempotente; otra empresa o inexistente: **404 idéntico**. |

**Sin proveedor inyectado no se registra ninguna de las cinco** (404 real) y la aplicación se comporta como en P1 (más `reauth`).

## 4. Reglas de seguridad y su prueba

| # | Regla | Prueba (en `backend/tests/`) |
|---|---|---|
| R1 | El secreto de la invitación solo se guarda como hash y se devuelve una vez | `test_the_secret_is_stored_only_as_a_hash_and_returned_exactly_once` (busca el secreto en **todas** las tablas) |
| R2 | Correo + rol + empresa los fija el dueño; el cuerpo no puede traer empresa, estado ni caducidad; `owner` no se invita | `test_invalid_or_forged_invitation_bodies_…` (8 casos) |
| R3 | 7 días exactos, un solo uso, revocable, una vigente por correo y empresa (también en la base) | `test_the_owner_creates_…_for_seven_days`, `test_only_one_pending_…`, `test_the_database_itself_refuses_…`, `test_a_callback_cannot_be_replayed_…`, `test_two_flows_started_with_the_same_invitation_…`, `test_the_last_second_of_the_seventh_day_…` |
| R4 | Solo `config.manage`; CSRF; límite por empresa; auditoría **sin** secreto ni correo | `test_only_config_manage_…`, `test_creating_an_invitation_needs_the_csrf_token`, `test_creating_invitations_is_rate_limited_…`, `test_creating_and_revoking_are_audited_…` |
| R5 | La respuesta no revela si el correo es de **otra** empresa, y no se le escribe | `test_the_answer_does_not_reveal_…`, `test_the_provider_is_asked_to_invite_only_…` |
| R6 | Correo verificado = el booleano `True` y **coincidencia exacta** (solo mayúsculas/minúsculas): ni `+etiqueta`, ni espacios, ni homoglifo cirílico | `test_only_a_real_boolean_true_…`, `test_the_provider_email_must_be_exactly_the_invited_one`, `test_email_case_does_not_matter` |
| R7 | Sin invitación ni vínculo no se crea nada; **nunca** se vincula por coincidencia de correo | `test_an_identity_with_no_invitation_…`, `test_an_identity_is_never_linked_to_an_existing_account_by_matching_email` |
| R8 | Una identidad ya vinculada no se re-vincula a otra persona | `test_an_identity_already_linked_to_one_user_…` |
| R9 | La cuenta creada **no tiene contraseña utilizable** | `test_the_account_created_by_an_invitation_has_no_usable_password` |
| R10 | Rol y empresa los decide la base; el navegador no aporta atributos | `test_the_start_request_accepts_no_identity_attributes_…`, `test_forged_callback_parameters_change_nothing`, `test_the_token_of_a_never_lands_a_person_in_b_…` |
| R11 | `state` + PKCE S256 + nonce + cookie de correlación de un solo uso; el flujo se quema **antes** de hablar con el proveedor; una cookie ajena falla **sin** anular el flujo de otra persona | `test_a_callback_from_a_browser_that_did_not_start_the_flow_…` (*login CSRF*), `test_every_malformed_or_tampered_callback_fails_closed` (7), `test_a_code_issued_for_another_flow_…`, `test_a_provider_that_is_down_…`, `test_an_expired_flow_is_refused`, `test_the_correlation_cookie_is_…`, `test_neither_the_state_nor_the_flow_cookie_…` |
| R12 | **Un solo rostro** ante cualquier fallo (código, `Location`, cuerpo, cookies) | `test_every_failure_looks_exactly_the_same_to_the_browser`, `test_a_revoked_or_expired_invitation_cannot_start_and_answers_like_a_made_up_token` |
| R13 | Una sesión nueva **reemplaza** a la que presentó el navegador | `test_completing_the_flow_replaces_the_session_the_browser_presented` |
| R14 | Desactivar: sesiones fuera (ya existía), baja pedida al proveedor y, aunque el proveedor siga autenticando, **nuestra base manda** | `test_a_deactivated_person_cannot_sign_in_…` |
| R15 | El rol vigente (no el de la invitación) es el del siguiente inicio de sesión | `test_a_role_change_by_the_owner_is_what_the_next_sign_in_gets` |
| R16 | Migración aditiva: una base anterior conserva datos y sesiones; `auth_time` ausente = no reciente | `test_a_pre_p2_database_migrates_additively_…` |

## 5. `auth_time` y autenticación reciente («step-up»)

* **Qué exige:** las **escrituras** (`POST`) cuyo permiso es `config.manage` o `approvals.decide` requieren `auth_time` de **≤ 15 min** (`AICOS_REAUTH_MAX_AGE_SECONDS`, por defecto 900; en el piloto solo 1–900, si no el arranque se niega). Si no: `401 reauth_required`, **antes** de tocar ningún dato (una auditoría `reauth_required` deja rastro). Quien no tiene el permiso recibe 403, no 401.
* **Dónde vive la regla:** dentro de `need()`. Toda ruta nueva que use esos permisos queda cubierta; `test_every_sensitive_post_route_is_gated_and_none_was_forgotten` lo comprueba **generando la lista desde el OpenAPI vivo** (una ruta sensible nueva sin caso hace fallar la prueba).
* **Frescura:** edad ≤ ventana y no adelantada más de 60 s; `NULL`, ilegible o sin zona horaria = **no reciente**. Reloj controlado: 14:59 y 15:00 pasan, 15:01 no.
* **Lecturas sin exigencia** (decisión explícita: la pantalla de Configuración debe poder abrirse; lo que se protege es el **cambio**). Operaciones **no** incluidas todavía: `plans.confirm`, `runs.resolve`, copias, exportar/borrar datos — decisión del dueño (§9).
* **Reautenticar:** con **contraseña** (`POST /api/auth/reauth`: la de la persona de la sesión, con límite de intentos compartido con el inicio de sesión) o con el **proveedor** (`reauth: true`): el flujo exige la **misma** identidad vinculada, la **misma** sesión viva y un `auth_time` **nuevo** (≥ inicio del flujo − 5 s) — un proveedor que reutiliza su sesión vieja no cuenta —, y **rota** la sesión.
* **Un inicio de sesión con SSO viejo sí entra, pero no es reciente:** `test_an_old_provider_authentication_signs_in_but_is_not_fresh`. Un `auth_time` del futuro (> 60 s) no inicia sesión; un proveedor sin `auth_time` inicia sesión y toda operación sensible pide reautenticar.
* **Aviso de alcance:** `auth_time` mide **cuándo** se autenticó la persona, **no** con qué factor. «MFA obligatorio en cada inicio de sesión del proveedor» sigue siendo una configuración del proveedor **no verificada** (P3/P4).

### Cuándo se exige, por entorno

| Entorno | Proveedor | `auth_time` se guarda | Se exige | Contraseña local | Rutas de identidad |
|---|---|---|---|---|---|
| sin `AICOS_ENV` / `local` | no | sí | **no** (compatibilidad absoluta de local/demo) | sí | no |
| local + proveedor inyectado (pruebas) | sí | sí | sí | sí | sí |
| `pilot` sin proveedor (el piloto de P1) | no | sí | **sí** (sesiones de contraseña: 15 min) | sí | no |
| `pilot` + proveedor **no simulado** | sí | sí | sí | **desactivada** (403, sin gastar ni registrar intentos) | sí |
| `pilot` + proveedor **simulado** | — | — | — | — | **el arranque se niega (código 78, antes de abrir la base)** |

## 6. Por qué el simulado no puede iniciar sesión en un piloto real

1. **No hay configuración que lo active.** `AICOS_IDP_PROVIDER` con **cualquier** valor (en `local` o `pilot`) detiene el arranque antes de abrir la base: en esta versión no hay proveedor configurable. El simulado solo se inyecta **en código**: `create_app(..., identity_provider=FakeProvider())`.
2. **El piloto lo rechaza:** `validate_startup` exige que el proveedor declare `is_fake = False` **explícitamente** (un objeto sin el atributo se trata como simulado). Mensaje sin valores; código 78.
3. **Ningún proceso normal lo importa** (`api.py` solo conoce la interfaz): prueba en un proceso real, con y sin `AICOS_ENV=pilot`.
4. **Sin proveedor, sin rutas**; con proveedor, la tabla de rutas crece **exactamente** en las cinco documentadas y ninguna contiene «fake», «simul», «mock» ni «_test».
5. **Ningún socket:** el flujo completo corre con `socket.connect` y `create_connection` prohibidos.

**Límite honesto:** un código que **inyecte** un objeto con `is_fake = False` sí lo pasaría. Esta guarda protege contra la **configuración**, no contra quien edite el código del despliegue; en P3 el proveedor real se construye dentro del propio arranque, no se recibe de fuera.

## 7. Aislamiento entre empresas

* La invitación fija la empresa; el token de A solo sirve para A, con el rol que fijó A (`…token_of_a_never_lands_a_person_in_b…`); B no ve, no revoca ni distingue una invitación de A de una inexistente (404 **idéntico**, A **sin cambios** por huella SHA-256 de todas sus tablas con `company_id`).
* El mismo correo invitado por dos empresas: gana el primero que acepta; el otro no puede reutilizar la identidad (R8) y su invitación sigue pendiente.
* Auditoría por empresa; fallos sin empresa conocida **no** se auditan en ninguna (no hay a quién atribuirlos).
* Guarda estática (`qa/audit_identity_tenancy.py --check`): las dos rutas anónimas nuevas y las 6 consultas globales a propósito están **justificadas una a una** (la credencial —secreto, `subject`, sesión— es la clave; la empresa se deriva de la fila). Una consulta nueva sin `company_id` o una ruta anónima nueva hace fallar `test_v07_audit_tool.py`.

## 8. Evidencia

| Qué | Resultado |
|---|---|
| Pruebas rojas primero (commit `16e2a6e`) | 157 pruebas nuevas: **155 fallan por la razón correcta** (`create_app()` sin `identity_provider`, sin `app.identity`/`app.idp_fake`/`app.clock`, sin `settings.idp_provider`/`reauth_max_age_seconds`) y **2 son guardas** que ya pasaban (un proceso normal no importa el simulado) |
| pytest | **652 passed** en el commit final `f19e803` (las 492 de `main` **sin modificar una línea** + **160 nuevas**; `CI=true AICOS_REQUIRE_RESTIC=1`, 259 s). Sobre `a06445c` (155 nuevas): 647 passed. Las nuevas son 157 del commit rojo, −3 valores parametrizados que eran subcadenas del mensaje que debían no contener, +1 guarda de la herramienta de mutaciones y +5 pruebas añadidas después |
| Build, Playwright regresión, demo, piloto | Sobre `a06445c` (el único cambio de código posterior es registrar la **clase** del error del proveedor): build OK · Playwright regresión **13/13** · demo **4/4** · piloto **2/2**. El E2E nuevo recorre en Chromium real: ventana de 3 s → cuadro de reautenticación → una contraseña equivocada **no** reautentica → la correcta reintenta la **misma** operación → cancelar no deja ningún efecto |
| Mutaciones P2 (`qa/mutations_v07_p2.py`, 45) | **45/45 detectadas** en `f19e803` (la copia sin mutar pasa). La primera pasada dio **43/45**: sobrevivieron P09 y P14 y **ambas eran debilidades de mis pruebas**: «el flujo se quema» se probaba reintentando **sin** la cookie que el servidor ya había borrado (fallaba por otra razón), y la negativa por identidad ya vinculada también la cubría la clave primaria, pero por una vía de error interno sin auditoría. Se reforzaron y ahora se detectan |
| Mutaciones P1 (29), guarda estática, matriz de aislamiento | las 29 siguen aplicándose a una sola vez (se actualizaron 3 textos: `M12`, `M14`, `M15`, que el propio cambio movió) · guarda `--check` limpia · matriz sin cambios salvo la justificación de `POST /api/auth/reauth` |
| Bases y puertos del Mac | `aicos.db` y `demo_comercial.sqlite3` con el mismo tamaño y mtime; 8000/8731/8743/8750 con los mismos procesos |

**Qué se encontró al implementar:** (0) Las mutaciones P09/P14 delataron dos pruebas que pasaban por una razón distinta de la que decían (arriba). (1) `need()` ya importaba `audit` localmente en su rama de 403; mi rama nueva falló con `UnboundLocalError` en la primera ejecución (corregido: import único de módulo). (2) Tres textos de la herramienta de mutaciones de P1 dejaron de ser únicos por el propio cambio (la prueba `test_every_mutation_still_applies_…` lo detectó, tal como está diseñada).

## 9. Limitaciones y bloqueos pendientes (nada de esto está resuelto)

> **Actualización V0.7 P2.5 (PR A, Issue #25):** **B2** (el dueño no podía entrar con un proveedor real) y **B8** (`idp/start` sin límite propio) se resuelven en [`P25_VINCULO_DUENO_Y_LIMITES.md`](P25_VINCULO_DUENO_Y_LIMITES.md) (vínculo del dueño existente por operación local de un solo uso; cupos persistentes por origen y globales). El resto de la tabla **sigue pendiente**.

| # | Pendiente | Quién / cuándo |
|---|---|---|
| B1 | **No existe proveedor real** (WorkOS, P3): hay que crear una cuenta gratuita (decisión del dueño, sin tarjeta ni datos reales) y **comprobar lo no verificado**: que respete `login_hint`/`prompt=login`, que informe `auth_time`, qué significa su `email_verified` y que el MFA sea obligatorio en cada inicio de sesión. | Dueño → P3 |
| B2 | **El dueño no puede entrar con un proveedor real**: con un proveedor no simulado en el piloto la contraseña se desactiva y **no existe** aún el alta del dueño por proveedor (la CLI crea al dueño con contraseña; el rol `owner` **no** se puede invitar). P3 debe añadir `company_admin` «vincular/invitar al dueño». Hasta entonces, **el piloto con proveedor real no es utilizable**. | P3 |
| B3 | **MFA real: no probado.** `auth_time` ≠ MFA. | P3/P4 |
| B4 | **Correo real: no se envía.** La invitación la entrega el dueño (el secreto sale una vez en la respuesta); `invite(email)` es del proveedor. | P3 |
| B5 | **Sin pantallas de invitaciones ni de aceptación en la SPA.** (La SPA solo trae el cuadro de reautenticación por contraseña.) La reautenticación **por proveedor** existe y está probada en la API, pero la SPA aún no la inicia. | P3 |
| B6 | Reactivar a una persona **no** la reactiva en el proveedor (la interfaz no tiene `enable`); `logout_url()` no está conectado: cerrar sesión no cierra la del proveedor. | P3/P4 |
| B7 | Operaciones sensibles **no** cubiertas aún: `plans.confirm`, `runs.resolve`, copias, exportar/borrar datos. Se dejaron fuera para no añadir fricción sin decisión del dueño. | Dueño |
| B8 | `idp/start` anónimo no tiene un límite propio más allá de la caducidad de 10 min, la purga y el límite de **fallos** de enlace (5 por dirección/15 min); no hay herramienta de operador para **cambiar** la identidad vinculada de una persona. | P4/P6 |
| B9 | El verificador PKCE está en claro en `auth_flows` (≤ 10 min, se borra al caducar). Si se filtra la base en esa ventana, aún falta el `code`. | Aceptado |
| B10 | Los alias de correo (`ana+x@…`) **no** se consideran iguales (a propósito). | — |
| B11 | La guarda de `is_fake` protege la **configuración**, no el código del despliegue (§6). | — |
| B12 | **Sigue pendiente todo lo de P0 §9** (región/contrato de datos, VPS, dominio, canal de alertas, personas de prueba): **no se declara apto para usuarios reales.** | Dueño |

## 10. Cómo reproducir (todo local, sin red)

```bash
cd backend
.venv/bin/python -m pytest tests/test_v07_p2_*.py -q              # las pruebas de P2
cd .. && backend/.venv/bin/python qa/mutations_v07_p2.py          # 45 mutaciones sobre una COPIA temporal (nunca el árbol de trabajo)
backend/.venv/bin/python qa/audit_identity_tenancy.py --check     # guarda estática
cd frontend && npx playwright test -c playwright.pilot.config.ts  # piloto en Chromium real (ventana de reautenticación de 3 s)
```

## 11. Siguiente paso exacto

