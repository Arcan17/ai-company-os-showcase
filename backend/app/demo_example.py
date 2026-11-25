"""V0.11 (spec 006): una demo NUEVA abre con el producto en uso — una solicitud de cliente a medio camino y una decisión pendiente de la dueña — en vez de una pantalla vacía.

El ejemplo NO inserta filas a mano: envía el escenario «A» de la demo (Marta Rojas pide reparar su split) por el chat público REAL y deja que el motor determinista lo recorra hasta
`waiting_approval`. Así hereda reglas, eventos y auditoría; y una aprobación pendiente sigue teniendo la ejecución que la espera (`approvals.request` exige `run_id`).

Con un modelo de IA activo (demo local con Ollama) el ejemplo se crea IGUAL con el motor determinista —solo durante este arranque— y el proveedor vuelve a ser el modelo: así la demo con IA
también abre con la decisión de ejemplo y lo que se ve es siempre el mismo ejemplo.

Garantías (cada una con prueba y mutación):
  · APAGADO por defecto: solo se crea con `AICOS_DEMO_EXAMPLE=1` (run_demo.sh, start.sh, compose y la demo pública lo activan; las suites y el piloto no).
  · NUNCA en piloto, y SOLO en la empresa ficticia `clima-sur` de una base que aún no tiene ejecuciones (no ensucia una base ya usada ni una empresa dada de alta por el operador).
  · Siempre con el motor determinista (sin IA ni red), aunque la instancia use un modelo para el resto.
  · Idempotente y a prueba de cortes: la marca `demo.example` se escribe AL FINAL; si algo falla a medias no hay marca y no se duplica nada.
"""
from __future__ import annotations

import logging
import time
import uuid

from .config import settings
from .db import one
from .engine.orchestrator import ChatInput
from .events import audit
from .providers.deterministic import DeterministicProvider
from . import pilot

log = logging.getLogger("aicos")
MARK, SLUG = "demo.example", "clima-sur"
MESSAGE = "Hola, mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas. ¿Pueden venir esta semana?"   # escenario A de «Probar como cliente»
DEADLINE_S = 30.0


def ensure_example(eng, orch, *, deadline: float = DEADLINE_S) -> bool:
    """True si creó el ejemplo en esta llamada. Nunca lanza: un fallo se registra y el arranque sigue (la demo simplemente abre vacía, como siempre)."""
    try:
        return _ensure(eng, orch, deadline)
    except Exception:                                                              # noqa: BLE001
        log.exception("demo: no se pudo crear el ejemplo (la demo abre sin él)")
        return False


def _ensure(eng, orch, deadline: float) -> bool:
    if not settings.demo_example or pilot.is_pilot():
        return False
    with eng.connect() as c:
        co = one(c, "SELECT id FROM companies WHERE slug=:s", s=SLUG)
        if not co:
            return False
        cid = co["id"]
        if one(c, "SELECT 1 AS x FROM audit_log WHERE company_id=:c AND action=:a", c=cid, a=MARK):
            return False                                                           # ya creado: idempotente
        if one(c, "SELECT 1 AS x FROM runs WHERE company_id=:c", c=cid):
            return False                                                           # la base ya se usó: no se le agregan datos de ejemplo
    pace, modelo = settings.step_delay, orch.provider
    settings.step_delay = 0                                                        # es el arranque: nadie mira el grafo todavía
    orch.provider = DeterministicProvider()                                        # el ejemplo es SIEMPRE el mismo, aunque la instancia use un modelo; se restaura abajo
    try:
        out = orch.start_chat(SLUG, ChatInput(MESSAGE, "Marta Rojas", "+56 9 5555 0101", None, "demo-example-" + uuid.uuid4().hex[:12]))
        limit, status = time.monotonic() + deadline, "running"
        while time.monotonic() < limit:
            with eng.connect() as c:
                status = one(c, "SELECT status FROM runs WHERE id=:i AND company_id=:c", i=out["run_id"], c=cid)["status"]
            if status != "running":
                break
            time.sleep(0.05)
    finally:
        settings.step_delay, orch.provider = pace, modelo
    with eng.begin() as c:
        pend = one(c, "SELECT 1 AS x FROM approvals WHERE company_id=:c AND status='pendiente'", c=cid)
        if status != "waiting_approval" or not pend:
            log.error("demo: el ejemplo no llegó a una decisión pendiente (estado %s); no se marca", status)
            return False
        audit(c, cid, "system", "demo_example", MARK, out["run_id"], "ok", "escenario A")                  # la marca va AL FINAL
    return True
