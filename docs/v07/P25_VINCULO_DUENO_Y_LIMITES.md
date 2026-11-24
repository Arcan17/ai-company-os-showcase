# V0.7 · P2.5 (PR A) · Vincular al dueño existente y limitar `idp/start`

**Fecha:** 2026-11-23 · **Issue:** [#25](https://github.com/Arcan17/ai-company-os/issues/25) · **Base:** `main` `17bd198` (CI [45301823475](https://github.com/Arcan17/ai-company-os/actions/runs/45301823475) SUCCESS) · **Anterior:** [`P2_IDENTIDAD_E_INVITACIONES.md`](P2_IDENTIDAD_E_INVITACIONES.md) (cierra sus bloqueos **B2** y **B8**)

> **Qué es y qué NO es.** Este PR resuelve dos bloqueos concretos de P2 sin contratar nada: (B2) el dueño que la CLI creó con contraseña puede **vincular su identidad del proveedor**, y (B8) `POST /api/auth/idp/start` queda **limitado de forma persistente**. Sigue todo contra el proveedor **simulado e inyectado** de las pruebas: **no hay proveedor real, ni MFA real, ni correo real**, no se creó ninguna cuenta ni se desplegó nada, y el piloto **sigue sin ser apto para personas reales**. Las pantallas de invitaciones y aceptación (B5) son el PR B, aparte.

## 1. Vincular a un dueño existente (B2)

**Problema:** con un proveedor real en el piloto la contraseña se apaga, pero el dueño original solo existe con contraseña y el rol `owner` no se puede invitar → no podía entrar.

**Diseño (reutiliza tablas, flujo y CLI):**

| Pieza | Qué hace |
|---|---|
| `python -m app.company_admin link-owner --company SLUG [--ttl-minutes N] [--db …]` | **Operación local del operador**: necesita acceso al archivo de la base (la misma autorización que ya crea empresas y dueños). Imprime el secreto **una sola vez**. |
| `revoke-owner-link --company SLUG` / `unlink-owner --company SLUG --yes` | Revoca el secreto pendiente / quita la identidad del dueño **y cierra sus sesiones** (auditado). |
| `invitations` (misma tabla) | Dos columnas aditivas: `kind` (`employee`; `NULL` en bases de P2 = `employee`, o `owner_link`) y `target_user_id`. |
| Flujo (`idp/start` + `callback`) | El **mismo** de las invitaciones: `state` + PKCE + nonce + cookie de correlación de un uso; el flujo se quema antes de hablar con el proveedor; todo fallo = la misma redirección. |

**Reglas (cada una con prueba en `backend/tests/test_v07_p25_owner_link.py`):**

1. **Ninguna ruta HTTP lo inicia.** No se añadió ninguna ruta (la tabla de rutas de P2 no cambió: `test_with_a_provider_the_route_table_grows_by_exactly_the_documented_routes…`). La API **ni lista ni revoca** estos enlaces (`…is_invisible_and_untouchable_from_the_api`).
2. **Secreto**: 256 bits, **solo su hash**, de **un uso**, vigencia **5–1440 min** (60 por defecto), revocable; se busca por el hash y no aparece en ninguna tabla.
3. **No crea personas ni otro dueño.** El objetivo es el dueño **activo** de **esa** empresa (`role='owner'`, `company_id` de la invitación) y debe conservar el **mismo correo** con el que se emitió. Una invitación de empleado **nunca** puede crear un dueño, ni aunque alguien edite la fila (`…can_never_create_an_owner_even_if_the_row_is_forged`).
4. **El proveedor solo confirma el correo exacto del dueño** (booleano `True`; solo cambia mayúsculas/minúsculas; `+etiqueta`, espacios y homoglifos se rechazan). Rol y empresa no salen de él ni del navegador (`…forged_callback_parameters_do_not_change_who_is_linked`, `…the_browser_cannot_ask_for_an_owner_link…`).
5. **Entre empresas:** el token de A no vincula la identidad del dueño de B (ni al revés), y una fila con el objetivo apuntando al dueño de B se rechaza (`…never_links_an_identity_of_the_other`, `…forged_target…`).
6. **Un segundo vínculo exige revocar antes, explícita y auditadamente:** `link-owner` se niega si el dueño ya tiene identidad; `unlink-owner --yes` la quita, cierra sus sesiones y deja auditoría; la identidad desvinculada ya no entra. Un token emitido **antes** de otro vínculo no añade una segunda identidad (y la negativa es la **diseñada** y auditada, no un error de la base).
7. **Auditoría por empresa** (`auth.owner_link_create` / `auth.owner_link` / `…_revoke` / `…_unlink`; actor `system`/`cli` en la operación local), sin secretos ni correos.
8. **Migración aditiva:** una base de P2 sin las columnas migra sola; sus invitaciones de empleado siguen funcionando (`kind` `NULL` = `employee`) (`test_a_p2_database_without_the_new_columns_migrates…`). La CLI migra antes de operar y **no crea** una base vacía si la ruta está mal escrita.

**Después de vincular:** el dueño entra por el proveedor (`subject`, nunca el correo). La contraseña **sigue funcionando fuera de un piloto con proveedor real** (compatibilidad); en un piloto con proveedor real ya está apagada (P2) y esto es lo que lo hace utilizable.

## 2. Límites persistentes de `POST /api/auth/idp/start` (B8)

**Problema:** solo se limitaban los intentos con un enlace **inventado**; un inicio sin invitación, o con un enlace válido, no gastaba ningún cupo → se podía llenar `auth_flows` y abusar del proveedor.

| Cupo | Por defecto | Qué acota |
|---|---|---|
| `idp.start.src` (`AICOS_RL_IDP_START_SRC`) | **30 / 600 s** por origen | un origen no puede crear más de 30 flujos en 10 min (una oficina entera entrando a la vez pasa; 25 seguidos desde una dirección pasan) |
| `idp.start.all` (`AICOS_RL_IDP_START_ALL`) | **300 / 600 s** global | los flujos vivos de `auth_flows` (vida 10 min) y las llamadas al proveedor |

* **Persistentes**: viven en `rate_limits` (el limitador de V0.6): **sobreviven a un reinicio** y las comparten la instancia activa y las de espera; la base no guarda ninguna dirección (cubo = HMAC) — comprobado buscando la dirección en **todas** las tablas.
* **Antes de cualquier trabajo** (es una dependencia: corre antes de validar el cuerpo): **cuenta todo** — sin invitación, enlace válido o inventado, `reauth`, cuerpo mal formado — así que no hay forma de saltarlo sin token. Un inicio rechazado **no crea flujo ni cookie** y no gasta el otro cupo («todo o nada»).
* **Origen = el par TCP, o el cliente real tras un proxy declarado en `AICOS_TRUSTED_PROXIES`**; **nunca** una cabecera libre: probado con seis cabeceras de reenvío falsas (no evaden el límite ni incriminan a otro) y con el prefijo que el cliente antepone tras un proxy (no sirve).
* Reglas mal formadas detienen el arranque (`AICOS_RL_IDP_START_SRC='abc'`…), como el resto de reglas públicas.
* **No cambia:** el callback (no gasta cupo: un flujo ya iniciado se completa aunque el origen esté al límite), el inicio de sesión con contraseña, `reauth`, la creación de invitaciones y su límite, ni el límite de **fallos** de enlaces inventados de P2.

**Trade-offs honestos:** (1) el cupo global lo puede agotar quien controle **≥ 10 orígenes** distintos (300/30): bloquea **nuevos** inicios con el proveedor 10 min, no a las sesiones ya abiertas — límite de abuso, no de disponibilidad garantizada; (2) detrás de un proxy **no declarado** todos comparten un origen (el servidor lo avisa en el registro); (3) los cupos son por defecto y se pueden ajustar con las variables.

## 3. El simulado sigue bloqueado en un piloto real

No cambió nada de P2 (las pruebas de `test_v07_p2_pilot_guard.py` siguen en verde) y se añadieron tres: un enlace de dueño pendiente **no** permite arrancar un piloto con el simulado (código 78, antes de abrir la base); la CLI de vínculo, en un proceso real, **no importa** `app.idp_fake`; y en un piloto con un proveedor no simulado el dueño se vincula con cookies `__Host-` y después la contraseña responde 403.

## 4. Evidencia

| Qué | Resultado |
|---|---|
| **Pruebas rojas primero** (commits `5453e99` y, para la revisión, `71a579a`) | 66 pruebas nuevas: **61 fallan por la razón correcta** (sin `owner_link_create`/`owner_unlink`, sin las columnas `kind`/`target_user_id`, sin los cupos `rl_idp_start_*`, sin los subcomandos de la CLI, y las reglas mal formadas no detenían el arranque) y **5 son guardas** que ya pasaban (el navegador no puede pedir un dueño, el simulado sigue bloqueado en el piloto, sin proveedor no hay ruta…). Revisión del PR (`71a579a`, contra `826d7d6`): **13 rojas** (JSON roto sin cupo, standby, solo POST al path exacto, fallo cerrado, `unlink-owner` con token pendiente) + **4 deterministas de la carrera** (la ejecución queda en `waiting_approval` con la aprobación ya decidida) |
| pytest en `dc54d28` (HEAD del código corregido) | **752 passed** = las 721 de antes **sin modificar ninguna prueba existente salvo la huella de fallos del callback de P2 (ver §4b)** + **31 nuevas** (15 de la carrera, 12 de `idp/start`, 4 de `unlink-owner`); `CI=true AICOS_REQUIRE_RESTIC=1`, 273 s |
| Build, Playwright, demo, piloto en `dc54d28` | build OK · regresión **13/13** (incluye `demo.spec.ts:71`, el que falló en el CI) · demo **4/4** · piloto **2/2** |
| Mutaciones P2.5 (`qa/mutations_v07_p25.py`) en `dc54d28` | **39/39 detectadas** a la primera (la copia sin mutar pasa): las 21 de vínculo del dueño (agujas regeneradas para `unlink-owner`), 9 nuevas del límite ASGI (sin límite, sin global, sin origen, cabecera libre, GET, prefijo de ruta, falla abierto, instalado fuera de CSRF, valor por defecto) y 7 de la carrera (no releer, solo aprobadas, sin CAS, sin guarda `running`, regresión del presupuesto y del material, sin filtro de empresa). Historial: en la 1.ª versión del PR la pasada dio 25/27 (Q03/Q05: defensas redundantes que se enmascaraban) y quedaron aisladas |
| Mutaciones anteriores | P2: 45 agujas siguen aplicándose a una sola vez (se actualizaron 3 que este cambio movió: `P04`, `P41`, `P42`); P1: 29 sin cambios |
| Guarda estática (`qa/audit_identity_tenancy.py --check`) | limpia: **0 consultas sin justificar** (todas las consultas nuevas filtran por `company_id`), sin rutas anónimas nuevas |
| Rutas | **ninguna ruta nueva** (la prueba de P2 «la tabla de rutas crece exactamente en las cinco documentadas» sigue en verde) |
| Bases y puertos del Mac | `aicos.db` y `demo_comercial.sqlite3` con el mismo tamaño y mtime; 8000/8731/8743/8750 con los mismos procesos |
| **CI de GitHub** | **pendiente** (se anota en el PR al terminar; no se afirma verde sin leerlo). El anterior ([45309219594](https://github.com/Arcan17/ai-company-os/actions/runs/45309219594)) falló en `826d7d6` por la carrera de §4b |

**Encontrado al implementar:** al mover texto de `identity_flow.py` y `invitations.py` se rompieron 3 agujas de la herramienta de mutaciones de P2 (la guarda `test_every_p2_mutation_still_applies…` lo avisa, tal como está diseñada); y las mutaciones delataron por segunda vez pruebas que pasaban por una razón distinta de la que decían: cuando dos defensas protegen lo mismo, **cada una necesita una prueba que anule a la otra**.

## 4b. Revisión del PR #26 (revisión automatizada + CI): tres correcciones

| # | Hallazgo | Causa | Corrección | Prueba |
|---|---|---|---|---|
| 1 | **`idp/start` con JSON inválido no gastaba cupo** (P2, revisión automatizada) | El límite era una dependencia de FastAPI; FastAPI rechaza un cuerpo sintácticamente inválido (`{`) **antes** de resolver dependencias | Middleware ASGI `IdpStartLimit` (`ratelimit.py`): solo `POST` al path exacto y solo con proveedor, **antes de leer el cuerpo**; instalado dentro de CSRF y ActiveGate (sus 403/503 no gastan cupo); un limitador que falla rechaza (503); el cupo es el mismo entre instancia activa y en espera | `test_a_syntactically_invalid_body_spends_the_budget…` (4 cuerpos rotos + 3 inválidos de otro tipo), CSRF/origen ajeno, solo POST al path exacto, instancia en espera, fallo cerrado |
| 2 | **`unlink-owner` no revocaba el enlace pendiente si no había identidad** (P2, revisión automatizada) | `owner_unlink` salía antes de revocar | Revoca el enlace pendiente **antes** del retorno temprano; devuelve `links_revoked`; auditoría solo si algo cambió; idempotente; aislado por empresa | `test_unlinking_before_the_owner_links_revokes_the_pending_token…`, flujo ya iniciado, identidad + token a la vez, CLI |
| 3 | **El CI falló en `frontend/e2e/demo.spec.ts:83`** (`customer-confirm` no aparece) | **Carrera real del motor (lost wake-up), no del PR:** `ask_approval` crea la aprobación (ya visible) y varios pasos después marca `waiting_approval`; si se decide en esa ventana, el CAS de `decide()` (`WHERE status='waiting_approval'`) no encuentra nada y nadie reanuda la ejecución, que queda esperando una aprobación ya decidida | `Orchestrator.park_for_approval`: pasar a esperar **y releer esa aprobación en una sola transacción**; si ya está decidida, CAS `waiting_approval → running/decided` y **solo quien gana el CAS lanza** (`decide()` es el otro contendiente). Cubre presupuesto (principal y alternativo) y material | 15 pruebas **deterministas** (`threading.Event`, sin dormir): decidir antes de esperar (aprobar y rechazar), después, doble decisión, 8 carreras con barrera, material, y **una ejecución fallida en la ventana no se reactiva** |

**Por qué se descartó que fuera del PR o del hardware** (la investigación pedida): los 4 CI anteriores del mismo test pasaron (≈ 6 s); 6 corridas locales y 4 bajo CPU saturada (20 procesos) pasaron todas; el fallo del CI duró 22,8 s = 2 s + el `timeout` de 20 s **después** de aprobar. La reproducción **sin navegador** (`aprobar en cuanto la aprobación es visible`, motor en un hilo con ritmo de 0,12 s) dejó la ejecución en `waiting_approval` con la aprobación decidida en **8 de 8** intentos con el código del PR y en **0 de 8** con la corrección. No se subió ningún `timeout` ni se quitó ninguna aserción.

**Hallazgo adicional (mío):** la prueba de P2 `test_every_failure_looks_exactly_the_same_to_the_browser` comparaba el `expires=` de la cookie borrada, que Starlette escribe con la hora **real**: dos respuestas idénticas separadas por un cambio de segundo parecían distintas (flake latente). La huella ahora normaliza `expires=`.

**Ventanas análogas, NO corregidas aquí (a revisar):** `waiting_technician` / `waiting_task` / `blocked` marcan el estado de espera **después** de hacer visible la tarea o la alternativa, y sus reanudaciones (`continue_collab`, `complete_task`, `resolve`) usan el mismo CAS. `resolve` responde 409 explícito («ya no está bloqueada»), pero `continue_collab` y `complete_task` fallan **en silencio** si llegan en la ventana. Probablemente exijan el mismo patrón; no se tocó porque no es lo que falló ni lo que se pidió.

## 5. Limitaciones y pendientes (nada de esto está resuelto)

| # | Pendiente | Quién / cuándo |
|---|---|---|
| L1 | **No hay proveedor real, MFA real ni correo real.** El secreto lo entrega el operador al dueño **por un canal que él elija**; la herramienta no envía nada. | P3 / dueño |
| L2 | `unlink-owner` **no** da de baja la identidad en el proveedor (la interfaz es del dueño de la cuenta allí): el operador debe hacerlo. | P3 |
| L3 | Si el correo del dueño se escribió mal al crear la empresa, el vínculo falla (a propósito) y **no hay herramienta** para corregirlo: hoy exige tocar la base. | PR siguiente |
| L4 | El cupo global de `idp/start` lo agota quien controle muchos orígenes (§2). | Aceptado; se ajusta con las variables |
| L5 | Quien tenga **acceso al archivo de la base** puede emitir este enlace: es la misma confianza que ya existe para crear empresas, pero significa que el operador es de **máxima confianza**. | — |
| L6 | **B5 (pantallas de invitaciones, aceptación y reautenticación por proveedor) sigue pendiente: PR B.** B7 (`plans.confirm`…), B6 (reactivar/`logout_url`) y B12 (todo P0 §9) **tampoco** se tocaron. | PR B / dueño |

## 6. Cómo reproducir (todo local, sin red)

```bash
cd backend
.venv/bin/python -m pytest tests/test_v07_p25_*.py -q             # las pruebas de P2.5
cd .. && backend/.venv/bin/python qa/mutations_v07_p25.py         # 27 mutaciones sobre una COPIA temporal (nunca el árbol de trabajo)
backend/.venv/bin/python qa/audit_identity_tenancy.py --check     # guarda estática
# el operador, sobre su base (no la de demostración del Mac):
cd backend && .venv/bin/python -m app.company_admin link-owner --company mi-empresa --db data/mi_base.db
```

## 7. Siguiente paso exacto

