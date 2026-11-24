# V0.7 · P0 · Comparativa técnica y económica, y recomendación

**Fecha de los datos:** 2026-11-23 · **Issue:** #17 · **Alcance:** cuentas reales, invitaciones, recuperación, MFA, base de datos, hosting privado y correo para **un piloto privado** con una empresa ficticia de servicios técnicos (1 → 3 → 10 empresas, ≤ 20 usuarios cada una).
**Costo de esta fase: US$ 0.** No se creó ninguna cuenta, no se contrató nada, no se envió ningún dato a ningún servicio: solo se leyeron páginas públicas de precios, documentación y repositorios.

**Confianza de las fuentes.** Marcado así en cada tabla: **[oficial]** = página o documentación del propio proveedor leída hoy; **[repo]** = API de GitHub (licencia, última versión, actividad); **[3.º]** = resumen de terceros (blogs, agregadores) que **no** se pudo contrastar con la página oficial: úsese como indicio, no como cifra. Los precios cambian: **hay que reconfirmarlos antes de contratar** (el propio Clerk reorganizó los suyos el 2026-02-05).

## 1. Qué tiene que resolver la solución (y qué no)

Necesidades del Issue: invitar empleados por un canal verificado, recuperar el acceso, MFA, revocación inmediata, aislamiento entre empresas, HTTPS, exportación y borrado, trazabilidad, costo bajo. **Lo que ya está resuelto y se conserva** (V0.6): sesiones opacas con revocación, CSRF, límites de intentos, argon2id, auditoría, límites del canal público, copias verificadas.

**Criterios y pesos** (puntaje orientativo de 1 a 5: es un juicio razonado, **no una medición**): seguridad y MFA incluido en el plan gratuito 30 % · costo a 1–10 empresas 20 % · esfuerzo de integración y código nuevo 20 % · operación y mantenimiento 20 % · salida (dependencia del proveedor) 10 %.

## 2. Identidad: la decisión importante

La arquitectura que se propone **no depende del proveedor**: la app delega **solo las credenciales** (alta, contraseña, recuperación, MFA, correo de verificación) y conserva **sus propias sesiones, permisos, empresas y auditoría** (ver `P0_ARQUITECTURA_Y_PLAN.md`). Aun así, el proveedor decide costo y esfuerzo.

| Opción | Modelo y licencia | MFA en el plan **gratuito** | Invitaciones | Costo del piloto → 10 empresas | Salida | Puntaje |
|---|---|---|---|---|---|---|
| **WorkOS AuthKit** | gestionado; API/SDK propia (su referencia documenta `authenticate with refresh token` y su SDK); **no** es un ID token OIDC estándar de cliente público — a confirmar en el spike | **Sí, TOTP «Required» en el panel** [oficial: precios y docs de MFA] | **Sí**: el correo de invitación lo envía WorkOS [oficial: docs de invitaciones] | **US$ 0**: primeros 1 000 000 de MAU gratis [oficial]; dominio propio **US$ 99/mes** (opcional); SSO US$ 125 por conexión (no se necesita) | Media-baja: API propia → una capa de adaptación (§4) | **4,2** |
| Auth0 Free | gestionado; OIDC estándar | **No**: «Pro MFA Factors» figura desde el plan Essentials [oficial] | Organizaciones (5 gratis) [oficial] | US$ 0 hasta 25 000 MAU [oficial]; con MFA: Essentials, **desde ≈ US$ 35/mes** [3.º] | Alta (estándar) | 3,9 |
| Logto (Cloud / código abierto) | OIDC estándar; **MPL-2.0**, v1.46.0 [repo] | **No** en la nube gratuita: MFA es un complemento de Pro (**desde US$ 24/mes** + complemento) [oficial]; autoalojado, a verificar | Organizaciones: complemento de pago en la nube [oficial] | US$ 0 (sin MFA) / ≥ US$ 24 con MFA | **Muy alta**: se puede autoalojar | 3,8 |
| Keycloak (autoalojado) | **Apache-2.0**, 26.8.2 (2026-11-05) [repo]; Java + base de datos | Sí (incluido) | Sí (organizaciones) | US$ 0 de licencia, **pero** un servicio más que operar (memoria adicional: estimación, no medida) | Muy alta | 3,8 |
| Amazon Cognito | gestionado; OIDC estándar | MFA por TOTP: **no verificado para el plan Essentials**; SMS se cobra aparte [oficial] | creación de usuarios por administrador con correo de invitación (no verificado) | **10 000 MAU gratis** (Lite/Essentials), luego US$ 0,015 por MAU en Essentials [oficial] | Media (estándar; **a confirmar** si los hashes de contraseña se pueden exportar) | 3,6 |
| Ory Kratos (autoalojado) | **Apache-2.0**, v26.2.0 [repo]; *headless* | Sí (TOTP/WebAuthn) | no nativas | US$ 0 de licencia | Alta | 3,5 |
| Clerk | gestionado; SDK propio con sesiones propias | **No**: MFA solo en Pro, **US$ 25/mes** (US$ 20 anual) [oficial] | Sí, gratis (≤ 20 miembros por organización) [oficial] | US$ 0 sin MFA → US$ 25 con MFA | Baja | 3,3 |
| Construir con bibliotecas (`pyotp` 2.12.1, `py_webauthn` 3.0.1, `authlib` 1.9.10 [repo]) | código propio | lo que escribamos | lo que escribamos | US$ 0, pero **todo** el riesgo | Total | 3,1 |
| Otros revisados | Zitadel **AGPL-3.0** v4.20.5; Authentik y SuperTokens con licencias mixtas (la API de GitHub no declara una) [repo] | — | — | — | — | descartados para el piloto (copyleft o ambigüedad de licencia sin revisar) |

