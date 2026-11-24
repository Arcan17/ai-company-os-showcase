# Caso de estudio — AI Company OS: construir un sistema de agentes **que se pueda auditar**

**Qué se pretendía:** que un negocio de servicios técnicos delegue trabajo repetitivo (solicitudes → presupuesto → aprobación → visita → cierre con evidencia) a un equipo de «agentes» **sin perder el control**: cada acción con permisos reales, cada decisión importante con un humano y cada paso reproducible.
**Qué se decidió no hacer:** agentes autónomos generales. Los «agentes» son **roles lógicos** (código determinista + registro cerrado de herramientas); un LLM local es opcional y solo *propone*, el motor valida. Eso es lo que permite **probar** el sistema en serio.

## Tres decisiones de diseño que importan
1. **La empresa sale solo de la sesión**, nunca del cuerpo de la petición; cada consulta filtra por `company_id`; un id ajeno responde **igual** que uno inexistente. Se verifica con una guarda estática (`qa/audit_identity_tenancy.py`) y con una matriz de pruebas generada del propio OpenAPI.
2. **Todo efecto pasa por un registro de herramientas** (permiso → validación → transacción → auditoría) y las transiciones críticas son **compare-and-set** sobre filas, con claves de idempotencia en la base. Un reintento no duplica nada.
3. **Las esperas humanas son estados persistidos** (`waiting_approval`, `waiting_task`…): la ejecución no avanza hasta que *una persona* actúa desde su sesión, y sobrevive a un reinicio.

## Cuatro historias de ingeniería (todas reproducibles en este repositorio)

### 1 · La matriz de aislamiento encontró un fallo real
En vez de escribir a mano casos de «la empresa B no ve lo de A», la prueba **se genera**: lee el OpenAPI de la aplicación viva y el registro de herramientas, y para cada ruta o herramienta con un identificador llama como la empresa B con ids de A. El oráculo exige tres cosas: la respuesta es **idéntica** a la de un id inexistente, no aparece ningún dato de A, y una huella SHA-256 de **todas** las filas de A no cambia; además un caso de control demuestra que la prueba no es vacía. Si alguien añade una ruta con id sin caso, **la prueba falla**.
**Hallazgo:** `tasks.create` y `materials.request` aceptaban referencias (`request_id`, `assignee_user_id`, `commitment_id`) de **otra** empresa, y el panel mostraba el nombre de un empleado ajeno. Exigía conocer un id de 48 bits y no modificaba datos de A, pero rompía la regla 1. Corregido validando la pertenencia de cada referencia.
*Dónde verlo:* `backend/tests/test_v07_cross_tenant_matrix.py`, `backend/app/tools/definitions.py` (`_own_refs`).

### 2 · Un fallo de CI que parecía «flaky» y era una carrera del motor
Una prueba de navegador falló **una vez** en CI (el botón de confirmar no aparecía). Se descartó lo fácil con evidencia: los cuatro CI anteriores pasaron en ≈ 6 s; seis corridas locales y cuatro **bajo 20 procesos de CPU** pasaron; el fallo duró 2 s + el *timeout* de 20 s **después** de aprobar. Se **reprodujo sin navegador** (aprobar en cuanto la aprobación es visible, motor en un hilo): la ejecución quedaba atascada **8 de 8** veces.
**Causa:** el motor crea la aprobación (ya visible) y varios pasos después marca la ejecución `waiting_approval`; si alguien decide en esa ventana, el CAS de la decisión no encuentra nada y **nadie reanuda** (*lost wake-up*).
**Arreglo:** una transición atómica «esperar o reanudar»: en una transacción se marca la espera y se **relee esa aprobación**; si ya estaba decidida, solo quien gana el CAS reanuda. Conserva el fencing del lease y no reactiva ejecuciones fallidas. **15 pruebas deterministas** (eventos y barreras, **sin dormir**) cubren aprobar y rechazar en la ventana, después de esperar, doble decisión, ocho carreras simultáneas, el material adicional y una ejecución fallida en la ventana. Con el arreglo: 0 de 8 atascadas. No se subió ningún *timeout* ni se quitó ninguna aserción.
*Dónde verlo:* `backend/app/engine/orchestrator.py` (`park_for_approval`), `backend/tests/test_v07_p25_approval_race.py`.

### 3 · Las pruebas de mutación delatan pruebas que pasan por la razón equivocada
Se rompe **una** propiedad de seguridad por vez en una copia (p. ej. «el correo del proveedor no tiene que coincidir») y las pruebas deben fallar. **134** mutaciones en cuatro tandas. Varias veces sobrevivió una: no por un defecto del producto sino por **dos defensas que protegían lo mismo y se enmascaraban** (la unicidad de la base de datos cubría la comprobación de la aplicación, o una comprobación de correo cubría a la de empresa). La lección práctica: **cada defensa necesita una prueba que anule a la otra**; y una mutación que sobrevive suele significar una prueba que pasa por una razón distinta de la que dice.
*Dónde verlo:* `qa/mutations_v07_p25.py` (con su prueba `test_every_p25_mutation_still_applies…`, que avisa si el código cambia y una aguja deja de existir).

### 4 · Una idempotencia que era un canal de fuga
El chat público usaba el id del mensaje como clave de idempotencia: quien conociera o adivinara un id recibía **el token de conversación de otra persona**. No bastaba cambiar `Math.random()`: se exige **prueba de posesión** (la clave es el id más la huella de un secreto de 128 bits que genera el cliente y que el servidor no guarda). Al reproducirlo aparecieron dos defectos más: el motor reutilizaba la *solicitud* de la víctima, y una ráfaga idéntica daba un 500 por `IntegrityError`. Los tres se corrigieron con pruebas rojas primero.

## Métricas (todas con su comando)
| Métrica | Valor | Fuente |
|---|---|---|
| Pruebas automáticas | 770 | `pytest --collect-only -q` |
| Mutaciones de seguridad | 134 (29 + 45 + 39 + 21) | `python qa/mutations_v07_p{1,2,25}.py qa/mutations_v08.py --list \| wc -l` |
| Consultas sin justificar en la guarda de aislamiento | 0 | `python qa/audit_identity_tenancy.py --check` |
| Pruebas de navegador real | 18 + 4 + 2 | `frontend/e2e*`, `npx playwright test` |
| Hallazgos de la matriz de aislamiento corregidos | 1 | historia 1 |

## Qué no está resuelto (y se dice)
Hay **ventanas análogas** a la de la historia 2 en `waiting_technician`, `waiting_task` y `blocked` (por lectura de código; no se reprodujeron ni se corrigieron). No hay proveedor de identidad real, MFA verificado, correo ni despliegue; el modo piloto existe, pero **no** está listo para personas reales. Está documentado en [`docs/v07/`](v07/).

## Qué muestra de mí como ingeniero
Priorizar la **evidencia** sobre la apariencia (pruebas rojas primero, mutaciones, reproducir antes de arreglar); diseñar para que el sistema **falle cerrado** y se pueda auditar; y decir con precisión lo que *no* se probó.
