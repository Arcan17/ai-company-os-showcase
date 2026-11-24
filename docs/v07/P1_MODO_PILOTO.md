# V0.7 · P1 · Modo piloto (`AICOS_ENV=pilot`) y matriz dinámica de aislamiento entre empresas

**Issue:** [#19](https://github.com/Arcan17/ai-company-os/issues/19) · **Base:** `main` `56cdda0` (P0 fusionado, CI [45268209000](https://github.com/Arcan17/ai-company-os/actions/runs/45268209000) success) · **Diseño:** `P0_ARQUITECTURA_Y_PLAN.md` §4 y §6 (AC1–AC8).

> **Estado honesto.** Esto deja el **modo de ejecución** listo para un piloto PRIVADO con datos sintéticos. **No** hace apto el producto para usuarios reales: siguen sin existir identidad real (P2–P4), servidor y copias externas (P5), y el [Issue #20](https://github.com/Arcan17/ai-company-os/issues/20) (idempotencia del chat público) debe cerrarse **antes** de admitir a una persona real. No se contrató ni se desplegó nada.

## 1. Qué cambia y qué no

| Aspecto | Sin `AICOS_ENV` (o `local`) — **idéntico a antes** | `AICOS_ENV=pilot` |
|---|---|---|
| Base nueva | siembra Clima Sur, Motor Norte (+ demo) con cuentas `is_demo=1` | **vacía**; el operador da de alta la empresa con la CLI (`python -m app.company_admin create …`) |
| `GET /api/auth/demo-accounts` (devolvía la contraseña de demostración) | 200 | **404** (la ruta no se registra) |
| `/demo`, `/demo-login/*`, `/api/demo/*` (incl. `reset-scenarios`) | como antes (solo con `AICOS_DEMO_MODE=1` salvo `demo/info`, `reset-scenarios`) | **404** |
| `/api/setup/*` (alta local de empresas) | como antes (solo con `AICOS_ALLOW_COMPANY_CREATION=1` + loopback) | **404** |
| Cookie de sesión | `aicos_session`; `Secure` si `AICOS_COOKIE_SECURE=1` o (`auto` y https) | **`__Host-aicos_session`**, `Secure` **siempre**, `HttpOnly`, `SameSite=Lax`, `Path=/`, sin `Domain`; el borrado del logout también cumple el prefijo |
| Cabeceras | ninguna nueva | HSTS, `nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `CSP: frame-ancestors 'none'` (se aplica), CSP completa en **modo informe**, `Cache-Control: no-store` en `/api/*` — en **toda** respuesta, incluidos 400/403/429/503 de otros middlewares |
| Host | cualquiera | solo los nombres de `AICOS_TRUSTED_ORIGINS` (`host[:puerto]`); otro → `400 host no permitido` |
| Arranque | sin comprobaciones | **falla cerrado** (§2) |

No hay migración de esquema. **Reversión:** quitar `AICOS_ENV` (la base del piloto es una base SQLite normal; una base con empresas propias arranca también sin `AICOS_ENV`).

## 2. El arranque se niega a continuar (código de salida 78, antes de abrir la base)

`validate_startup` corre al principio de `create_app`. El mensaje **nombra las variables y nunca imprime sus valores**, ni correos, ni la ruta de la base. Si algo falla, no se abre, crea ni migra la base.

| Condición | Variable que se nombra |
|---|---|
| `AICOS_COOKIE_SECURE` distinto de `1` (`auto` o `0`) | `AICOS_COOKIE_SECURE` |
| `AICOS_TRUSTED_ORIGINS` vacío | `AICOS_TRUSTED_ORIGINS` |
| `AICOS_DEMO_MODE=1` | `AICOS_DEMO_MODE` |
| `AICOS_ALLOW_COMPANY_CREATION=1` | `AICOS_ALLOW_COMPANY_CREATION` |
| `AICOS_TRUSTED_PROXIES` con algo que no sea la propia máquina o una red privada (RFC 1918 / ULA / loopback) — p. ej. `0.0.0.0/0`, `::/0`, una IP pública — o ininterpretable | `AICOS_TRUSTED_PROXIES` |
| la base **ya contiene** usuarios `is_demo=1` (se abre en **solo lectura**) o no se puede comprobar (no SQLite, ilegible) | `AICOS_DB_URL` |
| `AICOS_ENV` con un valor que no es `local` ni `pilot` (p. ej. un error de tipeo: no se cae en silencio a «local») | `AICOS_ENV` |

`python -m app.main` / `uvicorn app.main:app` salen con **78** y **sin traza** (`app/main.py`). Un `HTTPS` detrás de proxy **no** depende de cabeceras de internet: la cookie es `Secure` aunque la petición llegue por http, `X-Forwarded-*` nunca se consulta, y la dirección del cliente es la de la conexión salvo que el proxy esté en `AICOS_TRUSTED_PROXIES` (y siempre se arranca con `uvicorn --no-proxy-headers`).

**Ejemplo de entorno** (sin secretos; el piloto aún usa contraseña local):

```bash
AICOS_ENV=pilot  AICOS_COOKIE_SECURE=1  AICOS_TRUSTED_ORIGINS=pilot.ejemplo.test
AICOS_DB_URL=sqlite:////ruta/propia/piloto.db   # una base NUEVA, nunca la de laboratorio
# primera empresa FICTICIA: sin --owner-password se genera una y se muestra UNA vez (no la pase por la línea de comandos si no la necesita)
python -m app.company_admin create --db /ruta/propia/piloto.db --name "Empresa Piloto" --owner-name "Nombre" --owner-email dueno@ejemplo.test
uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers   # detrás de un proxy TLS (Caddy, en P5)
```

## 3. Límites que se conocen (no se esconden)

1. **El `Host` de una comprobación de salud local debe ser uno declarado**: `curl -H "Host: pilot.ejemplo.test" http://127.0.0.1:8000/api/health`. Es deliberado (falla cerrado).
2. **La CSP completa va en modo informe y sin punto de recogida** (`report-uri`): hoy solo se ve en la consola del navegador. Recoger informes es trabajo de P5; la protección **aplicada** hoy es `frame-ancestors 'none'` + `X-Frame-Options: DENY`.
3. Un **500 generado por el servidor ASGI** (excepción no controlada) sale por encima de los middlewares de la aplicación y **no lleva** las cabeceras (el proxy de P5 debe repetir HSTS/`nosniff`).
4. HSTS usa `includeSubDomains` y **no** `preload` (no se puede deshacer fácilmente): el piloto debe tener un dominio propio.
5. Chromium acepta `Secure` + `__Host-` en `http://127.0.0.1`/`localhost`; Safari **no**. Por eso el piloto real va siempre con HTTPS y la prueba de navegador (`frontend/e2e_pilot`) usa Chromium.
6. La contraseña local sigue siendo el único inicio de sesión (P2 la sustituye).

## 4. Matriz dinámica de aislamiento entre empresas (AC5 y AC6)

`backend/tests/test_v07_cross_tenant_matrix.py` (60 pruebas, datos sintéticos, base temporal, **modo piloto activo**: dos empresas creadas con la misma función que la CLI del operador, sin siembra de demostración). La empresa **A** es la víctima (datos de todos los tipos: ejecución pendiente y bloqueada, mantención con material y compromisos, plan, tarea de plan, conversación pública y código de visita); la **B** es la atacante (dueño, administración y técnico con sesión).

**Se genera, no se escribe a mano.** El inventario sale del **OpenAPI de la aplicación viva** (arrancada en `local` y en `pilot`, para que una ruta de demostración nueva también exija decisión) y del **registro de herramientas**. Una ruta autenticada con un identificador —en la ruta, la consulta o el cuerpo— o una herramienta con identificadores que **no tenga caso ni justificación hace fallar la prueba** (`test_every_authenticated_route_with_an_identifier_has_a_matrix_case`, `test_every_registered_tool_with_an_identifier_has_a_matrix_case`, `test_every_other_authenticated_route_is_a_list_or_justified`).

**Oráculo** (por cada ruta y por cada identificador): B repite una llamada **válida** de B cambiando ese identificador por el de A, y se exige:
1. la respuesta es **idéntica** (estado y cuerpo, con los ids normalizados) a la que recibe con un identificador **inexistente** — no hay canal que distinga «existe en otra empresa» de «no existe»;
2. ni una cadena de A (id, nombre de empresa, cliente, correo, token, código de visita) en la respuesta, salvo el eco de lo que B mismo envió;
3. los datos de A **no cambian**: huella SHA-256 de todas sus filas de las 27 tablas con `company_id`, antes y después;
4. **control anti-vacío**: la misma llamada con los ids propios de B **funciona** y **difiere** de la respuesta al id inexistente. Si no difiriera, la prueba fallaría por otra razón y no probaría el aislamiento (así se detectó el hallazgo O2, §5).

| Qué | Cobertura |
|---|---|
| Rutas con identificador (`POST /api/approvals/{aid}/decide`, `GET /api/runs/{rid}`, `…/resolve`, `…/reassign`, `…/complete`, `config/*`, `plans/*`, `tools/{name}/invoke`, …) | **19 rutas**; los ids secundarios en el cuerpo (`new_owner_user_id`, `admin_user_id`, `user_id`) se cambian uno por uno |
| Listas | **todas** las rutas `GET` autenticadas sin identificador, como dueño, administración y técnico de B; más una prueba de que el escáner **sí** ve los datos de A cuando A los lista (si no, «sin fugas» podría significar «el escáner no ve nada») |
| SSE | B no abre `/api/runs/{id}/stream` ni `/api/plans/{id}/stream` de A (404 idéntico al inexistente); su flujo propio, abierto **mientras A emite eventos nuevos**, no transporta nada de A; leer los eventos de A con la empresa de B devuelve `[]` y con la de A, no. Se prueba sobre la app real por ASGI (el `TestClient` no entrega flujos infinitos) |
| Capa de herramientas | **31 de las 47** herramientas (las 16 restantes no reciben referencias a objetos), llamadas **directamente** con un principal de B que tiene **solo** el permiso de esa herramienta — alcanza también las que solo invocan los agentes y no son llamables por HTTP |
| Canal público (AC6) | el token de A con el `slug` de B → igual que un token inexistente; el código y la solicitud de A con el `slug` de B → no confirman nada y no cambian A; el **mismo** `client_msg_id` en dos empresas abre conversaciones distintas |

**Lo que NO prueba (no se esconde):** no es una prueba de ausencia de fugas, solo de las llamadas que la matriz construye; no cubre identificadores que el cliente **elige** y el servidor trata como claves de idempotencia (`client_msg_id`, `client_plan_id`, `client_request_id`: ver el [Issue #20](https://github.com/Arcan17/ai-company-os/issues/20) para el primero); no ejercita rutas anónimas más allá del canal público; y los ids de 48 bits no se prueban por fuerza bruta. El análisis estático (`qa/audit_identity_tenancy.py`, AC7) sigue vigente y ahora cubre **39 módulos / 387 consultas / 33 sin `company_id` justificadas una a una** (antes 38 / 386 / 32: la única consulta nueva es la comprobación de arranque de `pilot.py`, de solo lectura).

## 5. Hallazgos de la matriz

**O2 — `tasks.create` y `materials.request` aceptaban referencias de otra empresa (corregido en este PR).** Guardaban `request_id`, `assignee_user_id` y `commitment_id` sin comprobar que fueran de la empresa. Verificado con una base temporal: un administrador de B que conoce el id de un empleado de A creaba una tarea con ese `assignee_user_id` y el panel de B (`GET /api/dashboard`) mostraba **el nombre del empleado de A** (el `LEFT JOIN users` no filtraba por empresa). **Alcance real:** exige conocer un identificador de 48 bits; no se leyó ni modificó ningún dato de A más allá de ese nombre. **Arreglo:** `_own_refs` en `tools/definitions.py` (misma respuesta para ausente y de otra empresa) + los cinco `JOIN` de tareas a `users` filtran también por `company_id`. Solo cambia el comportamiento ante referencias inválidas, que antes se aceptaban en silencio. La matriz estaba **roja** por esto en el commit de pruebas (`71125ea`) y pasa a verde en `9ba8f38`.

**O1 / Issue #20 — sigue abierto** (idempotencia del chat público): no se toca en este PR (PR propio, ver el Issue). El PR #20 debe cerrarse antes de admitir a una persona real.

## 6. Mutaciones (AC8)

`qa/mutations_v07_p1.py` rompe **una** propiedad cada vez en una copia temporal y exige que las pruebas lo noten (primero comprueba que la copia sin mutar pasa). `python qa/mutations_v07_p1.py [--list|--only M05,M12|--check-needles]`. Una prueba (`test_v07_mutation_script.py`) comprueba que cada texto a mutar sigue existiendo una sola vez, para que la herramienta no se pudra.

| # | Mutación (una propiedad) | La detecta |
|---|---|---|
| M01 | demo-accounts vuelve a registrarse en piloto | `pilot_mode.py::test_pilot_starts_empty_and_hides_the_demo_surface` |
| M02 | setup/* vuelve a registrarse en piloto | `pilot_mode.py::test_pilot_starts_empty_and_hides_the_demo_surface` |
| M03 | demo/info y reset-scenarios vuelven en piloto | `pilot_mode.py::test_pilot_starts_empty_and_hides_the_demo_surface` |
| M04 | seed() siembra la demostración en piloto | `pilot_mode.py::test_pilot_starts_empty_and_hides_the_demo_surface` |
| M05 | se acepta AICOS_COOKIE_SECURE distinto de 1 | `pilot_mode.py::rechazo de configuración[AICOS_COOKIE_SECURE-0]` |
| M06 | se acepta AICOS_TRUSTED_ORIGINS vacío | `pilot_mode.py::rechazo de configuración[AICOS_TRUSTED_ORIGINS-2]` |
| M07 | se acepta AICOS_DEMO_MODE=1 | `pilot_mode.py::rechazo de configuración[AICOS_DEMO_MODE-3]` |
| M08 | se acepta AICOS_ALLOW_COMPANY_CREATION=1 | `pilot_mode.py::rechazo de configuración[AICOS_ALLOW_COMPANY_CREATION-4]` |
| M09 | se aceptan proxies de confianza públicos | `pilot_mode.py::rechazo de configuración[AICOS_TRUSTED_PROXIES-5]` |
| M10 | se acepta una base con cuentas de demostración | `pilot_mode.py::test_pilot_refuses_a_database_with_demo_accounts_and_leaves_it_untouched` |
| M11 | se acepta un AICOS_ENV desconocido | `pilot_mode.py::rechazo de configuración[AICOS_ENV-8]` |
| M12 | create_app no llama a validate_startup | `pilot_mode.py::rechazo de configuración[AICOS_COOKIE_SECURE-0]` |
| M13 | cookie de piloto sin prefijo __Host- | `pilot_mode.py::test_pilot_cookie_and_headers` |
| M14 | la cookie sin prefijo también autentica en piloto | `pilot_mode.py::test_pilot_rejects_the_unprefixed_session_cookie` |
| M15 | Secure ya no se fuerza en piloto (solo depende de la configuración) | `pilot_mode.py::test_pilot_cookie_stays_secure_even_if_the_setting_changes_after_startup` |
| M16 | el borrado de cookie del logout no cumple __Host- | `pilot_mode.py::test_pilot_logout_sends_a_valid_host_prefixed_deletion` |
| M17 | falta HSTS | `pilot_mode.py::test_pilot_security_headers_are_on_every_response` |
| M18 | las respuestas de /api/* ya no llevan no-store | `pilot_mode.py::test_pilot_security_headers_are_on_every_response` |
| M19 | SecurityHeaders deja de ser el más externo (el rechazo de host sale sin cabeceras) | `pilot_mode.py::test_pilot_only_answers_to_the_declared_host` |
| M20 | no se valida el host | `pilot_mode.py::test_pilot_only_answers_to_the_declared_host` |
| M21 | falta la CSP que impide incrustar la aplicación | `pilot_mode.py::test_pilot_security_headers_are_on_every_response` |
| M22 | GET /api/runs/{id} sin filtro de empresa | `cross_tenant_matrix.py::test_cross_tenant_matrix[GET /api/runs/{rid}]` |
| M23 | el coordinador carga planes sin filtro de empresa | `cross_tenant_matrix.py::herramientas[visits.propose]` |
| M24 | el flujo SSE de una ejecución se abre sin comprobar la empresa | `cross_tenant_matrix.py::test_cross_tenant_matrix[GET /api/runs/{rid}/stream]` |
| M25 | el token público se acepta con el slug de OTRA empresa | `cross_tenant_matrix.py::test_public_token_is_scoped_to_its_company` |
| M26 | la confirmación de visita no filtra por empresa | `cross_tenant_matrix.py::test_a_visit_code_and_request_of_a_do_nothing_through_the_slug_of_b` |
| M27 | las herramientas buscan solicitudes sin filtro de empresa | `cross_tenant_matrix.py::test_cross_tenant_matrix[POST /api/tools/{name}/invoke]` |
| M28 | una ruta NUEVA con identificador sin caso en la matriz | `cross_tenant_matrix.py::test_every_authenticated_route_with_an_identifier_has_a_matrix_case` |
| M29 | una consulta NUEVA sin company_id en un módulo anidado | `audit_tool.py::test_every_query_on_a_tenant_table_filters_by_company_or_is_justified` |

**29 / 29 detectadas; sobreviven: ninguna.** (Base sin mutar: pasa.) Una mutación equivalente apareció durante el desarrollo —quitar `pilot or` del `Secure` forzado no cambiaba nada porque el arranque ya exige `AICOS_COOKIE_SECURE=1`— y se resolvió con una prueba que cambia el ajuste *después* del arranque (M15), no con una excepción.

## 7. Evidencia (Mac, base temporal; el último commit solo cambia documentación)

| Qué | Resultado |
|---|---|
| Fase roja (commit `71125ea`) | **24 pruebas en rojo** (22 de modo piloto —`AICOS_ENV=pilot` aún no hacía nada— y 2 de la matriz por el hallazgo O2) y 64 guardas del comportamiento actual en verde; salida en el PR |
| Código verificado (commit `5e996c0`, árbol limpio, restic exigido) | **pytest 477 passed** (236 s) = las **387 pruebas de `main` sin modificar ni una línea** (`git diff 56cdda0 -- backend/tests` solo añade archivos) + 90 nuevas (29 modo piloto, 60 matriz, 1 herramienta de mutaciones) · build OK · Playwright **12/12** (81 s) · demo comercial **4/4** (11 s) · piloto en Chromium real **1/1** (nuevo) |
| Mutaciones | **29 / 29** detectadas (base sin mutar: pasa) |
| Guardia estática (AC7) | `--check` sale 0; 39 módulos · 387 consultas · 33 sin `company_id` justificadas una a una |
| Bases y servidores reales | `backend/data/aicos.db` y `backend/data/demo_comercial.sqlite3` con el **mismo tamaño y mtime** antes y después; los procesos de los puertos 8000/8731/8743/8750 siguen siendo los mismos (ningún reinicio). Ninguna prueba nombra esas bases (lo vigila una prueba) |
| CI de GitHub Actions | ver el PR (se verifica sobre el SHA final; **pendiente** hasta entonces) |

Reproducir: `backend/.venv/bin/python -m pytest -q backend/tests` · `python qa/mutations_v07_p1.py` · `cd frontend && npx playwright test -c playwright.pilot.config.ts`.

## 8. Qué sigue (no está en este PR)

1. **Issue #20** (PR propio, ya preparado: `fix/v07-chat-idempotency-token`) **antes de admitir a ninguna persona real**.
2. P2: proveedor de identidad detrás de nuestra interfaz (`FakeProvider`), invitaciones y `auth_time`/*step-up*; P5: servidor, HTTPS (Caddy), copias fuera de la máquina, recogida de informes de la CSP. **Nada de esto se contrató ni se desplegó.**
3. Observaciones que no bloquean: `client_plan_id` / `client_request_id` siguen usando `Math.random()` (rutas autenticadas; ver el Issue #20 §5); el análisis estático da por «filtradas» 4 consultas que solo mencionan `company_id` como columna (P0 §5).


