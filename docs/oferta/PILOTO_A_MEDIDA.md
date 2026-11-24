# Piloto a medida — equipos de agentes con aprobación humana

**Para quién:** negocios de servicios (técnicos, mantención, instalaciones, talleres, administración de edificios…) donde las solicitudes llegan por varios canales, un humano aprueba presupuestos o gastos y hoy se pierde trazabilidad («¿quién aprobó esto?», «¿se hizo la visita?», «¿dónde está la evidencia?»).
**Qué NO es:** un SaaS listo para producción. Es un **piloto de descubrimiento y prototipo** sobre un flujo concreto, construido con la plataforma de este repositorio, con datos ficticios o anonimizados.

## Qué recibes
1. **Taller de descubrimiento (60–90 min):** mapeamos **un** flujo real de punta a punta: quién pide, quién cotiza, quién aprueba, quién ejecuta, qué evidencia se exige para cerrar.
2. **Prototipo configurado para tu negocio** (entorno de demostración que corre en un computador): tu empresa ficticia con sus roles, plantillas de asistentes por persona, catálogo de servicios/repuestos y **reglas de aprobación** (qué requiere decisión humana y de quién).
3. **Recorrido grabado y guion de demostración** del flujo con tus términos, para mostrarlo a tu equipo.
4. **Informe de cierre** con: qué funcionó, qué es real y qué es simulado, riesgos, y una **recomendación go / no-go** con los siguientes pasos y su alcance. Si es «no-go», lo dice.

## Qué garantiza el diseño (y está probado en este repositorio)
Aprobación humana para lo que importa; cierre de tareas **solo con evidencia** y por su responsable; auditoría de cada paso; aislamiento entre empresas; ninguna acción fuera de un catálogo cerrado de herramientas. Cómo se probó: [`docs/SEGURIDAD.md`](../SEGURIDAD.md) y [`docs/CASO_DE_ESTUDIO.md`](../CASO_DE_ESTUDIO.md).

## Qué NO incluye (se dice desde el principio)
- **Puesta en producción**, alojamiento, dominio, copias externas, monitoreo ni soporte 24/7.
- **Integraciones reales** (WhatsApp, correo, ERP, calendario, pagos): en el piloto son simuladas. Conectar una es una **fase aparte** (ver abajo).
- **Inicio de sesión de tu equipo con un proveedor de identidad real y MFA:** hoy solo existe probado contra un simulado de pruebas.
- **Datos personales reales de clientes o empleados.** Si más adelante se tratan, primero se acuerda un tratamiento de datos y se revisa con asesoría legal (esto **no** es asesoría legal ni un cumplimiento de la Ley 21.719 por sí mismo).
- **Garantía de resultados** (ahorro de horas, ventas, etc.): el piloto mide el flujo; no promete cifras.

## Qué necesito de tu lado
- Una persona **dueña del proceso** que decida y esté disponible para el taller y dos revisiones.
- **Un** flujo acotado y ejemplos **anonimizados** (una solicitud, un presupuesto, un cierre).
- Claridad sobre qué decisiones **siempre** debe tomar un humano.

## Cómo se acuerda
- **Cotización a medida, sin precio público**: se define tras el taller según el alcance. Pago por hitos (taller → prototipo → informe).
- **Plazo:** se acuerda tras el taller; no se promete antes de conocer el flujo.
- **Propiedad y confidencialidad:** los datos que aportes son tuyos y se tratan como confidenciales; el código de la plataforma es del autor (ver [`LICENSE`](../../LICENSE)); lo que se construya **específicamente** para tu negocio se define por escrito antes de empezar.

## Fases posteriores (solo si el informe dice «go»; cada una se cotiza aparte)
1. **Un canal real** (por ejemplo, correo o WhatsApp Business) con aprobación humana y registro.
2. **Identidad real** con proveedor y MFA, invitaciones y dominio propio.
3. **Alojamiento, copias cifradas fuera del servidor y monitoreo**, con región de datos y contratos definidos por ti.

## Cómo empezar
Escríbeme por [LinkedIn](https://www.linkedin.com/in/bastian-altamirano-3805b4309/) o desde el [portafolio](https://portfolio-seven-dusky-80.vercel.app) con: tu rubro, **el flujo** que más te duele (una frase) y qué decisión hoy no deja rastro.
