# Primera impresión de la demo (spec 006)

Al recorrer la demo con una base recién abierta, la dueña veía «Nada requiere tu decisión hoy» y «No hay trabajos en curso»: un producto aparentemente vacío, cuyo valor solo aparecía si el visitante adivinaba que debía ir a «Probar como cliente». Esta entrega cambia la **primera impresión**, no el producto.

## Qué se agregó
1. **Datos de ejemplo** (`AICOS_DEMO_EXAMPLE=1`; activado en `run_demo.sh`, `compose.yaml` y la unidad de demo del HP; apagado por defecto). Una base sin uso abre con una solicitud a medio camino y una decisión pendiente (Marta Rojas, $63.000). Se crean **recorriendo el escenario A por el chat público real** con el motor determinista —no con filas insertadas a mano—, así que heredan reglas, eventos y auditoría. La demo pública siembra la plantilla una vez y cada visitante recibe su copia aislada.
2. **Recorrido guiado de 4 pasos** («Ver cómo funciona»): ver la decisión, aprobarla, ver la trazabilidad, probar una orden prohibida. Cada paso se da por cumplido **leyendo el estado real**, nunca por un clic del recorrido; solo señala (un borde) y explica, y nunca aprueba ni escribe por la persona. Avanza solo, omite pasos ya cumplidos, se puede cerrar y retomar, y funciona en modo Simple y Completo, también en celular.
3. **Tarjeta de resultados** (`GET /api/results`, permiso `company.summary`): solicitudes atendidas, decisiones tomadas y tiempo por solicitud (mediana) de los **últimos 30 días**, calculados con registros que el sistema ya guarda, filtrados por empresa. Sin solicitudes cerradas el tiempo dice «aún sin datos» (`null`, nunca 0).

## Qué NO cambia
El motor, los permisos, las reglas de aprobación y los `data-testid` existentes. Con el ejemplo apagado, `/api/demo/info` responde exactamente lo de siempre. El ejemplo **nunca** se crea en piloto, con un modelo de IA, en una base que ya se usó ni en una empresa dada de alta por el operador.

## Cómo se comprobó
`backend/tests/test_v11_demo_example.py`, `test_v11_demo_info.py` y `test_v11_results.py` (las cifras se comparan con un conteo independiente), 16 mutaciones en `qa/mutations_v11.py` (todas detectadas), la auditoría estática de aislamiento por empresa y las pruebas de navegador de `frontend/e2e_first` (recorrido completo Simple→Completo, «ya aprobé antes de empezar», otra persona no recibe el recorrido, celular de 375 px sin desplazamiento horizontal).

## Límites honestos
- El «tiempo por solicitud» de una demo recién abierta no tiene datos: hace falta completar un flujo (solicitud → aprobación → cierre) para que aparezca. No se inventa uno.
- Las cifras de una demo pública son de **su** espacio, no de ningún cliente real: son datos ficticios.
- El resaltado del recorrido es un borde sobre un elemento; si una pantalla cambia sus identificadores de prueba, hay que actualizarlo.
- Un hallazgo del camino: la regla de estilo `.glass { position: relative }` anulaba la posición fija del recorrido desde el rediseño; ahora se fija en línea.

## Demo local con IA gratis (`run_demo_ia.sh`)
Para mostrar «IA de verdad» sin gastar: `./run_demo_ia.sh` arranca la demo local con el planificador del coordinador usando un modelo de **Ollama** (por defecto `qwen3.5:9b` en el propio Mac, ~4 s por plan). Comprueba antes que Ollama responde y que el modelo está instalado, y si no, dice qué hacer. Mientras el modelo responde, la pantalla del Coordinador muestra **«Pensando…»** con un contador de segundos (solo si el planificador es un modelo; con el motor de reglas es instantáneo). La IA **solo propone**: el plan sale rotulado «Propuesto por IA (modelo X) · validado por el motor», lo prohibido se bloquea igual y una persona aprueba.
- La demo abre también con la decisión de ejemplo: el ejemplo lo arma siempre el motor determinista, aunque la instancia use un modelo, y el modelo se restaura después.
- **Solo local.** La demo pública sigue negándose a arrancar con un modelo de IA.
- **Medido en el HP** (2026-11-25, solo lectura, con el modelo que ya estaba cargado): 50 casos, 0 violaciones, 24 planes propuestos por la IA (15 de 16 válidos), mediana de 16 s y máximo de 18,5 s por plan, solo CPU. Un equipo sin GPU sirve para mostrarlo, no para una demo con prisa; por eso el plazo por plan es de 90 s (`AICOS_LLM_PLAN_TIMEOUT`) y existe el indicador.
- Para usar el Ollama del HP desde el Mac: túnel SSH y `AICOS_OLLAMA_URL=http://127.0.0.1:18434 AICOS_OLLAMA_MODEL=qwen3:4b-instruct-hermes ./run_demo_ia.sh`.

## Kit de grabación (`qa/grabar_demo.sh`)
Para grabar un video o sacar GIFs de cómo funciona **de verdad**, sin grabar la pantalla a mano: `qa/grabar_demo.sh` recorre la demo real (la misma app y el mismo servidor, en su propio puerto y base) con un cursor visible y rótulos en español, y deja en `./grabaciones/` (ignorada por Git): `demo-completo.mp4`, una **escena por archivo** (`escena-NN-nombre.mp4` y `.gif`, de 0,1 a 1 MB cada una) y `capturas/*.png`.
- **Escenas:** entrada con cuentas ficticias → la dueña abre y ya hay una decisión → aprueba una persona (la cifra de resultados sube) → trazabilidad en el visualizador → orden prohibida bloqueada → un plan real → cierre («datos ficticios · aprobación humana siempre»).
- `qa/grabar_demo.sh` usa el motor de reglas. **`qa/grabar_demo.sh --ia`** usa IA local gratis (Ollama; `AICOS_OLLAMA_URL` / `AICOS_OLLAMA_MODEL`): la escena del plan muestra «Pensando…» y luego «Propuesto por IA · validado por el motor».
- **Sin descargas:** el video no usa el ffmpeg descargable de Playwright; la prueba captura los cuadros por el protocolo del navegador y el script los une con el `ffmpeg` del sistema (y `gifsicle`, si está, para achicar los GIF).
- **Sin riesgo de que el guion se pudra:** la misma prueba corre rápida y sin video en el CI (`playwright.record.config.ts`), así que si la interfaz cambia y el recorrido deja de funcionar, el CI falla.
- Nada se sube ni se publica desde aquí: los archivos quedan en tu carpeta para que los revises antes de usarlos.
