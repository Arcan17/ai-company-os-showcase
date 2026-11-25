"""Provider interface (pattern borrowed from AgentDesk: business code depends only on this abstraction).

A provider only PROPOSES structured decisions (classification, message draft). It never calls tools or
touches the DB: the engine validates what it returns and executes actions under the acting agent's permissions.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

CATEGORIES = ["reparacion_gas", "reparacion_electronica", "reparacion_ventilador", "instalacion", "mantencion", "diagnostico"]
URGENCIES = ["alta", "media", "baja"]


@dataclass
class Classification:
    category: str
    urgency: str
    parts: list[str] = field(default_factory=list)
    rationale: str = ""
    source: str = "deterministic"  # deterministic | llm:<model>


class Provider(ABC):
    name = "base"
    mode = "deterministic"  # label shown in the UI: deterministic | llm

    @abstractmethod
    def classify(self, text: str, part_skus: list[str]) -> Classification: ...

    @abstractmethod
    def draft_customer_message(self, kind: str, ctx: dict) -> str: ...

    @abstractmethod
    def parse_material_need(self, text: str, catalog: list[dict], my_requests: list[dict]) -> MaterialNeed: ...

    def propose_plan(self, text: str, open_requests: list[dict]) -> "PlanProposal | None":
        """LLM providers may propose a plan; the deterministic provider returns None and the engine's own rules are used."""
        return None


class ProviderConfigError(RuntimeError):
    """Configuración inválida o insegura de un proveedor de IA: el servidor se niega a arrancar (salida limpia, sin traza)."""


_LABEL_KEEP = re.compile(r"[^\w .,'\-áéíóúüñÁÉÍÓÚÜÑ]", re.UNICODE)


def safe_label(value, limit: int = 40) -> str:
    """Un nombre que viene de un CLIENTE (texto libre de un tercero) antes de mostrarlo a un modelo: solo letras, números y puntuación simple, una línea y como máximo `limit` caracteres.
    No es la defensa (la defensa es el resultado de vocabulario cerrado + el validador); es para no darle al modelo un canal cómodo de 120 caracteres con símbolos, saltos de línea o delimitadores."""
    t = _LABEL_KEEP.sub(" ", str(value or ""))
    return " ".join(t.split())[:limit]


def minimal_requests(open_requests: list[dict]) -> list[dict]:
    """Lo ÚNICO que un modelo ve de las solicitudes abiertas: id, nombre del cliente (saneado) y categoría. Jamás la descripción ni los mensajes del cliente."""
    return [{"id": str(r.get("id", ""))[:40], "customer": safe_label(r.get("customer")), "category": str(r.get("category") or "")[:40]} for r in open_requests]


UNSUPPORTED = ["comprar", "pagar", "enviar_mensaje_externo", "agendar_o_reservar", "aprobar", "modificar_stock", "eliminar_datos", "otro"]


@dataclass
class PlanProposal:
    """What a provider may SUGGEST for an owner's instruction. Closed vocabulary only: the engine rebuilds the real plan from these
    flags after validating each one against the text and the company's data. A provider never names users, tools or arguments."""
    review_pending: bool = False
    admin_verify_materials: bool = False
    technician_confirm: bool = False
    notify_owner: bool = False
    request_id: str | None = None
    unsupported: list[str] = field(default_factory=list)
    source: str = "deterministic"


@dataclass
class MaterialNeed:
    sku: str | None
    qty: int = 1
    request_id: str | None = None
    reason: str = ""
    source: str = "deterministic"


def _grounded(sku: str, catalog: list[dict], text: str) -> bool:
    """An LLM pick must be supported by the technician's own words: the SKU itself or a distinctive word of the part's name."""
    t = text.lower()
    part = next((c for c in catalog if c["sku"] == sku), None)
    if not part:
        return False
    if sku.lower() in t:
        return True
    stop = {"para", "carga", "juego", "unidad", "exterior", "original", "compatible", "split", "btu"}
    words = [w for w in re.findall(r"[a-záéíóúñ]{3,}", part["name"].lower()) if w not in stop]
    stems = {w[:5] for w in words}
    return any(tok[:5] in stems for tok in re.findall(r"[a-záéíóúñ]{3,}", t))


def validate_material_need(need: MaterialNeed, catalog: list[dict], my_requests: list[dict], text: str | None = None) -> MaterialNeed | None:
    """Engine-side verification of ANY provider output: sku must be in the company catalog, qty 1..10, request must be one of the
    technician's own open orders. Returns None when invalid (caller falls back to the deterministic parser)."""
    skus = {c["sku"] for c in catalog}
    ids = {r["id"] for r in my_requests}
    if need.sku not in skus or not (1 <= int(need.qty) <= 10):
        return None
    if text is not None and need.source.startswith("llm") and not _grounded(need.sku, catalog, text):
        return None  # the model picked a catalog item the technician never mentioned → reject (no invented materials)
    rid = need.request_id if need.request_id in ids else (next(iter(ids)) if len(ids) == 1 else None)
    if rid is None:
        return None
    return MaterialNeed(need.sku, int(need.qty), rid, (need.reason or "")[:200], need.source)