**Descartes con razón.**
* *Construir flujos de recuperación e invitación nosotros:* son el código de seguridad más propenso a errores (entropía y caducidad de los tokens, enumeración de cuentas, toma de cuenta por correo, abuso de invitaciones) y las bibliotecas (`pyotp`, `py_webauthn`) son **primitivas**, no flujos. El Issue pide explícitamente no hacerlo si una solución existente lo resuelve mejor.
* *Autoalojar Keycloak/Kratos en el piloto:* añade un servicio con su propia base, actualizaciones y copias a quien ya opera uno solo. Es la **vía de salida** si algún día se exige no usar un SaaS (soberanía de datos), no el punto de partida.
* *Clerk y Auth0:* su MFA cuesta; con un piloto de ≤ 20 usuarios que **exige MFA a todos** esa partida existe desde el primer día.
* *`fastapi-users`* (MIT, v15.0.5 [repo]) trae sus propias tablas y respaldos de JWT/cookie que **duplicarían** nuestra capa de sesiones, que es la parte ya probada.

**Límites de WorkOS que condicionan el diseño (leídos en su documentación hoy):**
1. Los tokens de AuthKit **no traen `amr`, `acr` ni una marca de «MFA completado»**; `auth_time` es el instante de la última autenticación activa y **no** prueba MFA. La garantía es la configuración «MFA **obligatorio**» (no aplica a usuarios de SSO). → «MFA para operaciones sensibles» se implementa como **MFA obligatorio en cada inicio de sesión + frescura de `auth_time` + reautenticación**, no como un paso adicional puntual.
2. La integración usa su API/SDK propios (no se verificó que ofrezca «código + PKCE + ID token» estándar). → el adaptador (§4 del documento de arquitectura) aísla ese detalle.
3. Un dominio propio para las pantallas de acceso cuesta US$ 99/mes: en el piloto se usa el dominio alojado de WorkOS.
4. **Datos:** el servicio guardaría correo, nombre y credenciales de los usuarios; la región y el contrato de tratamiento **no se verificaron** (ver §7).

## 3. Correo (invitaciones y recuperación)

Si **el proveedor de identidad envía** sus correos (WorkOS lo hace para las invitaciones [oficial]), la app no necesita infraestructura de correo: **US$ 0** y sin dominios SPF/DKIM propios. Si algún día se envía desde la app: Resend, plan gratuito **3 000 correos/mes con tope de 100 al día**, Pro US$ 20/mes por 50 000 [oficial]; Postmark gratis solo 100/mes [3.º]; Amazon SES ≈ US$ 0,10 por 1 000 [3.º, con un cambio de plan para cuentas nuevas reportado en julio de 2026: reconfirmar]. **Recomendación:** que lo envíe el proveedor de identidad en el piloto.

## 4. Base de datos

