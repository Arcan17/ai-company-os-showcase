# Viñetas para CV / LinkedIn (cada cifra con su fuente)

Úsalas tal cual o recórtalas. **Todas son verificables con un comando** del repositorio; no agregues números que no estén en la columna «Fuente».

## Español
- Diseñé y construí **AI Company OS**, una plataforma multiempresa de equipos de agentes de IA con permisos reales, aprobación humana y auditoría (Python, FastAPI, SQLAlchemy, React/TypeScript, SQLite→PostgreSQL).
- Aseguré la calidad con **800 pruebas automatizadas**, **146 mutaciones de seguridad** y pruebas de navegador real (Playwright); toda consulta sobre datos de una empresa está cubierta por una **guarda estática** y una **matriz de aislamiento generada del OpenAPI**.
- **Diagnostiqué una carrera real del motor** («aprobación decidida antes de que la ejecución espere») que se manifestó como un fallo único de CI: la reproduje **8 de 8 veces sin navegador**, la corregí con una transición atómica y la fijé con **15 pruebas deterministas** (sin `sleep`).
- Implementé **identidad e invitaciones** (state + PKCE + nonce, secretos solo como hash, un solo uso, autenticación reciente para operaciones sensibles) y un **modo piloto que falla cerrado** ante configuraciones inseguras, con límites persistentes y cabeceras de seguridad.
- Documenté con honestidad lo que **no** está resuelto (sin proveedor de identidad real, MFA ni despliegue) y entregué un caso de estudio reproducible.

## English
- Designed and built **AI Company OS**, a multi-tenant platform of AI agent *roles* with real permissions, human approvals and audit trail (Python, FastAPI, SQLAlchemy, React/TypeScript).
- Backed it with **800 automated tests**, **146 security mutation checks**, real-browser E2E, a **static tenancy audit** and a **cross-tenant isolation matrix generated from the live OpenAPI**.
- **Root-caused a real engine race** (a lost wake-up on human approvals) first seen as a single CI failure: reproduced it **8/8 without a browser**, fixed it with an atomic park-or-resume transition and pinned it with **15 deterministic tests**.
- Built **identity & invitations** (state + PKCE + nonce, hash-only secrets, single use, step-up authentication) and a **fail-closed pilot mode** with persistent rate limits and security headers; documented what is **not** done.

## Fuentes
| Cifra | Comando |
|---|---|
| 800 pruebas | `cd backend && .venv/bin/python -m pytest --collect-only -q` |
| 146 mutaciones | `python qa/mutations_v07_p1.py --list \| wc -l` (29) + `_p2.py` (45) + `_p25.py` (39) + `mutations_v08.py` (21) + `mutations_v09.py` (12) |
| 8/8 reproducciones, 15 pruebas deterministas | `docs/CASO_DE_ESTUDIO.md` (historia 2) y `backend/tests/test_v07_p25_approval_race.py` |
| Guarda y matriz de aislamiento | `python qa/audit_identity_tenancy.py --check` · `backend/tests/test_v07_cross_tenant_matrix.py` |
