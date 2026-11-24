# V0.6 P4 — Límites de tasa del canal público (`/api/public/*`)

Issue [#13](https://github.com/Arcan17/ai-company-os/issues/13). **Esto no autoriza despliegue ni datos reales**: cierra el abuso anónimo del canal del cliente, no convierte el prototipo en un producto listo para producción (ver §9).

## 1. El problema, comprobado en código y reproducido

El canal público no usa sesión ni CSRF por diseño (no hay cookie que falsificar). Antes de este cambio nada limitaba sus tres rutas:

| Ruta | Abuso posible | Qué se vio antes del arreglo |
|---|---|---|
| `POST /api/public/{slug}/chat` | cada mensaje abre una conversación y **lanza una ejecución del motor** | 6 chats de más → 6 × `200`; runs sin tope |
| `GET /api/public/{slug}/outbox?token=` | enumerar tokens de conversación; leer a ritmo ilimitado | 60 lecturas inválidas simultáneas → 60 × `404` |
| `POST /api/public/{slug}/visits/confirm` | adivinar el **código de confirmación de la visita: 6 hex = 24 bits** (`secrets.token_hex(3)`) | 10 fuentes distintas → 10 intentos libres, sin techo |

Sin límite, adivinar un código a 1 000 intentos/s tarda ≈ 2,3 h de media (4,7 h el espacio completo). Además, los lanzadores del repositorio arrancaban `uvicorn` con sus opciones por defecto, que **confían en `127.0.0.1` y reescriben `request.client` desde `X-Forwarded-For`**: cualquier visitante detrás de un proxy local que no sobrescriba esa cabecera elegía su propia «dirección».

Las 41 pruebas rojas que lo reproducen están en el commit `c659f72` (`backend/tests/test_v06_ratelimit.py`, ejecutadas contra `main` `07541d8`).

## 2. Diseño

```
petición → CsrfMiddleware → ActiveGate → PublicRateLimit → ruta
              (403)           (503 standby)      (429)
```

* **El limitador es el más interno a propósito**: `/api/public/*` ya está exento de CSRF (no se convierte en 403), y una escritura que una instancia *standby* rechaza con 503 **no gasta presupuesto**. El estado vive en SQLite, así que la instancia activa y la standby comparten **un único presupuesto** (la standby sigue sirviendo `outbox` y `confirm`).
* **Algoritmo: GCRA** (un cubo de fichas guardado como **un solo número** por cubo, `tat`). Regla `N/V`: ráfaga de `N` y luego una petición cada `V/N` s. La decisión completa es **un único `INSERT … ON CONFLICT DO UPDATE … WHERE`**: «comprobar» y «gastar» no pueden separarse, ni entre hilos ni entre procesos (SQLite serializa a los escritores).
* **Cadenas de cubos en una sola transacción de escritura**: una petición gasta una ficha de *cada* cubo de su cadena (fuente → empresa), **o de ninguno**. Una petición rechazada no gasta nada, no prolonga ningún bloqueo y no puede usarse para vaciar el presupuesto de otro. Antes de escribir hay una lectura sin bloqueo: una avalancha de rechazadas nunca toma el candado de escritura.
* **Sin oráculo.** Las lecturas fallidas (`outbox.miss`) se cobran **antes** de buscar y se **devuelven** si la búsqueda acierta. Agotado el presupuesto, un token válido y uno inválido reciben el mismo `429`. Igual con la confirmación: ni siquiera el código correcto responde distinto cuando el presupuesto de esa solicitud se acabó.
* **Falla cerrado.** Si el limitador no puede decidir (base bloqueada, error), la petición recibe `503` + `Retry-After: 5`; nunca se deja pasar.
* **Respuesta**: `429` con `Retry-After` (segundos, ≥ 1) y mensaje en español. Nada de lo rechazado crea runs, solicitudes, visitas, auditoría ni filas en la tabla de límites.

### Cubos y valores por defecto (`AICOS_RL_*`, formato `peticiones/ventana en segundos`)

| Cubo | Clave | Por defecto | Equivale a | Para qué |
|---|---|---|---|---|
| `chat.src` | empresa + fuente | `20/600` | ráfaga 20, luego 2/min | quien más conversaciones abre |
| `chat.co` | empresa | `200/3600` | ráfaga 200, luego 3,3/min | tapa total: protege al motor aunque roten direcciones |
| `outbox.src` | empresa + fuente | `150/60` | 150/min | una pestaña sondea ~50/min; caben ~3 |
| `outbox.co` | empresa | `1200/60` | 1 200/min | |
| `outbox.miss` | empresa + fuente | `10/300` | ráfaga 10, luego 2/min | tokens que no existen (enumeración) |
| `confirm.src` | empresa + fuente | `10/600` | ráfaga 10, luego 1/min | **el más estricto por fuente** |
| `confirm.target` | empresa + **solicitud** | `5/900` | ráfaga 5, luego 1 cada 3 min | **tope por solicitud, venga de la fuente que venga** |
| `confirm.co` | empresa | `60/600` | | adivinanza repartida entre muchas solicitudes |
| `unknown.src` / `unknown.all` | fuente / global | `20/60` · `200/60` | | *slugs* inexistentes y rutas públicas desconocidas: un nombre elegido por el atacante **no crea cubos** |

* **Aislamiento entre empresas**: la clave incluye la empresa. Agotar Clima Sur desde una fuente no consume nada de Motor Norte (probado a nivel de fuente y de tapa de empresa).
* **Idempotencia (`client_msg_id`)**: un reintento duplicado devuelve la misma ejecución (`deduplicated: true`) **pero también gasta presupuesto**: reenviar el mismo mensaje no es gratis. Un `429` no crea nada; pasada la ventana, el mismo id devuelve la ejecución original sin duplicarla.
* **Ajuste solo por configuración.** Una regla mal formada (`0/60`, `abc`, `5/0`, `1e9/60`) **detiene el arranque** nombrando la variable. **No existe un interruptor que apague los límites ni un modo de pruebas/demo que los relaje**: el módulo no contiene ninguna rama de ese tipo (`test_the_limiter_has_no_test_mode_branch`) y la suite corre con los mismos valores que producción. Los E2E usaron 10 conversaciones públicas (regresión) y 3 (demo) frente a los 20 de ráfaga, sin tocar ningún valor.

### Por qué un código de 6 hex ya no se puede adivinar (a ritmo humano)

Intentos máximos sobre **una** solicitud: `5 + t/180 s`. En 24 h: 485 → probabilidad de acierto ≤ 485 / 16 777 216 ≈ **0,003 %**; en 7 días ≈ 0,02 %. Y la tapa `confirm.co` limita el total por empresa a 60 + 6/min **entre todas las solicitudes**. Rotar direcciones no ayuda: el cubo `confirm.target` no depende de la fuente.

## 3. Quién es «la misma fuente»

1. La dirección del **par TCP** (`scope["client"]`). IPv6 se agrupa por su **/64** (un cliente controla un /64 entero: rotar dentro no crea fuentes nuevas); IPv4 mapeado en IPv6 equivale a su IPv4.
2. **`X-Forwarded-For` solo se cree si el par es un proxy listado en `AICOS_TRUSTED_PROXIES`** (IPs/CIDR separados por comas; **vacío por defecto = ninguna cabecera se cree jamás**). Entonces se recorre la cabecera **de derecha a izquierda** y gana la primera dirección que no sea a su vez un proxy de confianza; lo que el cliente antepone a la izquierda se ignora. Un valor no interpretable en esa frontera deja al proxy como fuente (no se adivina).
3. **No se lee nunca** `Forwarded`, `X-Real-IP`, `True-Client-IP`, `CF-Connecting-IP`, `Client-IP` ni `Host` (la empresa sale de la ruta, no de la cabecera).
4. La misma regla se aplica ahora al **límite de intentos de login** (`api.client_ip` → `ratelimit.client_address`).

### Configuración de un proxy inverso (obligatoria si lo hay)

```bash
# uvicorn NO debe procesar las cabeceras de proxy: la confianza se declara solo con AICOS_TRUSTED_PROXIES
AICOS_TRUSTED_PROXIES=127.0.0.1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8731 --no-proxy-headers
```

* El proxy debe **añadir a la derecha de `X-Forwarded-For` la dirección que él ve** (nginx: `proxy_set_header X-Forwarded-For $remote_addr;` sobrescribe lo que traiga el cliente; ejemplo no ejecutado en este repositorio). Un proxy que **no** pone la cabecera (reenvío TCP puro) deja a todos los visitantes en **un solo cubo** (el del proxy): el servidor lo avisa **una vez** en el log cuando ve la cabecera de un par local/privado que no es de confianza.
* **No uses** `--forwarded-allow-ips '*'` ni dejes `--proxy-headers` activo: uvicorn reescribiría la dirección **antes** de que la vea la aplicación y el limitador no podría distinguir una cabecera falsa. Hay una prueba que **falla si un lanzador de este repositorio** (`run_*.sh`, Playwright, `qa/`, pruebas) arranca `uvicorn` sin `--no-proxy-headers`/`proxy_headers=False` (`test_every_launcher_of_this_repo_disables_uvicorn_proxy_headers`).
* **NAT/CGNAT**: visitantes legítimos que comparten dirección comparten presupuesto. Los valores por defecto toleran una oficina pequeña; nadie queda bloqueado indefinidamente (las fichas se reponen continuamente). Detrás de un NAT grande, sube `AICOS_RL_CHAT_SRC` / `AICOS_RL_OUTBOX_SRC` en vez de desactivar nada.

## 4. Privacidad y retención

* **No se guarda ninguna dirección, token ni id de solicitud.** El nombre de cada cubo es `HMAC-SHA256(clave, tipo|empresa|sujeto)` (128 bits). La clave es aleatoria y por base (`rate_limit_meta`, creada con `INSERT OR IGNORE`: dos procesos que arrancan a la vez comparten la misma). Columnas: `bucket, kind, company_id, tat`. No hay columna de IP (probado, y se comprobó además que el archivo `.db` y su WAL no contienen las direcciones usadas).
* **Retención = lo que dura la ventana.** Una fila cuyo `tat` ya pasó equivale a un cubo lleno, es decir, a no tener fila, y se **borra** (barrido cada 30 s, en la siguiente petición aceptada). Máximo: la ventana más larga por defecto, 1 h (`chat.co`). No se acumula historial de visitantes.
* **Tamaño acotado por la tapa de empresa, no por el atacante**: las filas de fuente solo existen para peticiones *aceptadas* (la cadena es atómica: si la tapa de empresa rechaza, la fila de fuente se deshace). Cota por empresa = ráfaga + reposición de su tapa durante la ventana de las filas que cuelgan de ella: ≲ 250 filas de `chat`, ≲ 2 400 de `outbox`, ≲ 150 de `confirm`; del orden de 100 bytes por fila, es decir, muy por debajo de 1 MB. Los *slugs* inventados comparten dos cubos. Índices: PK `bucket` (búsqueda) e `ix_rate_limits_tat` (barrido).
* **Copias de seguridad**: `rate_limits` y `rate_limit_meta` son **estado de ejecución, no datos**: `restore` los vacía siempre (como `instance_lease`) y el siguiente arranque crea una clave nueva (probado). Una copia nunca lleva contadores de visitantes.
* **Límite de esta protección**: quien lea la base durante la ventana puede, conociendo la clave, probar direcciones IPv4 (espacio de 2³²) y saber cuáles llamaron en la última hora. La mitigación es la retención corta, no el secreto.
* **Hallazgo previo, fuera de este PR**: `login_attempts.ip` y el detalle de auditoría `auth.login` (de #10) **sí guardan la IP en claro** (comprobado en la base del ensayo: esas dos columnas son las únicas con la dirección; el canal público no escribe ninguna). Queda como mejora aparte (minimizar/anonimizar tras N días).

## 5. Concurrencia y varios procesos

* Hilos: 100 lecturas inválidas simultáneas contra un servidor real con el presupuesto por defecto (10/300) → **exactamente 10** `404` y 90 `429`. En las pruebas: 60 lecturas con presupuesto 7 → 7 y 53; 24 chats con presupuesto 5 → **exactamente 5** runs; 30 confirmaciones sobre una solicitud con presupuesto 4 → exactamente 4.
* Procesos: dos servidores reales sobre el mismo archivo (activa + standby) comparten el presupuesto; matar con `SIGKILL` a uno no «perdona» nada (el estado vive en la base).
* Un mismo servidor puede ser activo y estar bajo carga de ejecuciones: el limitador usa transacciones cortas de un solo `UPSERT` por cubo (sin lectura previa dentro de la transacción, que en WAL falla con `SQLITE_BUSY_SNAPSHOT`) y corre en el pool de hilos para no bloquear el bucle de eventos.
* Medido en el ensayo (servidor real, límites por defecto, 80 sondeos válidos): **p50 5,0 ms · p95 5,7 ms · máx 7,4 ms** por sondeo, que incluye ≈ 3 lecturas y 3 transacciones de escritura del limitador. Un sondeo cada 1,2 s por pestaña.

## 6. Alertas

Una línea `WARNING` por (tipo, empresa) y minuto, con el número de rechazos suprimidos, **sin direcciones** y sin escrituras extra en la base durante un ataque:

```
límite público excedido: kind=confirm.target empresa=clima-sur (rechazos suprimidos desde el aviso anterior: 12)
```

| Línea | Significado | Acción |
|---|---|---|
| `kind=confirm.target` / `confirm.src` | alguien está probando códigos | revisar la solicitud; no hay que hacer nada para detenerlo |
| `kind=chat.co` / `outbox.co` / `confirm.co` | la empresa agotó su tapa: ataque distribuido **o** crecimiento real | decidir con el volumen real; si es legítimo, subir la regla |
| `kind=unknown.*` | barrido de rutas/empresas | informativo |
| `el limitador del canal público falló` (`ERROR` + traza) | se está fallando cerrado | investigar la base (bloqueos, disco) |
| `X-Forwarded-For recibida de un par local o privado …` | proxy sin configurar | configurar `AICOS_TRUSTED_PROXIES` |

Estado actual, sin direcciones: `SELECT kind, count(*), max(tat) FROM rate_limits GROUP BY kind;`. Los logs salen por stdout/stderr de `uvicorn` (los scripts `run_*.sh` los guardan en `backend/data/*.log`); no hay todavía un colector de alertas.

## 7. Qué pruebas fijan cada afirmación

`backend/tests/test_v06_ratelimit.py` (49 pruebas). Mutaciones comprobadas (se rompe **una** propiedad cada vez y la suite nueva debe notarlo; las 15 sistemáticas fueron detectadas, más una manual sobre el barrido de mantenimiento):

| Mutación | Detectada por |
|---|---|
| el upsert deja de ser condicional | 2 de las 3 pruebas de carreras entre hilos (chat y confirmación; la de lecturas inválidas no siempre cae en la ventana de carrera) |
| la cadena deja de ser todo-o-nada | las 3 pruebas de carreras |
| se cree `X-Forwarded-For` de cualquier par | 5 pruebas (incluida la del servidor real) |
| se usa la dirección más a la izquierda | proxy de confianza + login |
| las lecturas válidas no devuelven su ficha | sondeo legítimo + presupuesto de fallos |
| el presupuesto de fallos se cobra después de buscar | 7 pruebas (entre ellas la del oráculo: token válido tras agotar) |
| el limitador queda fuera del gate de standby | standby no gasta presupuesto |
| los cubos no distinguen empresa | 3 pruebas de aislamiento |
| el tope por solicitud depende de la fuente | rotación de fuentes |
| el nombre del cubo guarda la dirección en claro | privacidad |
| nunca se purga | retención |
| falla abierto | falla cerrado |
| regla inválida aceptada | 7 casos de arranque |
| sin tope por solicitud | confirmación |
| el login vuelve a mirar solo el par TCP | login con proxy |

## 8. Despliegue y reversa

* **Migración aditiva** (`create_all`): dos tablas nuevas; nada existente cambia. Comprobada (a) sobre **copias** de `aicos.db` (1 235 filas) y `demo_comercial.sqlite3` (114 filas), con el original abierto solo en lectura y copiado con la API de respaldo de SQLite: la app arranca sobre la copia, añade las tablas que faltan y **no cambia ninguna fila preexistente**; esas dos bases tienen la forma anterior a V0.6, así que la migración les añade también `instance_lease` y `login_attempts` (de #10/#12); marcas de tiempo y tamaño de cada original y de su `-wal`/`-shm` idénticos antes y después; y (b) por la prueba `test_an_existing_database_gets_the_tables_added_and_keeps_its_data`.
* **Reversa**: volver al commit anterior. Las dos tablas quedan sin uso y se pueden borrar a mano; el resto del esquema no depende de ellas.
* Además del código y las pruebas, el PR solo toca los lanzadores de este repositorio (`--no-proxy-headers`); **ningún servidor ni base del Mac fue tocado** (los servidores de los puertos 8731/8743 corren código anterior a V0.6 y no se reiniciaron) y nada se despliega.

## 9. Lo que NO cubre (pendiente, no oculto)

1. **Disponibilidad bajo ataque distribuido**: las tapas de empresa protegen al motor, pero un atacante con muchas direcciones que gaste esa tapa (`chat.co`: 3,3 por minuto sostenidos) puede **degradar** el chat de esa empresa. El compromiso es deliberado (el motor es el recurso caro); la alerta lo hace visible. No hay bloqueo por reputación, CAPTCHA ni *proof-of-work*.
2. **Fuentes compartidas (NAT)**: un abusador y clientes legítimos tras la misma dirección comparten presupuesto; un cliente legítimo puede recibir 429 en `outbox` mientras otro agota los fallos. La SPA ignora los errores de sondeo y reintenta a los 1,2 s: no hay *backoff* con `Retry-After`.
3. **El código sigue siendo de 24 bits.** El límite lo hace inadivinable en la práctica, pero subir la entropía (p. ej. 8 caracteres base32 = 40 bits) es la defensa de fondo y cambia el código que ve el cliente: queda como mejora aparte.
4. **Tamaño del cuerpo**: el limitador cuenta peticiones, no bytes; no se midió ni se limitó el tamaño de un cuerpo enorme (el modelo valida 2 000 caracteres *después* de leer el cuerpo).
5. **Varias empresas**: un atacante que conozca *n* `slug` obtiene *n* presupuestos por fuente (cada uno acotado por su tapa). Aceptable con pocas empresas.
6. **Solo SQLite y un solo equipo**: usa `MAX/MIN` escalares e `INSERT OR IGNORE`; migrar a PostgreSQL exige `GREATEST/LEAST` y `ON CONFLICT DO NOTHING`. Varias máquinas no pueden compartir el presupuesto.
7. **La IP en claro de `login_attempts` / auditoría de login** (§4), TLS, MFA y el cifrado de copias siguen pendientes (ver `ACCESO_Y_SESIONES.md` §5 y `BACKUP_RESTORE.md`).
8. Si alguien arranca `uvicorn` a mano con `--proxy-headers --forwarded-allow-ips '*'`, anula la protección: el repositorio no puede impedirlo, solo documentarlo y fallar si **sus** lanzadores lo hacen.

## 10. Biblioteca descartada

`limits` (y `slowapi`, que la envuelve) ofrece almacenamiento en **memoria, Redis, Memcached, MongoDB y Etcd** ([repositorio](https://github.com/alisaifee/limits)); no tiene uno de SQLite compartido entre procesos. Este prototipo es un archivo SQLite con dos instancias, y el límite debe **sobrevivir al reinicio y ser común a ambas**. Añadir Redis para un prototipo local contradice el criterio del issue. Lo escrito aquí son ≈ 330 líneas (documentación incluida); lo que sí se reutiliza es el candado de escritura de SQLite y el `UPSERT` condicional.

## 11. Evidencia (22-nov-2026, hora de Chile; Mac del autor, sin red externa)

Árbol verificado: commit `f960772` sobre `main` `07541d8`, árbol limpio. El PR enlaza además el resultado de GitHub Actions sobre la cabeza de la rama.

| Etapa | Resultado |
|---|---|
| Rojo (antes del arreglo, contra `main`) | 41 de 46 pruebas nuevas **fallan** (`c659f72`); las 5 que pasan son guardas del comportamiento previo (sondeo legítimo, CSRF, rutas de staff) |
| `pytest backend/tests` | **293 passed** (244 previas + 49 nuevas) en 111,8 s |
| Compilación del frontend | OK |
| Playwright de regresión | **12 / 12** (82 s) |
| Demo comercial | **4 / 4** (10,7 s) |
| Mutaciones | 15 / 15 detectadas (+ 1 manual) |

**Ensayo con servidor real** (`uvicorn … --no-proxy-headers`, límites por defecto, base temporal, puerto libre; las cabeceras `X-Forwarded-For` rotan en cada intento):

```
1. confirmación, 12 adivinanzas con XFF rotando: 5×400 → 7×429 | el código CORRECTO después: 429 Retry-After: 60
   la visita sigue sin confirmar (estado=propuesta)
2. outbox, 14 tokens inventados con XFF rotando: 10×404 → 4×429 | el token VÁLIDO después: 429 (sin oráculo)
3. chats seguidos tras el primero: 19×200 → 1×429 | Retry-After: 29 s; 10 más: 10×429 | runs antes/después: 20/20
4. Motor Norte desde la misma fuente tras agotar Clima Sur: 200
5. 100 lecturas inválidas SIMULTÁNEAS: 10×404 · 90×429 · 0 otros
6. rate_limits: columnas [bucket, kind, company_id, tat]; ninguna dirección usada aparece en el archivo de la base ni en su WAL
```

Sobre la base del ensayo, `127.0.0.1` sí aparece en `login_attempts.ip` y en el detalle de auditoría de `auth.login`: es el inicio de sesión del dueño que hace el propio script (comportamiento previo de #10, ver §4); el canal público no escribió ninguna.

**No se tocó** `aicos.db` (mtime 22-nov 00:44), `demo_comercial.sqlite3` (22-nov 02:09), ni los servidores de los puertos 8731/8743/8000/8750, ni Safari; nada se desplegó. La migración se probó solo sobre copias (§8).
