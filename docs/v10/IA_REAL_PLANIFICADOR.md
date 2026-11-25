# V0.10 — IA real en el planificador del coordinador (spec 005)

Especificación, plan y tareas: `specs/005-ia-real-planificador/`. **Estado: Fase 1 (seguridad sin red ni clave).** Todavía no hay adaptador de pago ni gasto; el modelo local (Ollama) ya existía y es opcional.

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

## Límites honestos
- Las pruebas usan modelos **simulados**: demuestran que el motor se sostiene ante cualquier salida, no la calidad de un modelo real (eso es la evaluación de la Fase 3).
- El plazo duro corta la espera, pero un hilo de un modelo que sigue colgado no se mata (es un hilo demonio que termina solo).
- Falta: adaptador de pago, control de gasto, corpus y evaluación comparativa (Fases 2 y 3).
