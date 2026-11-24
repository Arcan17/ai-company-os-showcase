# Guion del video (2:45) — «Un equipo de agentes de IA que trabaja a la vista»

**Objetivo:** que un cliente potencial o un reclutador entienda en menos de 3 minutos **qué problema resuelve, qué control tiene el humano y qué evidencia respalda el trabajo**, sin prometer lo que no existe.
**Cómo grabarlo (lo haces tú; nada se automatiza ni se publica sin tu «sí»):** `AICOS_RESET=1 PORT=8790 ./run_demo.sh`; ventana del navegador de **1440×900** con un perfil limpio (sin pestañas ni marcadores personales); grabación de pantalla del sistema; micrófono aparte. Cuentas ficticias (`dueno@climasur.test`, contraseña `demo1234`).

| Tiempo | Pantalla | Qué se ve | Narración |
|---|---|---|---|
| 0:00–0:15 | Título + pantalla de entrada | Banner «datos 100 % ficticios» y cuentas de demostración rotuladas | «Cuando las solicitudes llegan por todas partes, nadie sabe quién aprobó qué. Esto es AI Company OS: un equipo de agentes de IA que trabaja a la vista. Los datos son ficticios; todo lo que ves corre en mi computador.» |
| 0:15–0:45 | *Chat de cliente (prueba)* → escenario A → *Enviar* → *Ver ejecución* | El visualizador cambia de estado con eventos reales | «Una clienta escribe. Recepción la identifica, el coordinador clasifica, administración prepara el presupuesto y revisa el stock. Cada nodo es una fila real en la base; puedes hacer clic y ver entradas y salidas.» |
| 0:45–1:15 | Tarjeta de aprobación del dueño | La ejecución **se pausa** pidiendo decisión humana | «Aquí se detiene. Nadie puede aprobar el gasto salvo la dueña —ningún agente tiene ese permiso—. Aprobar o rechazar queda auditado.» → **Aprobar** |
| 1:15–1:40 | Operaciones → detalle de la solicitud | Visita **«Propuesta · sin confirmar»**; mensaje «preparado, no enviado» | «La visita queda *propuesta*, no confirmada. El mensaje al cliente es un borrador: aquí WhatsApp y correo están simulados, y lo decimos.» |
| 1:40–2:00 | Chat de cliente → botón «Simular: el cliente responde CONFIRMO» | La visita pasa a confirmada | «Solo cuando el cliente responde con su código la visita se confirma de verdad.» |
| 2:00–2:20 | *Coordinador*: escribir «Compra repuestos y avisa por WhatsApp a los clientes» | Plan **Bloqueado**, sin pasos ni efectos | «Y lo prohibido se bloquea: comprar, pagar, escribir a clientes o saltarse reglas no está en el catálogo de herramientas.» |
| 2:20–2:45 | Terminal (pytest y mutaciones) + `docs/CASO_DE_ESTUDIO.md` | `770 passed`; `39 / 39 detectadas` | «Lo que más cuido es poder demostrarlo: 770 pruebas automáticas, 134 mutaciones de seguridad, una matriz que prueba el aislamiento entre empresas y una carrera real del motor que reproduje 8 de 8 veces antes de corregirla.» |
| 2:45–3:00 | Pantalla final | Qué es / qué no es + contacto | «Es un prototipo local, no un SaaS: sin proveedor de identidad real ni despliegue. Si tu negocio tiene un flujo con aprobaciones, hagamos un piloto a medida. Mi contacto está en el repositorio.» |

## Capturas de apoyo ya disponibles
`docs/screenshots/02-flow-waiting-approval.png` · `03-flow-completed.png` · `04-operations-detail.png` · `11-d-material-approval.png` · `14-e-plan-proposal.png` · `17-e2-blocked.png` · `docs/demo/screenshots/safari-02-login.png`.

## Reglas para no prometer de más
No decir «seguro» sin decir «probado así» · no mostrar el correo ni otras pestañas personales · no nombrar clientes reales ni usar datos reales · repetir en pantalla «datos ficticios» y «WhatsApp/correo simulados».
