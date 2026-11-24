# Runbook — demo comercial V0.4 (para presentar desde el Mac)

Todo en **127.0.0.1**, datos **100 % ficticios**, motor **determinista** (sin LLM, sin red). Independiente del laboratorio `:8731`. Guion de referencia: `DEMO_COMERCIAL_V04.md` · qué decir/no decir: [`REAL_VS_SIMULADO.md`](REAL_VS_SIMULADO.md).

## 1. Comandos (desde la raíz del repo)
```bash
./run_demo_comercial.sh                # = start: prepara la BD ficticia (idempotente), arranca en un puerto libre, muestra BD/puerto/modo y corre el preflight
./run_demo_comercial.sh status         # qué corre (demo, plan B y laboratorio) — no toca nada
./run_demo_comercial.sh preflight      # comprobaciones de solo lectura antes de presentar
./run_demo_comercial.sh reset          # RESTABLECE SOLO la BD de la demo (pide escribir REINICIAR-DEMO; copia previa en backend/data/backups/)
./run_demo_comercial.sh planb          # plan B: BD con el guion YA ejecutado de verdad (API real) en otro puerto (8744)
./run_demo_comercial.sh stop           # detiene SOLO lo que este script arrancó
./run_demo_comercial.sh test           # pytest de la demo + Playwright del guion (sobre copias temporales)
```
Variables: `DEMO_PORT` (primer puerto a probar; por defecto 8743) · `AICOS_LLM_PROVIDER=ollama` (variante opcional con LLM local; **no** usar en la demo principal).

**Garantías**: nunca abre `backend/data/aicos.db`; el código se niega a arrancar la demo sobre una BD que no se llame `demo_comercial*`; nunca usa los puertos 8731/8000/8741/8742/8745 ni mata un proceso que no haya arrancado (pidfile + comprobación de comando y carpeta); solo loopback.

## 2. Direcciones locales (puerto por defecto 8743; el script imprime el real)
| Qué | URL |
|---|---|
| Guía, estado y accesos | `http://127.0.0.1:8743/demo` |
| Dueña (Carolina Vega) | `http://dueno.localhost:8743/demo-login/owner` |
| Administración (Javiera Soto) | `http://admin.localhost:8743/demo-login/admin` |
| Técnico (Matías Fuentes) | `http://tecnico.localhost:8743/demo-login/tech` |
| Plan B (ejecución ya terminada) | `http://dueno.localhost:8744/demo-login/owner?next=/coordinator` |

**Tres sesiones independientes en un solo navegador**: las cookies se separan por *nombre de host*, no por puerto; por eso cada persona usa un host distinto (`dueno.`, `admin.`, `tecnico.localhost`, que macOS resuelve a 127.0.0.1). Cada enlace inicia sesión con la cuenta ficticia correspondiente (solo existe en modo demo y solo desde loopback). Contraseña de las cuentas: `demo1234`.

## 3. Checklist antes de presentar (≈5 min)
1. `./run_demo_comercial.sh status` → el laboratorio :8731 sigue como estaba; la demo aparece detenida o corriendo.
2. Si la demo ya se usó: `./run_demo_comercial.sh reset` (escribe `REINICIAR-DEMO`). Luego `./run_demo_comercial.sh start`.
3. El preflight debe terminar en **«PREFLIGHT OK»** (una sola mantención abierta de Marta, stock coherente, 0 planes/tareas/fallos, 0 mensajes enviados, visita de Pedro «propuesta»).
4. Abre `/demo` y, desde ahí, las **tres ventanas** (una por persona). Disponlas lado a lado o en escritorios distintos.
5. Cierra notificaciones. **No uses el «Chat de cliente»** durante el guion (crearía otra solicitud de Marta).
6. Ten abierta la pestaña de Plan B (`./run_demo_comercial.sh planb`) por si hay que mostrar una ejecución ya terminada.

