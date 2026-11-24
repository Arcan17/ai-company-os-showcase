# Especificación (ligera) — Vitrina pública y oferta de AI Company OS

**Fecha:** 2026-11-24 · **Estado:** borrador para aprobar · **Origen:** decisión de Bastian (2026-11-24): objetivo «ambos, empezando por el portafolio», repo público de vitrina saneado, Spec Kit ligero.
**Fuente:** el repositorio privado `Arcan17/ai-company-os` (prototipo local multiempresa de equipos de agentes con aprobación humana; datos 100 % ficticios).

## 1. Problema y propósito
AI Company OS tiene mucha ingeniería comprobable (77 commits; 752 pruebas; 111 mutaciones; auditorías de aislamiento entre empresas; E2E en navegador real; documentación v05–v07), pero **nadie puede verlo**: el repositorio es privado, el README describe la v0.5, no hay video ni caso de estudio, y no hay una oferta concreta. Hay que convertirlo en **(a)** una pieza de portafolio que un reclutador o cliente entienda en 3 minutos y **(b)** una oferta de servicio creíble, **sin prometer lo que el producto no es** (hoy no tiene proveedor de identidad, MFA ni correo reales).

## 2. Historias de usuario
- **US1 · Reclutador/ingeniero (P1).** Abro el repositorio público y en 3 minutos entiendo qué es, qué decisiones de ingeniería tomó y cómo lo probó, y puedo ejecutarlo en mi máquina con un solo comando.
  *Prueba independiente:* una persona ajena al proyecto, sin ayuda, ejecuta la demo siguiendo el README.
- **US2 · Cliente potencial (P1).** Veo un video corto y una página que explican, en lenguaje de negocio, qué problema resuelve (solicitudes → plan → aprobación humana → ejecución con evidencia → auditoría) y qué se me ofrece concretamente.
- **US3 · Bastian (P2).** Tengo viñetas verificables para CV/LinkedIn y un caso de estudio para mi portafolio, y cada cifra tiene su fuente.
- **US4 · Revisor de seguridad (P3).** Puedo comprobar que el repositorio público no filtra secretos, datos reales, rutas personales ni conversaciones internas.

## 3. Requisitos funcionales
- **FR-001** Repositorio **nuevo y sin historial** (`ai-company-os-showcase`), construido por copia selectiva del privado; el privado no se modifica.
- **FR-002** README **actualizado a v0.7**: qué es, captura/GIF, inicio en un comando, arquitectura (diagrama), qué es **real** y qué es **simulado**, seguridad (qué se probó y cómo), limitaciones honestas y mapa de documentos.
- **FR-003** Documento `ARQUITECTURA` con diagrama (Mermaid) del flujo solicitud → coordinador → aprobación → ejecución → auditoría, y del modelo de aislamiento por empresa.
- **FR-004** Caso de estudio (`CASO_DE_ESTUDIO.md`): problema, decisiones y *trade-offs*, **dos o tres historias de ingeniería** reales del proyecto (p. ej. la carrera de «aprobación decidida antes de esperar», reproducida 8/8 y corregida con pruebas deterministas; la matriz de aislamiento que halló un fallo real; las mutaciones que delataron pruebas débiles) y métricas **con fuente**.
- **FR-005** `docs/oferta/` con la **oferta**: a quién, qué se entrega en un piloto a medida, plazo, qué NO incluye, qué necesita del cliente. Sin precios inventados (ver Aclaraciones).
- **FR-006** Video guion + capturas/GIF de los escenarios A y D (2–3 min). El guion se entrega en este alcance; la grabación la hace Bastian (o se automatiza con Playwright si él lo pide).
- **FR-007** Saneamiento verificable: informe `SANEAMIENTO.md` con lo excluido y por qué, y una comprobación automática (búsqueda de secretos, correos, rutas absolutas del Mac, nombres de herramientas internas) que falla el CI si algo aparece.
- **FR-008** CI en el repositorio de vitrina (pytest + build + Playwright de regresión) que pase **desde cero** en un clon limpio.
- **FR-009** Licencia y aviso legal explícitos (datos ficticios; no es asesoría; sin garantía).
- **FR-010** Plantillas de texto listas para pegar: viñetas de CV, publicación de LinkedIn, descripción de gig/servicio.

## 4. Criterios de éxito (medibles)
- **SC-001** Un clon limpio del repositorio de vitrina arranca la demo con **un comando** y pasa **pytest + build + Playwright de regresión** en CI.
- **SC-002** La comprobación de saneamiento no encuentra **ningún** secreto, correo personal, ruta absoluta ni mención de herramientas/conversaciones internas, y su propia prueba demuestra que **sí detecta** cada categoría (mutación).
- **SC-003** Toda cifra del README, el caso de estudio y las plantillas de CV tiene una **fuente** (comando o archivo) y coincide con la ejecución real.
- **SC-004** Una persona ajena ejecuta la demo y completa el escenario A siguiendo solo el README, en **≤ 10 minutos**.
- **SC-005** El README nombra **explícitamente** lo que NO existe (proveedor de identidad real, MFA, correo, despliegue) en las primeras pantallas.

## 5. Fuera de alcance
Construir funcionalidades nuevas del producto (PR B, P3/WorkOS, despliegues, hosting, dominio, pasarela de pago); publicar nada en GitHub/LinkedIn/Fiverr **sin tu aprobación explícita**; datos o clientes reales; precios y compromisos legales; modificar el repositorio privado, sus bases o sus servicios.

## 6. Supuestos
Datos y cuentas 100 % ficticios; el simulado de identidad solo existe en pruebas; el repositorio privado seguirá siendo la fuente de verdad (la vitrina es una **copia publicada a mano**, no un espejo); el contacto público se define por Bastian (no se incluye ningún correo personal por defecto).

## 7. Aclaraciones pendientes (máx. 3; con recomendación)
1. **Licencia:** ¿código abierto (MIT) o «código visible, todos los derechos reservados»? *Recomendado:* **visible, todos los derechos reservados** (evita que un tercero revenda el producto que quieres ofrecer; sigue sirviendo para empleo).
2. **Nombre y contacto públicos:** ¿qué contacto aparece (LinkedIn/portafolio, no el correo)? *Recomendado:* LinkedIn + página del portafolio.
3. **Oferta y precio:** ¿se publica un precio de referencia o solo «cotización a medida»? *Recomendado:* **sin precio público** hasta tener al menos un piloto real que lo calibre.
