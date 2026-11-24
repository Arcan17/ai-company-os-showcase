# Qué es real, qué es simulado y qué explicar a una empresa

Resumen de una página para el presentador. Todo lo de abajo está **verificado en el Mac** con la demo comercial (ver `REPORTE_PREPARACION_DEMO.md`). La demo usa una empresa, personas y datos **100 % ficticios**.

## Real (funciona de verdad en la demo)
| Qué | Cómo se comprueba |
|---|---|
| Base de datos persistente (SQLite), multiempresa, permisos por rol, auditoría | Cada plan, tarea, delegación y evidencia está en `plans`, `plan_steps`, `tasks`, `delegations`, `events`, `audit_log`; el guion automático compara pantalla ⇄ SQLite |
| El dueño pide trabajo en lenguaje natural y el coordinador arma un **plan con datos reales** de la empresa (solicitud, personas, stock) | El plan queda guardado **antes** de ejecutar; nada se crea hasta que una **persona** confirma |
| Delegación a administración y técnico, **con dependencia** (el técnico espera a administración) | La tarea del técnico existe pero «en espera» hasta que administración cierra la suya |
| Cierre de tareas **con evidencia**, solo por su responsable | Intentos de otra persona o con evidencia insuficiente se rechazan y quedan auditados |
| Verificación del coordinador sobre el **registro** (no sobre lo que alguien afirma) + foto real del stock | El plan solo pasa a «Completada» cuando todo está cerrado con evidencia |
| Límites: compras, pagos, mensajes externos, aprobar, borrar, agendar e intentos de saltarse reglas se **bloquean** | La prueba de control («Compra repuestos y avisa a todos por WhatsApp») queda bloqueada, sin pasos ni efectos |
| Visualizador en vivo (SSE) e historial reproducible | Los eventos mostrados son las filas de `events` (mismo número y orden); sobrevive a reiniciar el servidor |
| Aislamiento entre empresas y entre personas | Otra empresa no ve el plan (404); administración y técnico no tienen poderes del dueño |

## Simulado o inexistente (decirlo SIEMPRE)
- **WhatsApp, correo, teléfono, pagos, calendarios, compras a proveedores: no existen.** Los mensajes al cliente son borradores «preparado, no enviado». La visita queda «propuesta» hasta que el *cliente* la confirme (no se confirma sola).
- **Los «agentes» son roles lógicos**: código determinista + herramientas con permisos. No son agentes autónomos generales. El modelo de lenguaje local (Ollama) es **opcional**, solo *propone* y el motor valida; la demo corre sin él y sin red.
- **Datos y personas ficticios**: Clima Sur, Marta Rojas, Javiera, Matías, el stock. Las cuentas demo usan una contraseña conocida.
- **El reloj de vencimientos** es manual (botón), no hay tareas programadas en segundo plano.

## Limitaciones que conviene explicar antes de que lo pregunten
1. Es un **prototipo local**, no un servicio: sin alta de usuarios reales, sin recuperación de contraseña, sin TLS, sin copias de seguridad programadas, una sola máquina.
2. No hay integración con sus herramientas actuales (órdenes de trabajo, stock, WhatsApp, correo). **Ese es justo el primer flujo a descubrir con ellos.**
3. La seguridad probada es la de un laboratorio con datos ficticios; no es una certificación ni garantía de producción.
4. Un fallo de ejecución no se reintenta solo (decisión deliberada: reintentar sin reconciliar efectos podría duplicar trabajo).
5. Solo se ha probado con español y con un caso de servicio técnico (mantención de aire acondicionado).

## No prometer
Ahorro de costos o porcentajes, disponibilidad 24/7, cumplimiento legal, que ya existen clientes pagadores, llamadas o mensajes reales, ni que «la IA decide sola». Si preguntan por precio: «aún no hay precio; primero queremos entender su flujo».

## Preguntas difíciles (respuestas honestas)
- **«¿Esto manda WhatsApp a mis clientes?»** No. Prepara el borrador; enviar requeriría una integración y la aprobación de una persona.
- **«¿Y si la IA se equivoca?»** El modelo (si se usa) solo propone; el plan lo valida el sistema, lo confirma una persona, y todo lo que se crea queda auditado. Lo que no está permitido se bloquea.
- **«¿Puedo usarlo con mis datos?»** Todavía no: faltan autenticación real, respaldos e integración. Podemos acordar un piloto acotado cuando eso esté resuelto.
- **«¿Funciona sin internet?»** Sí, la demo corre en local y no hace ninguna petición externa (se comprobó).
