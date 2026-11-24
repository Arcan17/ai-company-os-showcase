# AI Company OS — hoja de ruta de prototipo V0.5 a producto comercial V1

**Fecha de decisión:** 2026-11-22 · **Producto canónico:** `Arcan17/ai-company-os` · **Objetivo:** una plataforma B2B confiable para pequeñas empresas de servicios que quieran coordinar solicitudes, empleados y acciones verificables, con suficientes controles para un piloto pagado.

> Este documento es una **planificación**. Ninguna etapa futura se considera implementada hasta contar con código, pruebas, despliegue e intervención humana verificados. **La V0.5 se integró en `main` por PR #6, merge commit `1fbcf8b`.** La rama de esta hoja de ruta no modifica bases ni servidores del Mac.

## Propuesta de valor (enfocada)

**Cliente inicial que evaluaremos:** empresas chilenas pequeñas de mantenimiento, instalación y servicios técnicos con un dueño y 2–20 trabajadores.

**Problema:** el dueño y administración coordinan solicitudes por varios canales, asignan tareas, persiguen evidencias, olvidan compromisos y carecen de un historial verificable de decisiones. 

**Qué vendemos:** «Pide un trabajo al coordinador, revisa el plan, aprueba lo que tiene impacto, reparte tareas y comprueba con evidencia que el equipo las completó». **No** vendemos cinco modelos LLM autónomos ni una agencia telefónica completa en V1.

**Producto mínimo pagable** = multiempresa, cuentas reales, permisos por rol y empleado, un canal de entrada/salida útil, decisiones humanas sobre acciones materiales, tareas persistidas con seguimiento, comprobación de resultados, seguridad, respaldo/restauración, soporte y una forma de suscribirse. No requiere CRM genérico ni 30 integraciones.

## Qué existe al iniciar

| Capacidad | Verificación / límite |
|---|---|
| Empresas ficticias, trabajadores, asistentes por plantilla | V0.5 integrada en `main` |
| Coordinador determinista + LLM local opcional | Planes acotados, confirmación humana y registry de herramientas |
| Asignación dinámica, tareas, aprobación, eventos SSE, visualizador | SQLite + tests locales reportados |
| Demo comercial ficticia en Mac | Servidores/DBs separados, **no despliegue público** |
| Seguridad Safari | Script AppleScript mutador eliminado en V0.5 |
| CI, auth producción, Postgres, colas, canales, facturación | Pendientes o no probados |
| Empresas interesadas en pagar | No validado |

## Releases y condiciones de avance

### M1 — V0.6: calidad automática y operación segura

**Primero:** GitHub Actions reproducible (pytest, build React/TS, Playwright regresión + demo) en runner Linux aislado. Los tests en GitHub deben dar **PASS real**, no solamente los del Mac. No exponer secretos ni bases reales.

**Después:** seguridad de acceso (CSRF con cookie, credenciales y sesiones reales, limitación de intentos, secure cookies bajo TLS, eventos auditados, revocación, trusted proxy), migración y backup/restore versionados, recuperación segura del motor. Mantener un catálogo de amenaza/capacidad, y tests de abuso, multiempresa, concurrencia y fallos.

**Gate:** CI verde en PR y main, restauración comprobada, ningún retry puede duplicar un movimiento, y suite A–E intacta. Sin deploy público por este solo gate.

### M2 — V0.7: una empresa piloto autenticada de forma real

Comparar alternativas gestionadas a operar infraestructura propia para PostgreSQL/Auth/Backups/Storage; preferir herramientas existentes con coste y seguridad controlados, sin una migración masiva si no ofrece ventaja demostrable.

Necesitamos: invitación y recuperación de cuenta reales, administración de empleados, MFA o segundo factor adecuado para operaciones sensibles, conexión segura HTTPS, controles por tenant verificados (RLS si PostgreSQL), borrado/exportación y retención, trazabilidad de accesos, monitoreo básico, despliegue de staging privado y respaldos periódicos restaurables.

