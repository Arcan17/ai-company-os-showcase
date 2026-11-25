# V0.10 — IA real en el planificador del coordinador (spec 005)

Especificación, plan y tareas: `specs/005-ia-real-planificador/`. **Estado: Fases 1 y 2 (sin red ni clave real).** Hay adaptador de pago y control de gasto, probados con un cliente simulado; la evaluación con modelos reales es la Fase 3.

## Por qué la seguridad es estructural
Un modelo solo puede devolver un `PlanProposal`: cuatro banderas sí/no sobre acciones de un **catálogo cerrado**, un `request_id` y etiquetas de acciones no soportadas de un enum. No escribe pasos, no nombra personas, no llama herramientas. El motor revalida cada bandera contra el texto de la dueña y los datos de la empresa, escanea lo prohibido (comprar, pagar, escribir a clientes…) sobre el **texto crudo** (el modelo no puede quitar un bloqueo), limita a 6 pasos y no crea nada hasta que la dueña confirma.

## Qué agregó la Fase 1
| Cambio | Efecto |
|---|---|
| Plazo duro (`AICOS_LLM_PLAN_TIMEOUT`, 60 s) y ninguna excepción escapa | Un modelo caído, lento o que lanza un error ya no rompe la creación del plan: se usan las reglas. |
| Códigos de respaldo (`error`, `lentitud`, `invalida`, `sin_tareas`) | El plan guarda el motivo y la interfaz dice «Por reglas: la IA tardó demasiado», «propuesto por IA (modelo X)» o «Reglas deterministas». |
| Forma de la respuesta | Booleanos mal tipados, `request_id` de otro tipo, etiquetas que no son lista… se tratan como propuesta inválida. |
| Lo que ve el modelo | Solo `id`, nombre del cliente **saneado** (≤ 40 caracteres, sin símbolos ni saltos de línea) y categoría. Nunca la descripción ni los mensajes del cliente. |
| Demo pública | Se niega a arrancar con cualquier proveedor distinto de `deterministic`. |

## Un hallazgo
El nombre de un cliente (hasta 120 caracteres de texto libre de un tercero) entraba tal cual al prompt del modelo. No era explotable por la estructura cerrada del resultado, pero era un canal cómodo para una inyección; ahora se sanea y se limita.

## Evidencia
- `backend/tests/test_v10_planner_hostile.py` (27 casos): excepciones, plazo, respuestas mal formadas, propuestas sin tareas, intentos de quitar bloqueos, instrucciones hostiles con un modelo «obediente», lo que ve el modelo, concurrencia con un modelo roto.
- `qa/mutations_v10.py`: 10 mutaciones, todas detectadas.

## Fase 2: modelo de pago y control de gasto
`AICOS_LLM_PROVIDER=anthropic` activa un adaptador con el **SDK oficial** (`anthropic`, versión fijada) que solo hace `propose_plan` y devuelve el mismo `PlanProposal` de vocabulario cerrado; clasificar, redactar y materiales los sigue haciendo el motor determinista y se rotulan como tal.

| Pieza | Comportamiento |
|---|---|
| Configuración | `AICOS_ANTHROPIC_API_KEY` (a propósito NO la variable estándar, para no gastar por accidente con la clave de otra herramienta), `AICOS_ANTHROPIC_MODEL` (por defecto `claude-opus-5-5`), precios por millón de tokens (tabla conocida o `AICOS_ANTHROPIC_PRICE_IN/OUT`; un modelo sin precio **no arranca**). Sin clave, topes inválidos o modelo sin precio: el servidor se niega a arrancar con un mensaje claro. |
| Llamada | Salida estructurada (esquema JSON), esfuerzo `low`, `max_tokens` 1024, **sin reintentos** (`max_retries=0`: cada llamada cuesta), sin temperatura ni herramientas; plazo del planificador (60 s). |
| Gasto (`providers/budget.py`) | Tabla `llm_usage`. **Antes de llamar** se reserva el costo del PEOR caso (entrada estimada por exceso + toda la salida permitida) en UNA sentencia atómica contra el tope del día (USD 1 por defecto) y del mes (USD 10); si no cabe, **no se llama** y el plan sale por reglas con el motivo «se alcanzó el tope de gasto de la IA». Al terminar, la reserva se cambia por el costo real (tokens de `usage`); una llamada fallida libera su cupo; una reserva huérfana (proceso muerto) sigue contando (se equivoca hacia gastar menos). |
| La clave | Solo del entorno; enmascarada en `repr`, errores y registros (prueba). |
| Interfaz | El rótulo del tablero distingue «IA de pago (API)» de «LLM local». |

Evidencia: `backend/tests/test_v10_anthropic_budget.py` (20 casos, sin red) y 15 mutaciones nuevas en `qa/mutations_v10.py` (25 en total), todas detectadas.

## Límites honestos
- Las pruebas usan modelos **simulados**: demuestran que el motor se sostiene ante cualquier salida, no la calidad de un modelo real (eso es la evaluación de la Fase 3).
- El plazo duro corta la espera, pero un hilo de un modelo que sigue colgado no se mata (es un hilo demonio que termina solo).
- Falta: el corpus y la evaluación comparativa con modelos reales (Fase 3), que es donde se mide calidad y costo reales.
- El precio de lista usado para el tope es el declarado al 2026-11-03; si cambia, hay que actualizarlo (o fijarlo con `AICOS_ANTHROPIC_PRICE_IN/OUT`). El costo real lo factura el proveedor: el tope es una barrera de la aplicación, no la factura.
- Restaurar una copia de seguridad antigua devuelve el contador de gasto a su valor de entonces (puede subestimar el gasto reciente).
- No se usa el parámetro de respaldo del lado del servidor del proveedor: ante un rechazo o una falla, el respaldo es el plan por reglas, rotulado.
