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
