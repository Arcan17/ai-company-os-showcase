# Seguridad — qué se probó, cómo reproducirlo y qué NO se probó

**Alcance:** prototipo local con datos ficticios. Esto **no** es una certificación ni una auditoría de terceros: es la evidencia que el propio proyecto genera y que cualquiera puede volver a ejecutar. Documentos de detalle: [`docs/v06/`](v06/) (acceso, sesiones, copias, límites, recuperación) y [`docs/v07/`](v07/) (auditoría de identidad y aislamiento, modo piloto, identidad e invitaciones, vínculo del dueño).

## 1. Controles y su prueba
| Tema | Qué hace | Cómo se prueba (comando o archivo) |
|---|---|---|
| **Contraseñas y sesiones** | argon2id (parámetros OWASP); sesiones **opacas** en el servidor (solo el hash del token en la base); vida absoluta de 12 h y límite por inactividad; «cerrar sesión en todos lados» | `backend/tests/test_v06_access.py`, `test_security.py` |
| **CSRF** | Dos capas: origen del navegador (`Origin`/`Sec-Fetch-Site`) y token sincronizador **por sesión**; el login también se protege contra *login CSRF* | `test_v06_access.py`, `test_v06_integration.py` |
| **Intentos de acceso** | Límites **persistentes** por par (correo, dirección), por correo y por dirección; no se registran los intentos bloqueados | `test_v06_access.py` |
| **Aislamiento entre empresas** | La empresa sale **solo** de la sesión; un id ajeno responde igual que uno inexistente; huella SHA-256 de las filas de A antes/después | `backend/tests/test_v07_cross_tenant_matrix.py` (matriz generada del OpenAPI y del registro de herramientas) |
| **Guarda estática de aislamiento** | Toda consulta sobre una tabla de empresa filtra por `company_id` o está justificada una a una; toda ruta anónima está documentada | `python qa/audit_identity_tenancy.py --check` |
| **Canal público del cliente** | Límites persistentes por origen y por empresa (GCRA), sin guardar direcciones (cubos con HMAC); códigos de confirmación protegidos por límites por objetivo; idempotencia del chat con **prueba de posesión** | `test_v06_ratelimit.py`, `test_v07_chat_idempotency.py` |
| **Modo piloto** (`AICOS_ENV=pilot`) | Arranque que **falla cerrado** (código 78, antes de abrir la base) ante una configuración insegura; sin superficie de demostración; cookie `__Host-`; HSTS, `nosniff`, `Referrer-Policy`, `X-Frame-Options`, `frame-ancestors 'none'`; solo atiende los hosts declarados | `test_v07_pilot_mode.py`, `frontend/e2e_pilot` (Chromium real) |
| **Identidad e invitaciones** | Interfaz de proveedor; invitaciones de 7 días, un uso, revocables, secreto solo como hash; `state` + PKCE + nonce + cookie de correlación; inicio por `subject`, nunca por correo; todo fallo con la **misma** respuesta | `backend/tests/test_v07_p2_*.py` |
| **Autenticación reciente** | Las escrituras de configuración y de aprobaciones exigen autenticación de ≤ 15 min (`401 reauth_required`) | `test_v07_p2_step_up.py` |
| **El simulado no inicia sesión en un piloto real** | Ningún valor de configuración lo activa; el piloto se niega a arrancar con él; ningún proceso normal lo importa; sin proveedor no existen las rutas | `test_v07_p2_pilot_guard.py` |
| **Vínculo del dueño y límites de `idp/start`** | Operación local de un uso con caducidad y revocación; no crea personas; cupo por origen y global **antes** de leer el cuerpo | `test_v07_p25_owner_link.py`, `test_v07_p25_idp_start_limits.py` |
| **Ejecución y concurrencia** | Aprobación humana (`approvals.decide` no lo tiene ningún agente); transiciones por compare-and-set; lease de una instancia; recuperación tras reinicio; una aprobación decidida **antes** de que la ejecución espere ya no se pierde | `test_v06_recovery.py`, `test_v07_p25_approval_race.py` (deterministas, sin dormir) |
| **Copias** | Copia verificada, cifrada fuera del servidor (restic) y restauración que borra sesiones y estado de ejecución | `test_v06_backup*.py` |

## 2. Cómo se comprueba que las pruebas valen
- **Pruebas de mutación** (113): se rompe **una** propiedad por vez en una copia y las pruebas deben fallar. `python qa/mutations_v07_p1.py` · `_p2.py` · `_p25.py`. Varias veces sobrevivió una mutación y siempre fue por una **prueba débil** (dos defensas que se enmascaraban), no por el producto; se reforzaron.
- **Rojo primero:** cada hallazgo se escribe como prueba que **falla** por la razón correcta antes de corregir.
- **Prueba desde un clon limpio:** las cifras del README salen de un clon sin estado previo.

## 3. Qué NO se probó (y no debe darse por hecho)
- **No hay auditoría ni pentest de terceros.** Nada aquí sustituye una revisión profesional.
- **No hay proveedor de identidad real, MFA ni correo.** La autenticación reciente mide *cuándo* se autenticó alguien, **no con qué factor**.
- **Sin prueba de carga ni de disponibilidad.** Una sola máquina y SQLite.
- **CSP completa solo en modo informe** y sin colector; un error 500 del servidor ASGI sale sin las cabeceras de seguridad; el token de conversación pública no caduca.
- **Ventanas análogas** a la de la carrera corregida (`waiting_technician`, `waiting_task`, `blocked`): identificadas por lectura de código, **no reproducidas ni corregidas**.
- **Cumplimiento legal** (datos personales, contratos, región de datos): fuera de alcance; ver la oferta y la hoja de ruta.
