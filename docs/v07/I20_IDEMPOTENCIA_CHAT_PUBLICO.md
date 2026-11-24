# V0.7 · Seguridad pre-piloto · Idempotencia del chat público sin regalar la conversación (Issue #20)

**Issue:** [#20](https://github.com/Arcan17/ai-company-os/issues/20) · **Base:** `main` `56cdda0` · **Alcance:** un PR pequeño y propio, independiente del modo piloto (#19). **Estado: hallazgo de revisión, riesgo POTENCIAL; explotación contra datos reales NO demostrada** (solo reproducida en una base temporal con empresas ficticias).

## 1. El problema (verificado antes del arreglo)

`POST /api/public/{slug}/chat` identificaba un reintento **solo** por `client_msg_id`. Ante un reintento devolvía el `conversation_token` de la conversación original **a cualquier petición pública de la misma empresa** que repitiera ese id; y ese token es la **credencial** de `GET /api/public/{slug}/outbox?token=…` (lee los mensajes, la solicitud y el código de confirmación de visita). El widget generaba el id con `Math.random()` (8 caracteres base36, ≈ 41 bits y no criptográfico).

Efecto: quien conozca o adivine el id de un mensaje ajeno **de la misma empresa** obtiene la conversación de otra persona. La búsqueda estaba acotada por empresa (`company_id`), así que **no hay cruce entre empresas**; el límite de tasa de `/api/public/*` (#13) acota los intentos pero no sustituye a la autorización.

Segundo defecto, encontrado al reproducirlo: el **motor** también usaba el id como clave de la *solicitud* (`requests.create` con `chat:{id}`), de modo que una ejecución con un id repetido habría reutilizado la **solicitud y el cliente** de la víctima. Y una **ráfaga** de peticiones idénticas simultáneas terminaba en `UNIQUE constraint failed: messages.idem_key` (500), ya en `main`.

> «No basta sustituir `Math.random()`»: un id aleatorio mejor **no autoriza** nada (es un identificador, no una credencial; puede aparecer en registros, capturas, soporte o un reintento de red) ni elimina la divulgación por ids conocidos. Hay que **demostrar posesión** para recuperar el token.

## 2. Contrato de idempotencia (después)

| Elemento | Qué es | Qué NO es |
|---|---|---|
| `client_msg_id` | identifica el **envío** (6–80 caracteres `[A-Za-z0-9_-]`) | una credencial: puede conocerse |
| `retry_secret` *(nuevo, opcional)* | lo genera el **cliente** con un CSPRNG (≥ 128 bits; el widget envía 22 caracteres base64url); 22–128 caracteres `[A-Za-z0-9_-]` y al menos 10 distintos (cordura, **no** prueba de entropía) | nunca se guarda ni se registra: del servidor solo sale su **huella SHA-256 (32 hex)** dentro de la clave de idempotencia |
| Clave de idempotencia | `(empresa, "chat:{client_msg_id}:{huella(retry_secret)}")` | — |

* **Reintento legítimo** (mismo id **y** mismo secreto, p. ej. tras un *timeout*): encuentra la ejecución y recibe la **misma** `run_id`, `conversation_token` y `request_id` (`deduplicated: true`), sin crear ejecución, conversación, mensaje ni solicitud nuevos.
* **Mismo id, secreto ausente o distinto:** la clave es otra → **no hay nada que repetir** → es una conversación **nueva y propia** del solicitante, con su propio token. No recibe nada de la víctima, **no hay canal de existencia** (la respuesta es la de cualquier conversación nueva) y no se puede **ocupar** el id de otro (la clave incluye el secreto).
* **Sin `retry_secret`:** no hay idempotencia por id (cada petición es una conversación). El widget siempre lo envía. Un cliente de API que quiera reintentos idempotentes debe generar el secreto.
* **Ráfaga idéntica:** la primera crea; las demás, tras perder la inserción (clave única), vuelven a **leer** y reciben la misma conversación. Una sola ejecución.
* El límite de tasa de #13 sigue cobrando **cada** petición (un reintento no es gratis) y es por empresa.

Además, defensa en profundidad: la lectura del token de la conversación filtra `AND company_id` (se quitó su entrada de la lista de consultas globales justificadas de `qa/audit_identity_tenancy.py`), y el motor usa **la misma clave** para la solicitud (`state.input.chat_key`; las ejecuciones en curso creadas antes del cambio conservan la clave antigua).

## 3. Qué cambia (archivos)

`backend/app/api.py` (campo + validación) · `backend/app/engine/orchestrator.py` (`ChatInput.retry_secret`, clave de idempotencia, reintento ante ráfaga, sin persistir el secreto) · `frontend/src/pages/CustomerChat.tsx` (id y secreto de 128 bits con `crypto.getRandomValues`) · pruebas.

**Pruebas existentes modificadas (5, a propósito):** codificaban el comportamiento vulnerable —reintentar por id **sin** secreto debía recuperar la misma conversación (una de ellas lo usaba incluso como atajo para volver a leer su token)— y ahora llevan un secreto: `test_flow.py::test_retries_do_not_duplicate`, `test_v06_ratelimit.py::body()` (todas las peticiones llevan secreto; el resto de esa suite no cambia), `test_v06_recovery.py::…customer_retry_creates_no_duplicates`, `test_scenario_d.py::start_d` y su relectura del token, y el ayudante `conftest.chat(secret=…)`. Ninguna aserción se relajó: el reintento sigue devolviendo la **misma** ejecución.

## 4. Pruebas (rojo → verde)

`backend/tests/test_v07_chat_idempotency.py` (15): **11 en rojo contra `main`** (el atacante recibe el token de la víctima con solo el id; con un secreto equivocado también; su ejecución reutilizaba la solicitud de la víctima; sin secreto se deduplicaba; la validación del secreto no existía; la ráfaga de 8 peticiones lanzaba `IntegrityError`) y 4 guardas que pasan antes y después (reintento legítimo, reintento tras terminar la ejecución, misma clave en otra empresa, el secreto no se guarda). Navegador real: `frontend/e2e/v07_chat_idempotency.spec.ts` (el widget envía 128 bits de CSPRNG y el reintento recupera la misma conversación).

## 5. Límites y riesgos que quedan

1. **Quien robe el par (id + secreto) del navegador de la víctima** (XSS, extensión, captura de la memoria de la pestaña) recupera su conversación: el secreto vive solo en memoria de la pestaña, pero eso es lo máximo que se puede ofrecer sin cuentas de cliente. HTTPS es obligatorio (el piloto ya lo exige, #19).
2. **Los tokens de conversación siguen siendo la credencial de lectura** (`outbox`): 24 hex generados por el servidor (96 bits); con el límite de tasa y el contador de «token inexistente» (#13) la adivinación no es práctica, pero no hay caducidad ni revocación del token (backlog).
3. **Un reintento sin secreto no se recupera** (p. ej. si la pestaña se recargó): se crea una conversación nueva; la anterior queda sin dueño en el navegador. Es el coste de no regalar credenciales.
4. Las demás claves de idempotencia del cliente (`client_plan_id`, `client_request_id`, con `Math.random()` en `Coordinator.tsx`/`Operations.tsx`) son de rutas **autenticadas**, acotadas por empresa y usuario, y no devuelven ninguna credencial; no se tocan aquí.
5. `retry_secret` viaja en el cuerpo de una petición `POST` sobre HTTPS; **no registrar cuerpos de petición** (decisión de P5, ya prevista en el diseño del piloto).

## 6. Reversión

Es compatible hacia atrás para el widget y para las bases existentes (sin migración). Revertir el PR devuelve el comportamiento vulnerable: **no se recomienda** si ya hay personas reales.