| Opción | Hecho | Veredicto |
|---|---|---|
| **SQLite (actual) + copias verificadas + restic** | Un nodo, una instancia activa (lease), WAL; copias, restauración y ensayo ya probados (V0.6); 0,7 MB hoy | **Se mantiene para el piloto** |
| PostgreSQL gestionado | Neon: plan gratuito y plan *Launch* por uso (típico ≈ US$ 15/mes) [oficial]; Supabase: gratuito pero **pausa el proyecto tras 1 semana de inactividad** [oficial] → inadecuado para un staging | **Se aplaza**, con disparadores (abajo) |
| PostgreSQL propio | trabajo de portar `backup.py`, índices parciales, `INSERT OR IGNORE`, PRAGMAs y volver a probar todo (auditoría §6) | se aplaza |

**Disparadores para pasar a PostgreSQL (cualquiera):** más de **una instancia** de la aplicación; más de **10 empresas** activas o escrituras concurrentes que superen lo medido en P5; un requisito de **aislamiento a nivel de base** (seguridad por filas) impuesto por un cliente o una auditoría; o una necesidad de réplica de lectura. Hasta entonces la defensa en profundidad del aislamiento son **la guardia estática y la prueba generada de cruce** (P1), que cuestan días, no un proyecto de migración.

## 5. Hosting privado con HTTPS

| Opción | Hecho verificado | Costo mensual | Comentario |
|---|---|---|---|
| **VPS Hetzner CPX12 + Caddy + systemd** | CPX12 **€ 9,99/mes** sin IVA (AMD, 2 GB, 40 GB NVMe); CPX02 € 5,49 (1 GB) [oficial]; regiones: Alemania, Finlandia, **Ashburn, Hillsboro**, Singapur [oficial]; Caddy **Apache-2.0**, v2.11.9 con HTTPS automático [repo] | **≈ € 10** (constante para 1, 3 y 10 empresas) | coincide con lo ya construido y probado: unidades **systemd** y **restic** de V0.6, CI en Linux |
| Fly.io (región `gru`, São Paulo) | región existe; multiplicador 1,615 → máquina `shared-cpu-1x` 256 MB ≈ **US$ 3,54**; RAM US$ 6/GB; volúmenes US$ 0,15/GB; instantáneas US$ 0,08/GB [oficial]; exige tarjeta | ≈ US$ 8–10 con 512 MB y un volumen | menos operación del sistema operativo, pero exige un `Dockerfile` y `fly.toml` que **hoy no existen** en el repositorio |
| Cloudflare Zero Trust (plan gratuito) | hasta **50 usuarios**, ZTNA incluido [oficial] | US$ 0 | segunda puerta opcional delante de la app (necesita un dominio en Cloudflare) |
| Tailscale Funnel (lo que ya usa DocuSeguro) | cliente BSD-3, v1.104.7 [repo] | US$ 0 | no sustituye a un servidor 24/7; **fuera de alcance** (no se toca nada de DocuSeguro/HP) |
| El Mac del dueño | — | — | **descartado**: no es un servidor (se duerme, se apaga; ver `BACKUP_RUTINA_Y_DESTINO_EXTERNO.md` §5) |

**Latencia:** no se midió. Los centros de datos con precio oficial verificado de Hetzner están en Europa y EE. UU.; Fly tiene São Paulo. Para un piloto **sintético** con pocos usuarios no es decisivo; **sí lo es la región de los datos** antes de un piloto con personas reales (decisión del dueño, §7).

## 6. Costos mensuales estimados