## 4. Guion de 10 minutos con lo que **de verdad** funciona
| Min | Persona · pantalla | Qué hacer | Qué se debe ver |
|---|---|---|---|
| 0:00 | Dueña · Dashboard | Mostrar el banner «DEMO COMERCIAL», KPIs, agentes «Disponible» | 4 solicitudes (2 activas · 2 históricas), 0 aprobaciones, 0 tareas, sin alertas |
| 1:00 | Dueña · Clientes y operaciones | Abrir **Marta Rojas** y **Pedro Gálvez** | Mensaje del cliente, presupuesto con repuesto real; visita «Propuesta · sin confirmar» y mensaje «preparado · no enviado (simulado)» |
| 2:00 | Dueña · Coordinador | Escribir: `Revisa la mantención pendiente de Marta Rojas; pide a administración que verifique materiales y al técnico que confirme después de administración.` → *Proponer plan* | Plan de 4 pasos con Javiera y Matías; «todavía no se creó ninguna tarea» |
| 3:30 | Dueña · Coordinador | Revisar pasos/permisos y pulsar **Confirmar plan** | «Esperando a: Javiera Soto (administración)… Después: Matías Fuentes» |
| 4:30 | Administración · Mi asistente | Preguntar `¿Cuánto stock hay de KIT-MANT?` (responde 6 u.) y completar la tarea con evidencia (≥10 caracteres) | «Tarea completada con tu evidencia» |
| 6:00 | Técnico · Mi asistente | Antes: «en espera de Javiera Soto». Ahora: su tarea aparece; completarla con evidencia | «Tarea completada con tu evidencia» |
| 7:15 | Dueña · Coordinador → visualizador | Plan «Completada» con resumen y evidencias; pulsar *Ver en el visualizador* | 37 eventos persistidos, nodos en verde, «SSE conectado», inspector con `plans.assign_task`, evidencia por persona |
| 8:30 | Dueña · Coordinador | Escribir `Compra repuestos y avisa a todos por WhatsApp.` | Plan **Bloqueado**: «NO se ejecutará», sin pasos, sin botón de confirmar |
| 9:15 | — | Preguntas de descubrimiento (ver `docs/oferta/PILOTO_A_MEDIDA.md`) | — |

> Evidencia mínima de 10 caracteres: una frase como «Revisé el inventario: hay 6 kits (necesito 1) y las herramientas están completas».

## 5. Si algo falla durante la presentación
- **Plan B (real, no pregrabado)**: `./run_demo_comercial.sh planb` crea una BD aparte y ejecuta el guion completo contra la **API real** (3 usuarios), y la sirve en el puerto 8744. Abre el plan «Completada» y el visualizador: es una ejecución real y persistida de antes de la sesión. Explica que es la ejecución previa; **no** la presentes como en vivo.
- **Plan C (capturas)**: capturas reales del flujo completo en `docs/demo/screenshots/` (Chromium, 00–13) y capturas históricas de Safari del 22-nov (`safari-01…13`, solo renderizado). No hay vídeo grabado.
- **No** actives envíos, **no** borres datos para disimular una incidencia y **no** uses la base del laboratorio. Si el servidor se cayó: `./run_demo_comercial.sh start` (el plan pendiente se conserva; el reinicio está probado).
- Cookies cruzadas o login raro: abre cada persona con su enlace `*.localhost` (no con `127.0.0.1`), o usa una ventana privada.

## 6. Qué explicar siempre
Simulado: WhatsApp, correo, teléfono, pagos, calendarios (no existen). Roles lógicos, no agentes autónomos. Datos ficticios. Prototipo local sin usuarios reales. Detalle y respuestas a preguntas difíciles: [`REAL_VS_SIMULADO.md`](REAL_VS_SIMULADO.md).

## 7. Qué se verificó y cómo repetirlo
`./run_demo_comercial.sh test` corre `backend/tests/test_demo.py` y `frontend/e2e/demo_commercial.spec.ts` (guion de 10 min con 3 contextos/hosts en **Chromium headless aislado**, comparación UI ⇄ SQLite ⇄ eventos, prohibidos, permisos, aislamiento, reinicio, recrear fixture). **No toca Safari**.

**Safari = verificación MANUAL** (Issue #4). Ninguna herramienta de este repositorio crea, mueve, redimensiona, enfoca ni cierra ventanas de tu Safari ni lo automatiza (hay un test que lo impide: `backend/tests/test_qa_safety.py`). `python3 qa/safari_manual_checklist.py [puerto]` solo comprueba por HTTP que el servidor está listo y manda las cabeceras que Safari necesita, e imprime la checklist para que **tú** la hagas en una ventana que abras tú. Los lanzadores tampoco abren navegadores (`run_demo.sh` solo con `AICOS_OPEN_BROWSER=1`).

> Histórico: el 22-nov una versión anterior de `qa/safari_check.py` cerró ventanas personales de Safari (Issue #4). Ese script **fue retirado**. Sus «13/13» (capturas `docs/demo/screenshots/safari-*.png`, `docs/demo/safari-results.json`) validaban **renderizado, cookies por host y SSE**, **no** interacción dentro de Safari ni la seguridad del cierre de ventanas.

## 8. Problemas frecuentes
| Síntoma | Causa / solución |
|---|---|
| `DEMO_PORT=… está ocupado` | Otro proceso usa ese puerto; el script **no lo toca**. Quita `DEMO_PORT` y elegirá uno libre |
| El preflight dice «la demo ya se usó» (WARN) | Hay planes/tareas de una sesión anterior → `reset` |
| «No encuentro una solicitud abierta de Marta» | Se cerró/alteró la mantención; `reset` |
| Pide aclaración entre varias solicitudes | Se envió algo desde el «Chat de cliente»; `reset` |
| Pantalla en blanco al abrir `127.0.0.1` | Falta compilar el frontend: el script lo hace en el primer `start` (`npm ci && npm run build`) |
