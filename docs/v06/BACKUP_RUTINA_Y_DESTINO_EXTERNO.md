# V0.6 P5 · Rutina de respaldos, ensayo de restauración programable y destino externo cifrado

**Fecha:** 2026-11-23 · **Rama:** `feat/v06-backup-schedule-drill` (desde `main` `8d0c020`) · **Issue:** #15 · **Gate M1:** «restauración comprobada».
Módulos: `backend/app/backup_routine.py` · `backup_external.py` · `backup_schedule.py` (y mejoras a `backup.py`). Pruebas: `backend/tests/test_v06_backup_{safety,routine,external,schedule}.py` (+ `bkutil.py`).

> ### Estado, dicho sin adornos
> * **Construido y probado en local, sobre bases y repositorios TEMPORALES.**
> * **No instalado:** ningún `launchd`, `cron` ni `systemd` se creó en este equipo. El generador solo escribe archivos de texto en un directorio nuevo.
> * **No conectado:** ninguna cuenta, bucket, token ni contraseña existe. El destino externo (restic) se probó contra un repositorio en un directorio temporal. **Costo real hoy: US$ 0.**
> * **No toca nada del Mac del dueño:** `aicos.db`, `demo_comercial.sqlite3` y los puertos 8731/8743/8750/8000 no se abrieron, migraron ni reiniciaron.
> * Hasta que el dueño autorice la agenda y el destino, **no existen copias automáticas ni copias fuera del equipo**. Esto no está listo para datos reales.

## 1. Qué hace una pasada

```
python -m app.backup_routine run --db BASE --out DIRECTORIO [--external --env-file F]

  cerrojo (flock)  ──► limpiar restos viejos de un cierre brusco
        │
        ▼
  copia en línea de la base VIVA (origen solo-lectura) ──► verify (sha256 + tamaño + integrity_check + filas)
        │
        ▼
  ENSAYO: restore a ruta NUEVA ──► arrancar la app real sobre lo restaurado (migraciones + lectura)
        │        (sesiones revocadas, lease limpiado, instancia ACTIVA; todo medido = RTO)
        │  ─ si falla: pasada FALLIDA (exit 2); no se borra nada y no sale nada de la máquina
        ▼
  retención local: las N más nuevas ∪ la más nueva de cada uno de los últimos D días ∪ la última que VERIFICA
        ▼
  (opcional) restic push CIFRADO ──► retención externa ──► (cada 7 d) bajar y restaurar lo subido
        ▼
  diario (journal.jsonl, 0600, acotado, sin secretos)
```

| Comando | Para qué | Código de salida |
|---|---|---|
| `run` | una pasada completa | **0** ok · **1** terminó con avisos (p. ej. destino externo caído; la copia local está bien) · **2** falló · **75** ya había otra corriendo |
| `status` | estado, RPO/RTO medidos, alertas | **0** ok · **1** aviso · **2** crítico |
| `scrub [--external] [--read-data]` | re-verificar las N copias guardadas y, con `--external`, el repositorio (`restic check`, 5 % de los datos o todo con `--read-data`). **Pedir lo externo y no poder hacerlo es un FALLO**, nunca un «OK» solo-local | 0 · 2 |
| `schedule` | **preparar** archivos de agenda (no instala) | 0 · 2 |
| `external-init` / `external-rotate-key` | crear el repositorio cifrado / rotar su contraseña (pasos **explícitos**) | 0 · 2 |

Reglas que las pruebas fijan:

