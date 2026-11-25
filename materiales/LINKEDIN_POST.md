# Borrador de publicación de LinkedIn (para aprobar y publicar TÚ)

> Borrador para que lo revises y lo publiques TÚ (nada se publica desde aquí). El enlace ya apunta al repositorio público.

---

Llevo meses construyendo algo que no cabe en una demo de 30 segundos, así que decidí mostrar **cómo se prueba**, no solo cómo se ve.

**AI Company OS** es un prototipo de equipos de agentes de IA para negocios de servicios técnicos: el cliente escribe, el coordinador arma el plan, administración cotiza, **y las decisiones importantes —gastos, planes y mensajes a clientes— las aprueba una persona**. Cada paso queda auditado y cada tarea solo se cierra con evidencia.

Lo que más me importó fue poder demostrarlo:
• 915 pruebas automáticas y 196 mutaciones de seguridad (rompo una propiedad a propósito y las pruebas deben notarlo).
• Una matriz que prueba el aislamiento entre empresas, generada desde el propio OpenAPI.
• Un fallo único en CI que no taché de «flaky»: lo reproduje 8 de 8 veces sin navegador, era una carrera real del motor, y la fijé con 15 pruebas deterministas.

Y lo que **no** es: no es un SaaS en producción, no tiene proveedor de identidad real ni MFA, y WhatsApp/correo están simulados. Prefiero decirlo yo.

Código visible para evaluar mi trabajo, con un caso de estudio de cuatro historias de ingeniería: https://github.com/Arcan17/ai-company-os-showcase

Si tienes un negocio donde las aprobaciones y la trazabilidad hoy viven en chats y planillas, conversemos sobre un **piloto a medida**.

#IA #Python #FastAPI #Ingeniería #Testing
