# V0.6 · Acceso: sesiones, CSRF y protección del login

**Fecha:** 2026-11-22 · **Rama:** `feat/v06-access-hardening` (apilada sobre `feat/v06-ci-and-product-roadmap`, PR #9) · **Issue:** #8 · **Datos:** solo ficticios.
No es un producto listo para clientes: es el primer paso del gate M1 de `docs/ROADMAP_V1_PRODUCTO.md` (ver «Lo que NO cubre»).

## 1. Qué cambia

| Área | Antes (V0.5) | Ahora (V0.6) |
|---|---|---|
| Escrituras con cookie | `SameSite=Lax` como única defensa | **Token CSRF sincronizado** por sesión (`X-CSRF-Token`) **+** verificación de origen (`Sec-Fetch-Site` same-origin / `Origin` = Host) en todo `POST/PUT/PATCH/DELETE` de `/api/` |
| Login | sin límite; el correo inexistente respondía más rápido | contadores persistentes por (correo, IP), por correo y por IP; `429` + `Retry-After`; mismo mensaje para correo conocido y desconocido; trabajo de hash constante |
| Hash de contraseña | scrypt `n=2^14,p=1` (bajo el mínimo OWASP) | **argon2id** `m=19 MiB, t=2, p=1` (mínimo OWASP; ≈15 ms), con **re-hash transparente** de los hashes scrypt en el siguiente login correcto |
| Sesión | 12 h absolutas, sin renovación | 12 h absolutas **+ 120 min de inactividad** (deslizante, una escritura por minuto como máximo); nuevo login **reemplaza** la sesión presentada; purga de expiradas |
| Cierre | solo la sesión actual | + **«cerrar sesión en todos lados»** (`POST /api/auth/logout-all`) |
| Cookie | `HttpOnly; SameSite=Lax` | + `Secure` automático cuando la petición llegó por https (o forzado con `AICOS_COOKIE_SECURE=1`) |
| Auditoría | solo acciones de dominio | `auth.login` ok/denied, `auth.locked`, `auth.logout`, `auth.logout_all` en `audit_log`; los intentos de correos desconocidos solo en `login_attempts` (sin inquilino al que auditar). **Nunca** se guarda la contraseña probada |
| Endpoints «solo local» | `client.host` ∈ loopback | además: **sin** cabeceras de reenvío (`X-Forwarded-*`, `Forwarded`, `X-Real-IP`) y `Host` de loopback (un proxy en la misma máquina ya no abre `/api/setup/companies`; frena DNS-rebinding) |

Migración **aditiva** (`sessions.csrf`, `sessions.last_seen_at`, tabla `login_attempts`); ensayada sobre **copias** de `aicos.db` y `demo_comercial.sqlite3` (login 200, token emitido, hash migrado a argon2id, conteos intactos, `integrity_check ok`). Las sesiones V0.5 abiertas siguen vivas: `/api/auth/me` les emite su token.

## 2. Amenazas → control → prueba

| Abuso | Control | Prueba (`backend/tests/test_v06_access.py` salvo indicación) |
|---|---|---|
| Página ajena hace POST con la cookie de la víctima | token por sesión + origen | `test_csrf_write_with_cookie_but_without_token_is_rejected`, `test_csrf_browser_origin_signals` (9 casos) |
| Sitio hermano (`admin.localhost` → `dueno.localhost`, mismo *site*) | `Sec-Fetch-Site: same-site` se rechaza | caso `same-site` de `test_csrf_browser_origin_signals` |
| Token de otra sesión/usuario | el token vive en la fila de **esa** sesión | `test_csrf_token_is_bound_to_its_session` |
| «Login CSRF» (te meten en la cuenta del atacante) | el origen se verifica también en `/api/auth/login` | `test_login_csrf_cross_origin_login_is_rejected` |
| Fuerza bruta a una cuenta | 5 fallos/15 min por (correo, IP) | `test_account_locks_after_repeated_failures_even_with_the_right_password` |
| Adivinar desde muchas IP | 20 fallos/15 min por correo | `test_distributed_guessing_against_one_email_is_capped` |
| Relleno de credenciales | 50 fallos/15 min por IP | `test_ip_wide_failures_are_capped_across_many_emails` |
| Enumeración de cuentas (mensaje o tiempo) | mismo `429`/`401`; `DUMMY_HASH` verificado | `test_lock_response_does_not_reveal…`, `test_unknown_email_costs_the_same_hash_work…` |
| Un bloqueo no debe extenderse martillando | los intentos bloqueados **no** se registran | `test_lock_expires_with_the_rolling_window` |
| Sesión olvidada / equipo perdido | inactividad 120 min; logout-all | `test_idle_timeout_and_sliding_activity`, `test_logout_all_revokes_every_session…` |
| Fijación / acumulación de sesiones | login reemplaza la sesión presentada | `test_new_login_replaces_the_session_presented_by_the_cookie` |
| Hash débil | argon2id ≥ mínimo OWASP + re-hash | `test_new_hashes_are_argon2id…`, `test_legacy_scrypt_hash_still_logs_in_and_is_upgraded_in_place` |
| Proxy local / DNS-rebinding abre endpoints locales | sin reenvío + Host loopback | `test_loopback_only_endpoints_refuse_proxied_or_foreign_host_requests` (6 casos) |
| Base V0.5 con sesiones vivas | migración aditiva + token perezoso | `test_v05_database_migrates_additively_and_old_sessions_survive` |
| La SPA real funciona con todo esto | E2E V6-01…03 (`v05_third_company.spec.ts`) | escritura sin token = 403 · token correcto + `Origin` ajeno = 403 · logout-all cierra también la otra sesión |

## 3. Herramientas existentes evaluadas (datos verificados el 2026-11-22 en PyPI/GitHub)

| Necesidad | Candidato | Hallazgo | Decisión |
|---|---|---|---|
| Hash de contraseñas | **`argon2-cffi` 25.1.0** (MIT, release 2025-06-03, repo con push 2026-11-13) | Primera opción de OWASP; trae `check_needs_rehash` | **Reutilizado** (dependencia nueva; wheels para macOS y Linux; 4 paquetes pinneados) |
| CSRF | `starlette-csrf` 3.0.0 | **último release 2023-06-27**, double-submit (cookie legible por JS) | Descartado: abandonado y el patrón double-submit es el más débil frente a cookies inyectadas desde subdominios hermanos (exactamente nuestro modo demo) |
| CSRF | `fastapi-csrf-protect` 1.0.7 (MIT, 2025-09-16) | activo, pero también double-submit y exige su propio estado de cookie | Descartado: ya tenemos sesión en BD; un token sincronizado cuesta ~25 líneas, 0 dependencias y es lo que OWASP prefiere para apps con estado |
| Límite de intentos | `slowapi` 0.1.10 / `limits` 5.8.0 (MIT, activos) | contador **en memoria o Redis**, clave por IP; no da bloqueo por cuenta, no persiste tras reinicio, no audita | Descartado para el login (un contador que se borra al reiniciar y no distingue cuentas no cumple el objetivo). Evaluado de nuevo para `/api/public/*` en #13 y **también descartado** (sus almacenamientos son memoria, Redis, Memcached, MongoDB y Etcd; ninguno es un archivo SQLite compartido por dos procesos): se escribió un limitador persistente, ver `LIMITES_CANAL_PUBLICO.md` §10 |
| Proxy de confianza | `uvicorn --proxy-headers --forwarded-allow-ips` | ya incluido en uvicorn | **Reutilizado hasta #12; sustituido en #13**: `--proxy-headers` reescribe `request.client` *antes* de que la aplicación lo vea (y por defecto confía en `127.0.0.1`), así que un visitante detrás de un proxy local que no sobrescriba `X-Forwarded-For` elegía su dirección. Ahora todos los lanzadores usan `--no-proxy-headers` y la confianza se declara con `AICOS_TRUSTED_PROXIES` (`LIMITES_CANAL_PUBLICO.md` §3); el límite de login usa la misma regla. Se mantiene el rechazo de cabeceras de reenvío en endpoints locales |
| Sesiones firmadas | `itsdangerous` | cookie firmada sin estado = no revocable | Descartado: necesitamos revocación inmediata (desactivar empleado, logout-all) |

## 4. Configuración (todo opcional; valores por defecto seguros)

`AICOS_SESSION_IDLE_MINUTES=120` · `AICOS_COOKIE_SECURE=auto|1|0` · `AICOS_TRUSTED_ORIGINS=host[:puerto],…` (orígenes de navegador adicionales para escribir) · `AICOS_LOGIN_WINDOW_SECONDS=900` · `AICOS_LOGIN_MAX_PAIR=5` · `AICOS_LOGIN_MAX_EMAIL=20` · `AICOS_LOGIN_MAX_IP=50` · `AICOS_TRUSTED_PROXIES=ip|cidr,…` (vacío = no se cree ninguna cabecera de proxy) · límites del canal público `AICOS_RL_*` (ver `LIMITES_CANAL_PUBLICO.md`).

## 5. Lo que NO cubre (pendiente, no oculto)

1. **TLS y despliegue**: `Secure` solo es efectivo con https; no hay TLS en este repo. Prefijo `__Host-` del nombre de cookie: pendiente hasta tener https (Safari no acepta cookies `Secure` por http).
2. **MFA, cambio/recuperación de contraseña, invitaciones**: V0.7. Hoy la contraseña de un empleado la fija el dueño al crearlo.
3. **Bloqueo como palanca de DoS**: quien conozca un correo puede bloquearlo hasta 15 min (≥20 fallos desde distintas IP) o bloquear una IP compartida (≥50). Es el compromiso de ASVS (sin CAPTCHA ni segundo factor); queda registrado en `audit_log` y expira solo.
4. ~~`/api/public/*` sin límite de tasa~~ **resuelto en #13**: límites persistentes por empresa y por fuente, con tope por solicitud para el código de visita. Pendiente de aquello: ver `LIMITES_CANAL_PUBLICO.md` §9.
5. `GET /demo-login/*` cambia estado por GET; solo existe en modo demo, loopback y Host local.
6. El token CSRF y las cookies no protegen contra **XSS**; la SPA no usa `dangerouslySetInnerHTML`, pero no hay CSP todavía.
7. La IP del cliente es la del socket salvo que el par esté en `AICOS_TRUSTED_PROXIES` (uvicorn corre con `--no-proxy-headers`). `login_attempts.ip` y el detalle de auditoría de `auth.login` siguen guardando la IP **en claro**.

## 6. Clientes que no son el navegador (scripts de QA)

Un script con cookie de sesión pero sin token recibe `403 csrf: token inválido o ausente`: es la defensa funcionando, no un fallo. Hallazgo de la revisión de este PR: `qa/evidence.py` (`KeyError: 'plan'`) y `qa/llm_audit.py` (`AssertionError: (403, …)`) hacían `POST` sin `X-CSRF-Token`.
Corrección **sin relajar nada** (no hay bandera de omisión): `qa/qa_client.py::csrf_login(client, email)` inicia sesión, conserva el `csrf_token` que devuelve el servidor y lo envía en cada petición (se reemplaza en cada login: el token es de **esa** sesión). Ambos scripts lo usan. Para endpoints nuevos, cualquier cliente debe hacer lo mismo: `POST /api/auth/login` → `csrf_token` → cabecera `X-CSRF-Token`.
Los scripts ahora aceptan rutas por entorno (`AICOS_QA_DB`, `AICOS_QA_URL`, `AICOS_QA_SOURCE_DB`, `AICOS_QA_OUT`; por defecto, las de siempre) para poder ejecutarlos **de verdad** en pruebas sobre una base ficticia temporal, nunca sobre una base del usuario. Pruebas: `backend/tests/test_v06_qa_scripts.py` (4; las dos que ejecutan los scripts reales fallan si se vuelve al login sin token).

## 7. Cómo verificar

```bash
backend/.venv/bin/python -m pytest -q backend/tests            # 191 = 145 previas + 42 de acceso + 4 de scripts QA
cd frontend && npm run build && npx playwright test            # 12 (incluye V6-01…03)
```