1. **Una sola pasada a la vez.** `flock` del kernel sobre `DIRECTORIO/.routine/run.lock`: lo suelta el sistema si el proceso muere (incluso con `SIGKILL`), no quedan candados huérfanos. Una segunda invocación termina con 75 y queda anotada como `skipped`.
2. **Nada sale ni se borra tras un ensayo fallido.** La retención y la subida externa solo ocurren después de una copia verificada **y** restaurada con éxito.
3. **El estado no se fía del diario.** La «última copia buena» sale de **verificar los archivos ahora** (un archivo dañado después de la pasada aparece como dañado). El diario aporta solo lo que los archivos no pueden decir: ensayos y destino externo.
4. **Falla cerrado.** Sin copias, copia dañada, copia más vieja que el objetivo o ensayo fallido → **crítico**. Un destino externo caído es **aviso**, no pérdida local (con `--external-required` pasa a crítico).
5. **Retención que no destruye.** Contar las N más nuevas no protege nada si esas N son las dañadas (en `main` 8d0c020 `prune --keep 2` borraba la única copia buena: prueba roja de este Issue). Ahora la última que verifica sobrevive siempre y, si **ninguna** verifica, no se borra nada.
6. **Un fallo de disco es un fallo limpio.** Sin permiso, sin espacio o con un límite de tamaño → `FALLO: …` y exit 2, sin *traceback*, sin copia a medias visible y sin borrar copias anteriores.
7. **Un cierre brusco no deja nada confiable.** Sin manifiesto, un archivo no es una copia (no se lista, verifica ni restaura). Los restos se limpian en la siguiente pasada, **solo si son viejos** (uno reciente podría ser un `create` vivo de otra terminal).
8. **`scrub --external` no puede dar un éxito falso** *(PR #16, hallazgo revisión automatizada P2)*. El resultado lleva `external: {requested, performed, ok, code}`. Con una configuración ausente, con permisos abiertos, con una clave no permitida, sin archivo de contraseña, con el repositorio inaccesible o con una contraseña mala, el comando termina con **exit 2** y dice «verificación externa NO realizada» (`performed: false`); el diario lo registra sin secretos y `status` lo muestra como `scrub-failed` **hasta que una verificación EXTERNA exitosa lo despeje** (un scrub solo local no lo despeja: sería el mismo éxito falso, un paso después). Un repositorio dañado sí es una verificación realizada que falló (`performed: true`, `code: damaged`). Sin `--external` el mensaje dice «solo copias locales».
9. **Sin secretos en ninguna salida.** El diario, la salida de la rutina, los errores y el estado pasan por `redact` (valores conocidos, `esquema://usuario:clave@`, `ALGO_KEY=valor`).

## 2. Herramientas evaluadas (verificadas el 2026-11-23)

Criterio del Issue: **reutilizar lo mantenido; no construir un servicio de backup propio** si basta un planificador confiable + una herramienta existente. Lo único propio es el pegamento que ninguna herramienta trae: verificar que la copia **restaura y arranca la aplicación**, y sanear lo que no debe viajar (sesiones, lease, contadores).

| Pieza | Opción | Versión / actividad (GitHub API, 2026-11-23) | Decisión y razón |
|---|---|---|---|
| Copia consistente | API de copia en línea de SQLite (`.backup`) | stdlib | **Reutilizada** (ya en V0.6). `VACUUM INTO` descartado: una interrupción puede dejar una copia incompleta |
| Planificador | **launchd** (macOS), **systemd timer** (Linux), cron | del sistema operativo | **Reutilizados**: se generan los archivos; no se escribe un planificador. APScheduler/Celery beat descartados: viven dentro del proceso de la app (mueren con ella) y añaden dependencias |
| Destino externo cifrado | **restic** | v0.20.0 (2026-11-19), BSD-2, 37 k★, activo | **Adoptado (opcional)**. AES-256 en modo contador + Poly1305-AES, clave derivada con scrypt, deduplicación, `check --read-data`, políticas de retención, backends S3/B2/SFTP/local [(referencia)](https://restic.readthedocs.io/en/latest/100_references.html). Un binario, sin servidor |
| | Litestream | v0.5.18 (2026-11-03), Apache-2.0, 14.7 k★ | **No ahora.** Replica el WAL (recuperación a un punto en el tiempo) pero exige un **proceso residente** y un bucket con credenciales. El RPO objetivo del piloto (24 h) no lo necesita. Su historia reciente: la serie 0.5.0 tuvo fallos graves corregidos en 0.5.1 [(relato de un usuario)](https://mtlynch.io/notes/hold-off-on-litestream-0.5.0/). Candidato si algún día el RPO debe bajar a minutos |
| | kopia | v0.23.1 (2026-06-16), Apache-2.0 | Equivalente a restic (cifrado, dedup, S3/B2). *No se re-verificó en esta sesión*; se descartó por no aportar nada que restic no cubra y por tener menos historial en el repositorio |
| | borg | 1.4.5 (2026-07-18) | Su destino remoto habitual es SSH con `borg` instalado en el otro extremo; *no re-verificado*. Menos directo para un bucket |
| | age / rclone crypt | age v1.3.2 · rclone v1.76.0 (MIT) | `age` cifra archivos pero no trae repositorio, retención ni verificación; `rclone` sincroniza. Habría que construir encima lo que restic ya trae |
| Proveedores | Backblaze B2, Cloudflare R2 | ver §7 | Candidatos **sin contratar** |

## 3. RPO / RTO: metas propuestas (no prometidas) y mediciones

**Metas propuestas para el piloto inicial** (a decidir por el dueño):

| Objetivo | Meta | Cómo se mide / vigila |
|---|---|---|
| **RPO local** (datos que se pierden como máximo si muere la base) | ≤ 24 h → una copia diaria, alarma a las 26 h | `status`: `rpo.current_age_hours` y `rpo.worst_gap_hours_7d` (el **peor hueco real** de la semana, no el objetivo) |
| **RPO fuera del equipo** | ≤ 24 h (la subida va en la misma pasada) | `status --external-required` |
| **RTO** (de «tengo la copia» a «la app responde sobre la copia») | ≤ 30 min | `status`: `drill.measured_rto_seconds` = restaurar + arrancar medido en cada pasada. **No incluye** tu tiempo de detener el servidor, mover archivos y reiniciar (§6) ni la descarga desde el destino externo (medida aparte) |
| Ensayo de restauración | cada pasada; alarma a las 26 h | `status`: `drill.last_ok_age_hours` |

### Mediciones reales (este Mac, 2026-11-23; bases TEMPORALES; restic con repositorio LOCAL, sin red)

Cada fila: una pasada completa (`routine.run`) y una segunda tras escribir 200 filas, con datos **aleatorios incompresibles** (peor caso de compresión y deduplicación). Segundos, salvo indicación.

| Base | Copia en línea | Restaurar (incl. verificar) | Arrancar la app | **RTO local** | `restic backup` 1.º | `restic backup` 2.º | Bajar del destino | Repo tras el 1.º | **Crecimiento del 2.º** | Pasada 1.ª / 2.ª |
|---|---|---|---|---|---|---|---|---|---|---|
| 1,5 MB | 0,007 | 0,005 | 0,018 | **0,022** | 1,48 | 1,48 | 0,73 | 1,07 MB | +1,07 MB (100 %) | 3,0 / 2,2 |
| 105 MB | 0,198 | 0,162 | 0,131 | **0,292** | 1,65 | 1,60 | 0,79 | 105,0 MB | +4,2 MB (**4,0 %**) | 3,9 / 2,8 |
| 525 MB | 1,052 | 0,968 | 0,698 | **1,666** | 2,30 | 2,13 | 1,02 | 525,1 MB | +3,3 MB (**0,64 %**) | 8,3 / 5,2 |

*(Script: `bench_scale.py`; no forma parte del repositorio. Reproducible con `create_app` + `routine.run` + `Restic` sobre una base creada con `os.urandom`.)*

Lo que se desprende, y lo que **no**:

* **La base real de hoy** (`aicos.db` ≈ 0,7 MB) hace una pasada completa por CLI en **≈ 0,3 s** (0,28–0,29 s sin destino externo; 2,5–3,3 s con restic local, casi todo es derivar la clave con scrypt en cada invocación de restic). El **RTO medido** por el ensayo es **≈ 0,22 s**; ese valor incluye ≈ 0,2 s de **importar la aplicación en un proceso nuevo** (en las filas de la tabla el proceso ya está caliente, por eso «arrancar» sale menor).
* **RTO local ≈ 3,3 s por GB** (extrapolación lineal de la fila de 525 MB, **no medida** por encima de eso): ≈ 33 s para una base de 10 GB. Muy por debajo de la meta de 30 min. **No incluye** detener el servidor, mover archivos y reiniciar (minutos, manual) ni la descarga desde un destino real: la tabla mide un repositorio en el mismo disco, **la descarga a través de Internet depende de tu enlace y no se midió**.
* **Deduplicación:** a partir de ~100 MB cada copia diaria agrega < 5 % del tamaño; con una base de 1,5 MB restic sube casi todo cada vez (un archivo tan pequeño cabe en uno o dos fragmentos que cambian). Da igual: a ese tamaño el costo es cero (§7), y el cálculo de costos del peor caso no cuenta con la deduplicación.
* **RPO:** la pasada deja la copia a 0 h de antigüedad y dura < 10 s hasta 525 MB, así que el RPO lo fija la **agenda** (24 h) y que el equipo esté encendido, no la duración de la copia. El **peor hueco real** lo informa `status` (si el Mac está apagado un fin de semana, saldrá ≈ 72 h).



### Deduplicación de restic: el fallo del CI del PR #16, diagnosticado (2026-11-23)

**Qué falló.** La primera versión de la prueba de deduplicación (base de 12 MB + 20 filas nuevas, cota del 25 %) falló en el CI ([run 45112796211](https://github.com/Arcan17/ai-company-os/actions/runs/45112796211)): el *runner* instala **restic 0.18.1** de `apt` (`0.18.1-3ubuntu1`, go1.25.0, linux/amd64); el repositorio pasó de 12 616 237 a 16 075 886 bytes (**+3 459 649 B = +27,4 %**). En el Mac (restic **0.19.1**) había pasado. Dos hipótesis: **(A)** una diferencia de versión; **(B)** la prueba mide la suerte del cortador de restic.

**Mecanismo.** restic parte cada archivo en fragmentos con un cortador de contenido cuyo **polinomio elige al azar cada repositorio nuevo** (se ve con `restic cat config`: `chunker_polynomial`); los fragmentos miden ≈ 1 MiB de media (mínimo 512 KiB, máximo 8 MiB) y **todo fragmento que un cambio toca se vuelve a subir entero**. Con 20 filas nuevas SQLite modifica unas pocas páginas; cuántos fragmentos las contienen —y de qué tamaño— depende del polinomio. En un archivo de 12 MB (≈ 12 fragmentos) tocar 3–4 es un 25–33 %.

**Experimentos** (script `qa/restic_dedup_study.py`; todo temporal, contraseña de ensayo, sin red):

| # | Experimento | Resultado |
|---|---|---|
| 1 | **El mismo escenario de la prueba**, 30 repositorios nuevos, **restic 0.19.1 (Mac)** | crecimiento **0,5 % – 30,6 %**; mediana 6,9 %, media 9,5 %, p90 18,6 %; **1 de 30 supera el 25 %** (30,6 %, polinomio `3f208450dc6d05`). Es decir: la prueba **también falla en el Mac**, solo que menos a menudo |
| 2 | **Mismos datos con semilla**, 40 polinomios distintos, 12 MB, 0.19.1 | **4,2 % – 33,3 %**; mediana 9,0 %; **5 de 40 (12,5 %) superan el 25 %**. Con los datos fijos, lo único que cambia es el polinomio: el crecimiento depende de él, no del azar de los datos ni de la hora |
| 3 | El **peor** polinomio (`2a1ae808395e51`) con esos datos, **12 MB** → **64 MB** (0.19.1) | **+4 203 087 B (33,33 %)** → **+4 204 713 B (6,26 %)**. **Los bytes que crecen son prácticamente los mismos** (el cambio toca los mismos fragmentos); lo que cae es la proporción. En ambos casos 8 bloques nuevos |
| 4 | **El mismo contenido dos veces** (12 MB) | **+462 B (0,0037 %)**: la deduplicación funciona; solo crece una instantánea |
| 5 | **Diseño nuevo** (64 MB, mismo cambio, cota 25 %), 30 repositorios, 0.19.1 | **0,1 % – 4,1 %**; mediana 1,4 %; p90 3,2 %; **0 de 30** superan el 25 % |
| 6 | **Runner de CI, restic 0.18.1**, el mismo escenario (12 MB), 30 repositorios nuevos ([run 45200384342](https://github.com/Arcan17/ai-company-os/actions/runs/45200384342)) | **0,7 % – 25,7 %**; mediana 5,6 %; p90 18,7 %; **1 de 30 supera el 25 %** — igual que el Mac con 0.19.1 (1 de 30) |
| 7 | Runner, **diseño nuevo** (64 MB), 30 repositorios | **0,1 % – 5,3 %**; mediana 1,5 %; p90 3,6 %; **0 de 30** |
| 8 | Runner, **el mismo polinomio y los mismos datos con semilla** que el Mac (experimento 3): 12 MB → 64 MB | **+4 198 167 B (33,31 %) → +4 199 861 B (6,25 %)**. Mac 0.19.1: +4 203 087 B (33,33 %) → +4 204 713 B (6,26 %). Diferencia entre versiones: **≈ 4,9 KB (≈ 0,12 % de lo que crece)** |
| 9 | Runner, **el mismo contenido dos veces** (12 MB) | **+970 B (0,0077 %)** (Mac: +462 B) |

**Conclusión (con la evidencia, no con un umbral más flojo).** **La hipótesis (A) —una diferencia entre restic 0.18.1 y 0.19.1— queda refutada; la (B) —la prueba mide la suerte del polinomio— queda confirmada.** Con el mismo polinomio y los mismos datos las dos versiones suben los mismos bytes (experimentos 3 y 8: ≈ 4,2 MB; los ≈ 4,9 KB de diferencia son del orden de los metadatos: 0.19.1 escribe 8 bloques nuevos y 0.18.1 escribe 4, y la diferencia de bytes crudos es de 4,5 KB, coherente con bloques pequeños de metadatos y no con fragmentos de datos). Con polinomio aleatorio, **ambas versiones** reparten el crecimiento en un rango amplio (0,5–30,6 % en el Mac, 0,7–25,7 % en el runner) y **ambas superan el 25 % en 1 de cada 30 repositorios**: el 27,4 % del CI cae dentro de esa distribución. La deduplicación funciona en las dos (mismo contenido dos veces: +462 B y +970 B). El umbral del 25 % a 12 MB medía cuántos fragmentos de ≈ 1 MiB (hasta 8 MiB) tocaba el cambio, no si restic deduplica.

**Qué se cambió.** La cota **sigue siendo del 25 %**; cambió lo que se mide:
1. `test_pushing_identical_content_again_adds_almost_nothing`: el mismo archivo dos veces crece **< 1 %** — una propiedad de la deduplicación que **no depende de dónde corte restic**.
2. `test_a_small_change_on_a_large_database_stays_cheap`: el mismo cambio sobre **64 MB** con la misma cota del 25 % — con un margen de seis veces sobre el máximo observado (4,1 %), solo se cruza si la deduplicación está rota de verdad.
3. `test_the_incident_reproduces_with_the_unluckiest_polynomial_and_the_new_design_passes_under_it`: el incidente, **determinista**: el repositorio se crea con el peor polinomio (`restic init --copy-chunker-params` desde una plantilla con contraseña de ensayo, `backend/tests/restic_worst_polynomial.json`) y los datos llevan semilla. La rama 1 (diseño viejo, 12 MB) **reproduce el fallo** (> 25 %); la rama 2 (diseño nuevo, 64 MB) **pasa** bajo el mismo polinomio. La rama 1 es una caracterización: si un restic futuro corta distinto y deja de reproducir, se puede retirar; la rama 2 es la prueba de regresión.

**Versiones de restic soportadas y probadas:** **0.18.1** (*runner* de CI, `apt`) y **0.19.1** (Mac). No se probó 0.20.0 (publicada el 2026-11-19). El CI imprime `restic version` en cada ejecución; no se fija una versión descargada porque las pruebas ya no dependen de una versión concreta.


## 4. Cifrado y credenciales del destino externo: quién, dónde, cómo se rota

* **Quién cifra:** restic, en **este** equipo, antes de que nada salga. El proveedor solo ve bloques cifrados (la prueba `test_the_repository_is_encrypted_…` busca en claro `SQLite format 3`, `CREATE TABLE`, un correo y el nombre de las tablas dentro del repositorio: no aparece).
* **La contraseña del repositorio** vive en un archivo `0600` **fuera del repositorio de código** (p. ej. `~/.config/aicos/restic.pw`), o se obtiene con un comando (`AICOS_BACKUP_RESTIC_PASSWORD_COMMAND`, p. ej. el llavero de macOS). Nunca por argumento ni por variable de entorno; el código **se niega** a usar un archivo con permisos abiertos, de otro usuario, vacío o dentro del checkout (a un `git add -A` de publicarse).
* **Las credenciales del proveedor** (`B2_ACCOUNT_ID/KEY`, `AWS_*`, …) van en un archivo de entorno `0600` (`--env-file`) con **lista blanca** de claves: una clave desconocida (`LD_PRELOAD`, etc.) se rechaza **nombrando la clave, nunca el valor**. restic recibe un entorno mínimo: nada más del Mac llega a él.
* **Los archivos de agenda llevan solo rutas**, jamás valores (la prueba comprueba que el plist no contiene `EnvironmentVariables`).
* **Perder la contraseña = perder los datos del destino.** Antes de activar: guardarla en **dos** lugares fuera de este equipo (gestor de contraseñas + papel), y **hacer un ensayo de restauración desde otro directorio con solo esa contraseña**.
* **Rotación:** `python -m app.backup_routine external-rotate-key --env-file F --out D --new-password-file NUEVO` ejecuta `restic key passwd` y **comprueba al instante** que la contraseña nueva abre el repositorio (los datos no se re-cifran: restic envuelve una clave maestra por contraseña). La antigua deja de abrirlo en ese momento (prueba). Después: reemplazar el archivo configurado y destruir el viejo. Rotar la credencial del **proveedor** se hace en su consola y en el archivo de entorno; no toca los datos.
* **Un repositorio nunca se crea solo.** `run --external` contra una ruta mal escrita falla con `repo-missing` y **no crea nada** (prueba). Crear el repositorio es `external-init`, explícito.
* **Dato interno:** las instantáneas llevan `--host aicos` (no el nombre del Mac) y la etiqueta `aicos`; la retención externa agrupa por `host,tags` porque el nombre de archivo cambia en cada copia (agrupar por ruta —el valor por defecto de restic— **no olvidaría nunca nada**; hay una prueba y una mutación que lo cubren).

### Códigos de error estables del destino externo

`config` · `binary-missing` · `timeout` (se mata el **grupo** de procesos, no quedan huérfanos) · `wrong-password` · `repo-missing` · `repo-locked` · `damaged` (falló `check`) · `failed`.

## 5. Agenda: preparada, no instalada

```bash
cd backend
.venv/bin/python -m app.backup_routine schedule --kind launchd \
    --db /ruta/absoluta/a/la/base.db --out /ruta/absoluta/a/copias --dest /ruta/nueva/agenda-preparada
```

Escribe `local.aicos.backup.plist` (cada día 03:30) y `local.aicos.backup-status.plist` (`status` cada 6 h, desde una hora después de la copia: es la alarma si la copia deja de ocurrir) **y solo imprime** los pasos manuales de instalar y de deshacer. El generador: no importa `subprocess` (una prueba lo comprueba), se niega a escribir en `~/Library/LaunchAgents`, `/Library/Launch*`, `~/.config/systemd`, `/etc/systemd`, `/var/spool/cron`…, no pisa un directorio existente y es reproducible (mismas entradas → mismos bytes). También genera `--kind systemd` (timer con `Persistent=true`, `NoNewPrivileges`, `PrivateTmp`) y `--kind cron`.

**El directorio de logs** *(PR #16, hallazgo revisión automatizada P1)*. Una redirección `>> LOG` la abre el shell **antes** de que arranque Python: con un directorio inexistente ni la copia ni la alarma correrían jamás. Por eso cada línea de cron empieza `umask 077 && mkdir -p -- LOGS && cd BACKEND && … >> LOGS/aicos-backup.log 2>&1`: el directorio **y sus intermedios nacen 0700**, el log nace 0600, un directorio existente no se toca y, si no puede crearse, falla a la vista (cron lo envía por correo) y no ejecuta nada a medias. El directorio por defecto es `~/.local/state/aicos` para cron y systemd (`~/Library/Logs` solo existe en macOS: es el de launchd). **launchd** abre el log antes de ejecutar nada y no admite un `mkdir` en el plist, así que el generador **imprime** `(umask 077; mkdir -p -m 700 LOGS)` como primer paso de instalación (antes de `launchctl bootstrap`); el generador sigue sin escribir fuera de `--dest`. La prueba **ejecuta de verdad** las dos líneas de cron con `/bin/sh` y un directorio inicialmente ausente.

**Límites reales de una agenda en un Mac (sin verificar en este equipo porque no se instaló nada):**
* launchd ejecuta una tarea perdida **si el Mac estaba dormido**; si estaba **apagado**, no la recupera. La documentación de Apple y los foros no garantizan el comportamiento con la tapa cerrada [(hilo del foro de desarrolladores de Apple)](https://developer.apple.com/forums/thread/815034). Por eso existe `status`: **el RPO real de un portátil lo decide cuánto está encendido**.
* macOS puede pedir permiso a un trabajo en segundo plano para leer `~/Documents` (Privacidad y seguridad). **Sin probar**: se sabrá al instalar.
* Un Mac no es un servidor. Para un piloto con clientes, la agenda pertenece a un equipo que esté siempre encendido (el timer de systemd con `Persistent=true` ya está generado).

## 6. Procedimiento de recuperación (resumen; el detalle manual sigue en `BACKUP_RESTORE.md`)

1. Detener el servidor. 2. `restic restore <id|latest> --target DIR_NUEVO` (con la contraseña) **o** tomar la última copia local. 3. `python -m app.backup verify <archivo>` (el manifiesto viaja con la copia). 4. `python -m app.backup restore <archivo> --to NUEVA_RUTA` (sesiones revocadas, lease limpiado, `rate_limits` vacío). 5. Mover la base dañada sin borrarla y poner la restaurada. 6. Arrancar y comprobar. **Todo el recorrido en un equipo nuevo, con solo la contraseña, está cubierto por `test_what_was_pushed_comes_back_and_verifies_and_boots`.**

## 7. Costos estimados (USD; precios oficiales consultados el 2026-11-23, tarifas de lista de EE. UU.)

**Hoy: US$ 0** (nada contratado). Para cuando se autorice un destino externo:

| Proveedor | Almacenamiento | Salida de datos | Operaciones | Fuente |
|---|---|---|---|---|
| **Backblaze B2** | US$ 6,95 / TB-mes (facturado por byte-hora); **primeros 10 GB gratis** | gratis hasta 3 × lo almacenado; luego US$ 0,01 / GB | Clases A, B y C **gratis** | [precios B2](https://www.backblaze.com/pricing.md) |
| **Cloudflare R2** (Standard) | US$ 0,015 / GB-mes; **10 GB-mes gratis** | **gratis** | A: US$ 4,50 / millón (1 M gratis) · B: US$ 0,36 / millón (10 M gratis) | [precios R2](https://developers.cloudflare.com/r2/pricing/index.md) |

Otros proveedores (AWS S3 en São Paulo, etc.) **no se consultaron**: no se estiman. Tampoco se convierte a pesos (no se verificó el tipo de cambio).

**Cálculo del peor caso** (sin ninguna deduplicación): con la política por defecto (`7` diarias + `4` semanales + `12` mensuales + la más nueva) hay **como máximo 24 instantáneas**; almacenado ≤ 24 × tamaño de la base. La deduplicación medida (§3) lo reduce muchísimo, pero el cálculo no la usa.

| Tamaño de la base | Almacenado máx. (24 copias) | B2 / mes | R2 / mes |
|---|---|---|---|
| 1 MB (hoy: `aicos.db` ≈ 0,7 MB) | 24 MB | **US$ 0** (bajo los 10 GB gratis) | **US$ 0** |
| 100 MB | 2,4 GB | **US$ 0** | **US$ 0** |
| 1 GB | 24 GB | ≈ US$ 0,10 | ≈ US$ 0,21 |
| 10 GB | 240 GB | ≈ US$ 1,60 | ≈ US$ 3,45 |

*(B2: (GB − 10) × 0,00695; R2: (GB − 10) × 0,015; operaciones y salida dentro de lo gratis: una pasada diaria son decenas de peticiones, y restaurar una base entera cabe en el tope de salida gratuita de B2 y es gratis en R2.)*

**Lo que no está en esa tabla:** el equipo donde corre la agenda (un Mac encendido las 24 h, o un servidor, cuyo precio no se consultó), revisión legal de dónde residen los datos, y tu tiempo. **Disco local:** la retención local por defecto (3 últimas ∪ la más nueva de 14 días ∪ la última buena) deja como máximo ≈ 17 copias.




## 8. Decisiones que necesito del dueño antes de activar nada

| # | Decisión | Sin ella |
|---|---|---|
| 1 | ¿Dónde viven las copias externas? (B2 / R2 / un disco o equipo propio por SFTP / ninguno todavía). Crear cuenta y tarjeta es **tuyo**; no se hace sin autorización | no hay copia fuera del equipo |
| 2 | **Contraseña del repositorio** en dos lugares fuera del Mac, y ensayo de restauración con solo ella | un destino sin contraseña recuperable es peor que no tenerlo |
| 3 | ¿Dónde corre la agenda? Mac (se pierde si está apagado) o equipo siempre encendido. Autorizar **instalar** los archivos preparados | no hay copias automáticas |
| 4 | **Canal de alerta**: hoy `status` solo devuelve un código y escribe a un log; no avisa a nadie | una copia que deja de ocurrir se descubre tarde |
| 5 | Retención y derechos de los titulares (Ley 21.719): borrar a una persona de la base **no** la borra de copias existentes | riesgo legal con datos reales |
| 6 | Región del proveedor / transferencia internacional de datos (a revisar con abogado; **no verificado** aquí) | riesgo legal con datos reales |

## 9. Limitaciones y riesgos pendientes (sin esconder nada)

1. **Nada instalado ni conectado**: las copias automáticas y las externas **no existen todavía** (§ estado).
2. **Sin alerta activa** (decisión 4). El `status` periódico existe; el aviso, no.
3. **Un portátil no es un servidor** (§5): el RPO real lo fija el tiempo encendido. `launchd` y los permisos de `~/Documents` para un trabajo en segundo plano **no se probaron** (no se instaló nada).
4. **Una credencial con permiso de borrar puede borrar las copias externas** (ransomware o un error). Mitigación recomendada, **no probada aquí**: una clave de aplicación del proveedor limitada al *bucket* y, si el proveedor lo ofrece, bloqueo de objetos/versionado. Se evalúa al elegir proveedor.
5. **restic probado en dos versiones**: **0.19.1** (este Mac) y **0.18.1** (`0.18.1-3ubuntu1` de `apt` en el *runner* de CI: 374 pruebas, 1 omitida —`plutil`, solo macOS—, en verde); **no** en 0.20.0 (publicada hace 4 días) ni contra un proveedor real (la red, los límites de tasa y los cortes de un proveedor no se ejercitaron; solo un repositorio local y un `restic` falso para los fallos).
6. **Las copias locales no están cifradas en reposo** (solo `0600`/`0700` y el cifrado de disco del equipo). El cifrado de restic protege la copia **externa**. Contienen hashes de contraseña y de sesión: tratarlas como datos sensibles.
7. **RPO de 24 h, sin recuperación a un punto en el tiempo** (Litestream lo daría; no se adopta, §2).
8. **El ensayo prueba restaurar y arrancar en este mismo equipo.** Un desastre real implica otro equipo: la descarga desde el destino externo y el arranque ahí solo se probaron en el mismo Mac (con un repositorio local), no en hardware nuevo.
9. **Las mediciones de tamaño usan datos sintéticos aleatorios** (peor caso de compresión y deduplicación); la base real hoy mide ≈ 0,7 MB, así que lo medido a 100–500 MB es una extrapolación honesta, no la realidad del piloto.
10. **No hay *endpoint*** de estado: se evitó a propósito exponer detalles internos de las copias por HTTP; el informe es la línea de comandos. Mostrarlo en el panel del dueño requiere diseñar su autorización (pendiente).
11. **La retención del diario/restos usa el reloj del equipo**: un reloj que salta hacia atrás no puede hacer que una copia nueva parezca más vieja (orden monótono, #11), pero sí puede retrasar la limpieza de restos. Sin consecuencias de seguridad.
12. Los registros de `launchd` (`~/Library/Logs/aicos-backup*.log`) crecen sin rotación (≈ 0,3 KB por día: irrelevante, pero sin límite).



## 10. Evidencia (Mac del dueño, 2026-11-23; todo sobre bases y repositorios TEMPORALES)

| Etapa | Resultado |
|---|---|
| CI del `main` nuevo (`8d0c020`), run [45108463359](https://github.com/Arcan17/ai-company-os/actions/runs/45108463359) | **success**: 293 pytest, Playwright 12 + 4 |
| Pruebas rojas antes de implementar (`902b77f`) | **55 fallan + 7 errores** (62 pruebas). **4 son fallos reales de `app.backup` en `main`**, no de código nuevo: la retención **borraba la única copia buena** (con las N más nuevas dañadas) y también cuando **ninguna** verificaba; un destino sin permiso terminaba en *traceback*; «disco lleno» al publicar escapaba como `OSError` sin atrapar. El resto exige módulos que aún no existían |
| `pytest backend/tests` sobre `24357ae` (árbol limpio, `AICOS_REQUIRE_RESTIC=1`) | **356 passed** en 180,6 s = 293 previas + **63 nuevas** (`safety` 7 · `routine` 28 · `external` 14 · `schedule` 14) |
| Build del frontend | OK |
| Playwright de regresión / demo comercial | **12 / 12** (83 s) · **4 / 4** (10,7 s) |
| Mutaciones (se rompe UNA propiedad cada vez) | **32 / 32 detectadas.** La primera corrida detectó 30 de 31: **la S2 (cron con `%` sin escapar) NO fue detectada** — la prueba de rutas raras parseaba la línea deshaciendo el escape, así que no le importaba. Se reforzó (cada `%` debe ir escapado) y ahora cae. Después, al añadir la limpieza de ensayos muertos, se añadió la R13 (también detectada) |
| GitHub Actions del PR | ver el comentario/estado del PR: **no se afirma verde aquí** |

**Pruebas que ejercen fallos reales, no simulaciones dentro del código:**
`SIGKILL` en dos puntos de la creación (copia a medias · archivo de datos publicado sin manifiesto) → no queda nada listable ni restaurable, el cerrojo lo libera el kernel y la siguiente pasada se recupera · **límite de tamaño de archivo del kernel** (`RLIMIT_FSIZE`) en mitad de la copia → `FALLO`, exit 2, ninguna copia nueva ni borrada · 5 procesos simultáneos con una puerta de salida común → ninguna pasada se solapa con otra (los intervalos del diario son disjuntos) · un `restic` falso que cuelga con un hijo → se mata el grupo completo · un `restic` falso que imprime credenciales → no llegan al diario, a la salida ni al estado · un destino externo caído → aviso, copia local intacta, retención remota no ejecutada.

**Ensayo contra un servidor real** (`uvicorn --no-proxy-headers` en un puerto libre, base temporal, lease activo, escrituras HTTP reales mientras corre la rutina; restic con repositorio local):
* `run` con el servidor escribiendo: exit 0, **0,29 s**; restaurar 0,003 s + arrancar 0,22 s; segunda pasada igual. `status`: **OK**, RPO 0 h, peor hueco 7 d 0 h, **RTO medido 0,22 s**, sesiones revocadas = sí, instancia activa tras restaurar = sí.
* `external-init` + `run --external` (1.ª con restauración externa: 3,26 s; 2.ª sin repetirla: 2,49 s): exit 0. El repositorio (10 archivos, 26 790 bytes) **no contiene en claro** `SQLite format 3`, `dueno@climasur.test` ni `CREATE TABLE`. `scrub --external` OK; `status --external-required` OK.
* Con la copia **más nueva dañada**: `status` → **AVISO** `newest-corrupt` (exit 1); `app.backup prune --keep 1` **conserva la última buena** (quedan 2 copias); la siguiente `run` sana el estado a OK.
* `aicos.db` y `demo_comercial.sqlite3`: **sin cambios** (mtime y tamaño idénticos antes y después); los puertos 8731/8743/8750/8000 **siguen escuchando** y no se reiniciaron; ningún `launchd`/`cron` creado (el generador no puede: no importa `subprocess`).