| Partida | 1 empresa | 3 empresas | 10 empresas | Nota |
|---|---|---|---|---|
| Identidad (WorkOS AuthKit, MFA obligatorio) | US$ 0 | US$ 0 | US$ 0 | ≤ 200 usuarios ≪ 1 M de MAU gratis [oficial] |
| Correo de invitación y recuperación | US$ 0 | US$ 0 | US$ 0 | lo envía el proveedor |
| Servidor (Hetzner CPX12) | ≈ € 9,99 | ≈ € 9,99 | ≈ € 9,99 | un nodo; **capacidad no medida** con 10 empresas (se mide en P5) |
| Copias externas cifradas (restic → B2/R2) | US$ 0 | US$ 0 | US$ 0 | bajo los 10 GB gratis (ver `BACKUP_RUTINA_Y_DESTINO_EXTERNO.md` §7) |
| Dominio | **no verificado** (≈ US$ 10–15/año) | idem | idem | |
| **Total aproximado** | **≈ € 10** (+ IVA, + dominio) | ≈ € 10 | ≈ € 10 | |
| *Si el proveedor de identidad fuera Auth0 con MFA* | ≥ US$ 35 [3.º] | ≥ US$ 35 | ≥ US$ 35 (+ organizaciones > 5) | |
| *Si fuera Clerk* | US$ 25 | US$ 25 | US$ 25 + organizaciones | |
| *Si WorkOS con dominio propio* | + US$ 99 | + US$ 99 | + US$ 99 | opcional |
No se convirtió a pesos (tipo de cambio no verificado). **Hoy: US$ 0.**

## 7. Ley 21.719 (datos personales, Chile): lo que se sabe y lo que no

* **Vigencia:** la ley se publicó el 2024-12-13 y su artículo primero transitorio fija la entrada en vigencia el **2026-12-01**. Un proyecto de postergación (boletín 18.623-07) fue aprobado por el Senado (33–6) con una nueva fecha del **2027-10-15** y pasó a la Cámara. **A 2026-11-23 los resúmenes consultados indican que la postergación NO es ley publicada**; si no se publica antes del 1 de diciembre, rige la fecha original. Fuentes **secundarias** (estudios jurídicos): [Figueroa & Streit](https://fys.legal/postergacion-ley-datos-personales-fecha-vigencia/), [Eguiguren Abogados](https://www.eguigurenyabogados.cl/newsletter/modificaciones-prorroga-regimen-ley-datos-personales); el texto oficial está en [LeyChile (BCN)](https://www.bcn.cl/leychile/Navegar?idNorma=1209272): **el dueño debe confirmarlo allí antes de cualquier piloto con personas reales.**
* **Implicaciones de diseño (no es cumplimiento):** minimización (al proveedor de identidad solo correo y nombre; los datos del negocio quedan en nuestra base), retención y borrado documentados, exportación y borrado de los datos de una persona, **transferencia internacional** si el proveedor está fuera de Chile (contrato de tratamiento y región por verificar), roles de responsable/encargado, y quitar la IP en claro de `login_attempts` y `audit_log` (H7).
* **Piloto sintético:** con una empresa **ficticia** y cuentas de prueba no hay datos de clientes reales; los correos y nombres de las personas que prueben **sí** son datos personales: se les informa y se minimizan.

## 8. Recomendación

1. **Identidad:** delegar las credenciales a **WorkOS AuthKit** (plan gratuito, MFA TOTP obligatorio, invitaciones y correos incluidos, US$ 0), **detrás de una interfaz propia estrecha** (autorizar, canjear el código, invitar, desactivar, cerrar sesión) para poder cambiar a **Amazon Cognito** (estándar OIDC, 10 000 MAU gratis) o a **Keycloak autoalojado** (salida por soberanía) sin tocar el resto. La app **conserva** sus sesiones opacas, CSRF, permisos por rol, auditoría y `company_id`.
2. **Base de datos:** **SQLite** + copias verificadas + restic para el piloto; **PostgreSQL aplazado** con los disparadores de §4.
3. **Hosting:** un **VPS Hetzner CPX12 + Caddy (HTTPS automático) + systemd**, acceso solo por invitación y, opcionalmente, **Cloudflare Access** (gratuito ≤ 50 usuarios) como segunda puerta.
4. **Sin contratar nada hasta que el dueño lo autorice** (cuentas, tarjeta, dominio, servidor). Antes de elegir definitivamente el proveedor: un **spike** de medio día con el plan gratuito (P3) que compruebe lo que aquí **no** se pudo verificar: recuperación de contraseña, forzar MFA al usuario invitado, reautenticación para operaciones sensibles, desactivar un usuario, región y contrato de tratamiento de datos.
5. **Qué cambiaría esta recomendación:** si el spike muestra que no se puede forzar MFA al usuario invitado o que la reautenticación no es viable → **Cognito** (o Auth0 Essentials); si el dueño exige datos en un proveedor propio o en Chile → **Keycloak autoalojado**; si un cliente exige PostgreSQL → ver los disparadores.
