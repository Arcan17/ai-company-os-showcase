# V0.8 — Seguridad y control de datos de clientes

Especificación: `specs/003-seguridad-datos-clientes/`. Datos 100 % ficticios; la conformidad con la ley de datos personales de Chile **no** está evaluada aquí (se valida con asesoría legal antes de usar datos reales).

## Hallazgo (antes de cambiar nada)
Se probó con una sesión de técnico/a contra la demo: `/api/customers` y `/api/requests` ya le respondían 403, **pero** `GET /api/runs/{id}/events` (permiso `events.read`, que el técnico/a sí tiene) devolvía el teléfono y el correo de la clienta dentro de las entradas y salidas de herramientas (`customers.find`, `customers.create`…). El flujo en vivo (SSE) hacía lo mismo.

## Qué cambia
| Pieza | Comportamiento |
|---|---|
| Contacto oculto (`privacy.scrub`) | Un rol sin `customers.read` recibe `[oculto]` en lugar de teléfono, correo y dirección **en eventos, SSE, detalle de ejecución, planes, tareas y órdenes de trabajo**, tanto por clave (`phone`, `email`…) como por **valor** dentro de textos libres. Se enmascara al leer: lo guardado no se toca. |
| Registro de consultas | Abrir una ficha (detalle de solicitud o lista de clientes) deja `customer.view` en la auditoría con **los campos** entregados, nunca el dato. Una entrada por persona y ficha cada 10 minutos. Solo la dueña lo lee (`customers.access_log`). |
| Exportar | `GET /api/customers/{id}/export` (solo la dueña): ficha, solicitudes, mensajes, presupuestos y visitas; auditado. |
| Anonimizar | `POST /api/customers/{id}/anonymize` (solo la dueña, con autenticación reciente): exige escribir el nombre exacto, se niega con trabajo activo (409), reemplaza nombre/teléfono/correo/dirección por `[anonimizado]` en todas las tablas de la empresa (incluida la forma escapada en JSON) y conserva montos, fechas y estados; auditado. |

## Evidencia
- `backend/tests/test_v08_customer_privacy.py` (6) y `test_v08_customer_data_control.py` (9), más 3 casos nuevos en la matriz de aislamiento entre empresas.
- `qa/mutations_v08.py`: 21 mutaciones, todas detectadas (`--list` las enumera).
- `frontend/e2e/v08_customer_data.spec.ts`: la ficha del cliente (quién lo vio, descargar, confirmación por nombre, negativa con trabajo activo).

## Límites conocidos
- El nombre del cliente **no** se oculta al técnico/a (lo necesita para trabajar).
- La anonimización reemplaza por texto literal: un nombre muy corto (< 5 caracteres) no se barre por el riesgo de tocar otras palabras; los datos de contacto sí.
- El paso de autenticación reciente para anonimizar está configurado (`STEP_UP_PERMS`) pero su efecto lo prueba el modo piloto existente, no una prueba nueva de esta fase.
- No incluye cifrado en reposo, retención ni base legal.
