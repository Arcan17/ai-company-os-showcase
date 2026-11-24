# Tareas — Vitrina pública y oferta de AI Company OS

Orden de ejecución; cada bloque termina en algo verificable. `[P]` = se puede hacer en paralelo. Nada se publica sin aprobación explícita.

## Fase 1 — Saneamiento y esqueleto (bloqueante)
- [ ] T001 Inventario de lo que **no** puede ir público (secretos, correos, rutas del Mac, `backend/data/*`, capturas con datos, checkpoints/handoffs, referencias a Codex/ChatGPT, memoria) → `SANEAMIENTO.md` (US4, FR-007)
- [ ] T002 Lista blanca de copia (`backend/app`, `backend/tests`, `frontend/src`, `frontend/e2e*`, `qa/`, `docs` seleccionados, `run_demo.sh`) y script `tools/export_showcase.sh` **reproducible** (FR-001)
- [ ] T003 Comprobación automática de saneamiento `tools/check_showcase.py` + su prueba de mutación (una por categoría) (SC-002)
- [ ] T004 Primera copia + commit inicial sin historial; ejecutar la comprobación (debe pasar)

## Fase 2 — Que funcione desde cero
- [ ] T005 Arranque con un comando en un clon limpio (`run_demo.sh`), sin `backend/data` previo (SC-001, SC-004)
- [ ] T006 CI de la vitrina (`.github/workflows/ci.yml`): pytest + build + Playwright de regresión + comprobación de saneamiento (FR-008)
- [ ] T007 Ejecutar todo en local y anotar cifras reales (pruebas, E2E) como fuente de SC-003

## Fase 3 — Documentación que vende y que prueba
- [ ] T008 README v0.7 (qué es, qué NO existe, un comando, mapa) (FR-002, SC-005)
- [ ] T009 [P] `docs/ARQUITECTURA.md` con diagramas Mermaid (FR-003)
- [ ] T010 [P] `docs/CASO_DE_ESTUDIO.md` con las historias de ingeniería y métricas con fuente (FR-004, SC-003)
- [ ] T011 [P] `docs/SEGURIDAD.md`: qué se probó (matriz de aislamiento, mutaciones, guardas), cómo reproducirlo y qué NO se probó
- [ ] T012 [P] Licencia y aviso legal (FR-009; depende de la aclaración 1)

## Fase 4 — Oferta y materiales
- [ ] T013 `docs/oferta/PILOTO_A_MEDIDA.md` (a quién, entregables, plazo, exclusiones, requisitos del cliente) (FR-005; depende de la aclaración 3)
- [ ] T014 Guion del video de 2–3 min + lista de capturas/GIF (FR-006)
- [ ] T015 `materiales/` con viñetas de CV, post de LinkedIn y descripción de servicio, cada cifra con su fuente (FR-010, SC-003)

## Fase 5 — Revisión y publicación (requiere tu aprobación)
- [ ] T016 Revisión final: una persona ajena sigue el README (SC-004); revisión de seguridad del repositorio completo
- [ ] T017 **Tu decisión:** crear el repositorio público en GitHub y publicar (no se hace sin tu «sí» explícito)