> **P0 de V0.7 (Issue #17, 2026-11-23):** auditoría, comparativa y plan en [`docs/v07/`](v07/P0_ARQUITECTURA_Y_PLAN.md) — recomendación: credenciales delegadas a un proveedor de identidad gestionado tras una interfaz propia, **sesiones, permisos y `company_id` propios**, SQLite + restic en un VPS con HTTPS; PostgreSQL aplazado con disparadores. Es un **diseño**: nada de eso está implementado ni contratado.

**Gate:** una empresa ficticia de prueba puede pasar por alta, empleados, solicitud, coordinación, evidencia y baja sin acceso entre empresas. Un pentest/acceso hostil de QA no detecta fugas evidentes. Todo con datos sintéticos.

### M3 — V0.8: primer canal real, únicamente uno

**No seleccionar proveedor ni canal sin entrevistas**; investigar costos y restricciones. Primero correo empresarial o WhatsApp (o teléfono si ese fuese el dolor principal). Inbound/outbound autenticados, borradores/consentimiento, aprobación humana, límites de gasto, webhooks firmados, idempotencia/outbox/reintento seguro, acuse de entrega y posibilidad de detener envíos.

**Gate:** un mensaje externo autorizado se recibe o envía de verdad en entorno controlado con cuentas de prueba, queda auditable y no sale duplicado. La tarea y el resultado se relacionan con la comunicación.

### M4 — V0.9/V1.0: piloto pagado y funcionamiento sostenible

Entrevistas con cinco empresas (sin prometer resultado). Buscar 1–3 pilotos acotados con objetivo medible, contrato/privacidad, onboarding asistido, soporte, observabilidad, incidentes, backups probados, facturación gestionada con proveedor existente, exportación de datos, retención, costes por cuenta y límites de uso. Accesibilidad y compatibilidad entre navegadores.

**Gate V1:** 1 flujo importante que se ejecuta de extremo a extremo con datos reales **autorizados**, permisos/seguridad/soporte/operación demostrados, coste sostenible y cliente que mantiene su intención de pago por valor medido. Si falta demanda o falla seguridad, no llamar al producto «listo».

## Experimentos comerciales que correrán en paralelo

Antes de una integración costosa: conversar con 5 pequeñas empresas chilenas de servicios, preguntar qué herramientas usan, qué seguimiento manual hacen, quién tiene capacidad de pago y si pagarían por **un piloto definido**. No copiar datos empresariales en el prototipo. Elegir primer canal y priorización según respuestas; no asumir que más LLM equivale a mayor valor.

## Arquitectura objetivo (gradual)

```
Empresa/empleados ─HTTPS─► aplicación/API ─► control de identidad/tenant
                                      │
                                      ├─► coordinador + herramientas autorizadas
                                      │     ├─► aprobaciones humanas y tareas
                                      │     └─► outbox de acciones externas (gate)
                                      │
                                      ├─► BD transaccional + auditoría / backup
                                      ├─► worker/scheduler supervisado con idempotencia
                                      └─► eventos persistidos + SSE + visualizador
```

El registro de herramientas y las reglas de autorización actuales se conservan. Un LLM solo propone decisiones estructuradas validadas; no se le entregan credenciales ni poderes para hacer pagos, compras o envíos sin autorización.

## Riesgos y dependencias que no se pueden omitir

1. **Seguridad actual:** `security.py` implementa sesiones opacas HttpOnly con scrypt, pero la aplicación no cuenta todavía con un modelo de identidad/public onboarding apto para clientes reales; faltan CSRF, rate limit, MFA y TLS. `config.py` usa SQLite por defecto; `db.py` migra de forma propia. `ARCHITECTURE.md` reconoce hilo por ejecución y sin worker supervisado.
2. **Entornos:** los servicios y bases Mac V0.4/V0.5 se tratan como instalaciones separadas; **no** migrar `aicos.db` ni `demo_comercial.sqlite3` automáticamente por haber mergeado GitHub. Trabajar sobre copias/DBs efímeras.
3. **Privacidad en Chile:** Ley 21.719 y su postergación: el Congreso aprobó el 17-nov-2026 el cambio propuesto al **15 de octubre de 2027**, pero el 20-nov fue reportado **en fase de promulgación**, no asumir nueva vigencia oficial sin consultar texto publicado. Preparar desde ahora tratamiento, minimización, consentimiento/base legal, derechos de titulares, retención y roles de responsable/encargado. Fuentes: https://www.camara.cl/cms/2026/11/20/datos-personales-combustibles-y-sala-cuna-en-los-temas-despachados-por-la-camara/ y https://www.bcn.cl/leychile/Navegar?idNorma=1209272 .
4. **Referencia de seguridad**: OWASP ASVS 5.0 (https://asvs.owasp.org/); marcar qué controles existen y cuáles faltan. No afirmar certificación ni cumplimiento hasta evaluarlo.
5. **Economía:** evitar despliegues/servicios pagados antes de comparar costo total y tener hipótesis comercial verificable. GitHub Actions privado puede consumir minutos de la cuota según plan/uso.

## Entregable inmediato

- [Roadmap V1: issue #7](https://github.com/Arcan17/ai-company-os/issues/7) (este documento desarrolla el alcance).
- [V0.6 P0: issue #8](https://github.com/Arcan17/ai-company-os/issues/8), branch `feat/v06-ci-and-product-roadmap`.
- Primer PR: pipeline GitHub Actions aislado + este documento. **Si CI falla, reparar en la rama; no fusionar con controles fallidos.**
- Después del primer PR verde: iniciar V0.6 P1 — sesión/CSRF/rate limiting y análisis de migración/worker. No introducir canales ni producción hasta gates.

