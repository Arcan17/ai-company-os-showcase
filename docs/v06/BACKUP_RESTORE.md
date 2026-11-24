# V0.6 · Copias de seguridad restaurables (SQLite)

**Fecha:** 2026-11-22 · **Rama:** `feat/v06-backup-restore` (apilada sobre el PR #9) · **Issue:** #8 · **Gate M1:** «restauración comprobada».
Módulo: `backend/app/backup.py` (solo biblioteca estándar; sin dependencias nuevas). Pruebas: `backend/tests/test_v06_backup.py` (19: 10 originales + 9 de la revisión).

> **Issue #15 (rutina, agenda y destino externo cifrado):** lo que se agenda y lo que sale de la máquina está en [`BACKUP_RUTINA_Y_DESTINO_EXTERNO.md`](BACKUP_RUTINA_Y_DESTINO_EXTERNO.md). Cambios en esta herramienta: la retención **conserva siempre la copia más nueva que verifica** y no borra nada si ninguna verifica (`prune`/`--keep` + `--keep-daily`); un fallo de disco, permiso o espacio es `FALLO` con exit 2, no un *traceback*; `cleanup_orphans` limpia restos de un cierre brusco solo cuando son viejos.

## 1. Qué hace

| Comando | Qué garantiza |
|---|---|
| `create --db P --out D [--keep N]` | Instantánea **consistente de una base en uso** (API de copia en línea de SQLite, origen abierto `mode=ro`), convertida en **un solo archivo** (sin `-wal`/`-shm`), con `integrity_check`, conteo de filas por tabla y **sha256** en un manifiesto `.manifest.json`. Archivos `0600` **desde su creación**; directorio `0700` si lo crea la herramienta (uno existente no se modifica: solo se avisa). |
| `verify F` | Tamaño + sha256 + `integrity_check` + filas = manifiesto. Detecta truncado, un byte cambiado, manifiesto mentiroso o ausente. |
| `restore F --to NUEVA_RUTA` | Verifica primero; escribe **solo en una ruta que no existe** (creación atómica); revoca sesiones y contadores de login de la copia (`--keep-sessions` lo evita). Un fallo no deja archivo parcial. |
| `drill --db P --work D` | **Ensayo completo**: crea → verifica → restaura en otro lugar → **arranca la aplicación real sobre la copia** (migraciones + lectura) → borra la copia temporal → `PASS`/`FAIL` (exit 0/2). |
| `list` / `prune --keep N` | Lista; poda dejando las N más nuevas. Solo toca archivos con **nuestro nombre y nuestro manifiesto**; nunca deja 0 copias. |

`--db` es **siempre explícito** (no hay base por defecto): un error de tipeo no puede respaldar ni restaurar sobre la base equivocada. Un origen inexistente se rechaza (`sqlite3.connect` habría creado una base vacía).

```bash
cd backend
.venv/bin/python -m app.backup drill  --db data/OTRA.sqlite3 --work backups/ensayo      # prueba de restauración (lo que se agenda)
.venv/bin/python -m app.backup create --db data/OTRA.sqlite3 --out backups --keep 14
.venv/bin/python -m app.backup verify backups/aicos-<UTC>-<sha8>.sqlite3
```

## 2. Procedimiento de restauración real (manual, a propósito)

1. **Detener** el servidor que usa la base dañada (`kill` del proceso; en el laboratorio, `./run_v05_lab.sh stop`).
2. `python -m app.backup restore <copia> --to data/aicos.restaurada.sqlite3` → se verifica y se escribe en una ruta nueva.
3. Mover la base dañada **sin borrarla** (`mv data/aicos.db data/aicos.db.danada-<fecha>` y sus `-wal`/`-shm`) y poner la restaurada en su lugar.
4. Arrancar. La aplicación aplica sus migraciones aditivas al abrir; las **sesiones quedaron revocadas**: todos deben volver a iniciar sesión. El **lease de instancia** (`instance_lease`) tampoco viaja: `restore` lo vacía siempre, así la instancia restaurada se declara activa y recupera, en lugar de quedar en espera detrás del dueño que figuraba en la copia. Lo mismo ocurre con los **contadores de límite del canal público** (`rate_limits`) y su clave de hash (`rate_limit_meta`, #13): estado de ejecución, no datos; el siguiente arranque crea una clave nueva.

**Por qué `restore` nunca pisa un archivo existente:** SQLite no puede saber si un servidor ocioso aún tiene abierto el archivo viejo; renombrar un archivo bajo un proceso en marcha hace que este siga escribiendo en el archivo huérfano. Detener y cambiar es un acto humano explícito.

## 3. Herramientas evaluadas (datos verificados el 2026-11-22)

| Opción | Hallazgo | Decisión |
|---|---|---|
| `sqlite3.Connection.backup` / `.backup` (API de copia en línea) | Es el mecanismo oficial; copia página a página y el resultado es válido aunque haya escrituras durante la copia; en modo WAL **copiar solo el archivo principal es inseguro** | **Reutilizado** (ya lo usa `db._backup_before_migration`) |
| `VACUUM INTO` | Documentado como alternativa, pero una interrupción puede dejar una copia incompleta y gasta más CPU | Descartado |
| **Litestream** v0.5.18 (Apache-2.0, release 2026-11-03, repo activo) | Replicación continua a almacenamiento tipo S3 → recuperación a un punto en el tiempo | **Sigue siendo candidato para V0.7** (necesita un proceso residente y un bucket con credenciales: decisión y coste del dueño). Ver la comparación completa en el documento del Issue #15 |
| `restic` v0.20.0 (BSD-2, 2026-11-19) | Copias cifradas (AES-256 + Poly1305-AES) y deduplicadas fuera de la máquina, `check --read-data`, política de retención | **Adoptado como destino externo OPCIONAL (Issue #15)**: integrado y probado contra un repositorio local temporal; **no conectado a ninguna cuenta** |
| `pg_dump`/pgBackRest | Solo aplica si se migra a PostgreSQL | Fuera de alcance hasta decidirlo (roadmap M2) |

## 4. Lo que NO cubre (pendiente, no oculto)

1. **Sin copia fuera de la máquina ni cifrado en reposo *activados*.** Una copia local protege contra corrupción y errores humanos, **no** contra pérdida del disco o del Mac. Una copia contiene hashes de contraseña y de sesión: tratarla como dato sensible (por eso `0600`/`0700` y `backend/backups/` en `.gitignore`). La opción cifrada externa (restic) ya está **construida y probada en local** (Issue #15), pero sin cuenta, contraseña ni destino: activarla requiere autorización del dueño.
2. **Sin agenda instalada.** No se creó ningún `launchd`/`cron`. La agenda está **preparada** (`python -m app.backup_routine schedule`: genera los archivos sin instalarlos y muestra cómo instalar y deshacer).
3. **Sin recuperación a un punto en el tiempo**: se restaura al instante de la copia (RPO = la antigüedad de la última copia). Litestream lo resolvería (V0.7).
4. **Derechos de los titulares / retención** (Ley 21.719, aún sin vigencia confirmada): borrar a una persona de la base no la borra de las copias existentes; la política de retención (`--keep`) y la eliminación en copias deben definirse antes de datos reales.
5. Esta herramienta **no se ejecutó contra las bases reales** (`aicos.db`, `demo_comercial.sqlite3`): el ensayo se hizo sobre **copias** en un directorio temporal.

## 5. Correcciones de la revisión del PR #11 (cada fallo reproducido con una prueba en rojo antes de corregir)

| Hallazgo | Causa | Corrección | Prueba |
|---|---|---|---|
| Archivo temporal de `restore` legible por otros usuarios (umask 022) | sqlite creaba `.restore-*.tmp` 0644 y el `chmod 0600` llegaba **después** de copiar hashes de contraseña y de sesión | El archivo nace **vacío y 0600** (`O_EXCL`) antes de que SQLite escriba; el trabajo ocurre en un directorio temporal 0700 junto al destino; `create` ya no hace `chmod` a un directorio existente (solo avisa) y el manifiesto también nace 0600 | `test_restore_temp_file_is_born_private…`, `test_create_artifacts_are_private_from_birth…` |
| `create(keep=1)` podía borrar la copia recién creada | `created_at` con precisión de segundos → empate → orden de directorio arbitrario | `created_ns` **monótono**: estrictamente mayor que toda copia ya presente aunque el reloj no avance o retroceda; nombre con microsegundos; `prune` ordena por ese valor y **nunca** borra la copia que acaba de crear | `test_snapshots_in_the_same_instant…` (8 vueltas), `test_a_clock_that_steps_backwards…` |
| Sin enlaces duros (EOPNOTSUPP/EXDEV), `os.rename` **sobrescribía** un destino creado por otro proceso | el *fallback* de `restore` | Publicación exclusiva: enlace duro → `renamex_np(RENAME_EXCL)` (macOS) / `renameat2(RENAME_NOREPLACE)` (Linux) → si no hay ninguna, **falla** con error claro. Nunca hay `rename` que reemplace | `test_restore_never_overwrites_when_hard_links…`, `test_restore_fails_safe…`, `test_without_hard_links_an_atomic_noreplace…`, y **6 procesos reales** compitiendo por el mismo destino (con y sin enlaces duros): exactamente 1 gana (exit 0), 5 reciben exit 2 |
| Integración con #12: la copia restaurada heredaba el lease del servidor de origen | una copia de un servidor vivo trae dueño vivo + latido fresco; la app restaurada arrancaba en **espera** y `boot_check` no lo veía (y dejaba su hilo `lease-keeper` latiendo sobre una base borrada) | `restore` vacía siempre `instance_lease` (también con `--keep-sessions`); `boot_check` exige instancia activa, informa `active`/`sessions` y detiene el keeper | `test_a_restored_copy_never_carries_the_source_servers_lease`, `test_the_drill_boots_the_restored_copy_as_the_ACTIVE_instance…`, `test_boot_check_leaves_no_background_thread…` (en `test_v06_integration.py`) |

## 6. Evidencia

- `pytest backend/tests/test_v06_backup.py`: **19 passed** (los 10 originales: instantánea consistente con un escritor concurrente, 4 tipos de daño detectados, origen corrupto/inexistente, restauración sin sobrescritura ni archivo parcial, arranque real sobre la copia, esquema anterior migrado al arrancar, poda segura, códigos de salida del CLI; más los 9 de la revisión: permisos desde el nacimiento, orden con reloj congelado/retrocedido, publicación sin sobrescritura y 6 procesos reales compitiendo).
- Ensayo `drill` sobre copias de `aicos.db` y `demo_comercial.sqlite3`: **PASS** ambos (6 usuarios, 2 empresas); `restore` repetido sobre la misma ruta → exit 2.
- Árbol combinado (#10 + #11 + #12), 22-nov-2026: `drill` por CLI sobre una base **temporal** abierta por un servidor real con lease vigente → **PASS**, arranque `{'sessions': 0, 'active': True}`; el lease del origen no se tocó. Con el código anterior la copia restaurada heredaba ese lease y arrancaba en espera (3 pruebas en rojo en `test_v06_integration.py`, ahora en verde). `pytest backend/tests` → 244 passed.
