"""Default engine: explicit rules. No credentials, no network, clearly labelled 'deterministic' in every event."""
from __future__ import annotations

import re

from .base import Classification, MaterialNeed, Provider

RULES = [  # (regex, category, parts)
    (r"no enciende|no prende|placa|pantalla|c[oó]digo de error|error\s*e?\d", "reparacion_electronica", ["PLACA-INV"]),
    (r"no enfr[ií]a|gas|fuga|no congela|poco fr[ií]o", "reparacion_gas", ["GAS-R410"]),
    (r"ruido|ventilador|vibra|zumbido", "reparacion_ventilador", ["MOTOR-VENT"]),
    (r"instalar|instalaci[oó]n|nuevo equipo", "instalacion", []),
    (r"mantenci[oó]n|limpieza|preventiv", "mantencion", []),
]
# Motor Norte (taller) uses its own vocabulary but the same engine
RULES_MOTOR = [
    (r"frenos?|pastillas", "reparacion_frenos", ["PAST-FRENO"]),
    (r"aceite|mantenci[oó]n|cambio de aceite", "mantencion", ["ACEITE-5W30"]),
]
URGENT = re.compile(r"urgente|hoy|ahora|ya mismo|de inmediato|no puedo esperar")


class DeterministicProvider(Provider):
    name = "deterministic-demo-engine"
    mode = "deterministic"

    def classify(self, text: str, part_skus: list[str]) -> Classification:
        t = text.lower()
        urgency = "alta" if URGENT.search(t) else "media"
        for rx, cat, parts in RULES + RULES_MOTOR:
            if re.search(rx, t):
                return Classification(cat, urgency, [p for p in parts if p in part_skus] or parts, f"regla: /{rx[:30]}/", "deterministic")
        return Classification("diagnostico", urgency, [], "sin regla específica → visita de diagnóstico", "deterministic")

    def draft_customer_message(self, kind: str, ctx: dict) -> str:
        name = ctx.get("customer", "cliente").split(" ")[0]
        if kind == "visit_proposal":
            return (f"Hola {name}, preparamos una propuesta de visita para {ctx['slot']} con presupuesto estimado de ${ctx['total']:,.0f}. "
                    "Por favor confirma respondiendo CONFIRMO. Hasta entonces la hora NO está reservada.").replace(",", ".")
        if kind == "alternative":
            return (f"Hola {name}, entendemos que el presupuesto inicial no se ajusta. Te ofrecemos una alternativa: "
                    f"{ctx['alt']} por ${ctx['total']:,.0f}. ¿Te gustaría revisarla?").replace(",", ".")
        return f"Hola {name}, recibimos tu solicitud y la estamos revisando."

    def parse_material_need(self, text: str, catalog: list[dict], my_requests: list[dict]) -> MaterialNeed:
        t = text.lower()
        sku = next((c["sku"] for c in catalog if c["sku"].lower() in t), None)
        if not sku:  # match by catalog name words (e.g. "gas" → GAS-R410), unique match only
            hits = [c["sku"] for c in catalog if any(w in t.split() for w in re.findall(r"[a-záéíóúñ]{3,}", c["name"].lower()) if w in ("gas", "motor", "compresor", "placa", "pastillas", "aceite"))]
            sku = hits[0] if len(set(hits)) == 1 else None
        m = re.search(r"\bx\s?(\d{1,2})\b|(\d{1,2})\s*(?:unidades|u\b|cilindros|botellas|piezas)", t)
        words = {"un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4}
        qty = int(next(g for g in m.groups() if g)) if m else next((n for w, n in words.items() if re.search(rf"\b{w}\b", t)), 1)
        rid = (re.search(r"req_[0-9a-f]{12}", text) or [None])[0] if re.search(r"req_[0-9a-f]{12}", text) else None
        return MaterialNeed(sku, qty, rid, text[:200], "deterministic")
