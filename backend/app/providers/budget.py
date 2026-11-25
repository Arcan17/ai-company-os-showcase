"""V0.10 (spec 005, fase 2) — CONTROL DE GASTO de los modelos de pago.

Regla: el tope (día y mes) se verifica ANTES de llamar, contra la suma de lo gastado y de lo RESERVADO por llamadas en curso, y se reserva el costo del PEOR caso de la llamada (entrada estimada por exceso +
`max_tokens` de salida a precio de salida). La reserva y la comprobación son UNA sola sentencia (atómica): dos llamadas simultáneas no pueden pasarse del tope juntas. Al terminar, la reserva se
reemplaza por el costo real (tokens de `usage`). Una reserva que nunca se liquida (el proceso murió) sigue contando: el error es siempre hacia gastar MENOS.
No guarda textos ni claves: solo fecha, proveedor, modelo, tokens, costo y estado."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .. import clock
from ..config import settings
from .base import ProviderConfigError

# USD por millón de tokens (entrada, salida): precios de lista conocidos de los modelos de Anthropic al 2026-11-03 (tabla de la documentación oficial).
# El operador puede sobrescribirlos con AICOS_ANTHROPIC_PRICE_IN/OUT; un modelo que no está aquí y no trae precios configurados NO arranca (no se puede acotar un gasto que no se sabe calcular).
KNOWN_PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50), "claude-fable-5-2": (8.0, 40.0),
}
CHARS_PER_TOKEN_WORST = 2.0      # estimación por EXCESO de tokens de entrada (el español real ronda 3–4 caracteres por token)


def resolve_prices(model: str) -> tuple[float, float]:
    if settings.anthropic_price_in and settings.anthropic_price_out:
        try:
            pin, pout = float(settings.anthropic_price_in), float(settings.anthropic_price_out)
        except ValueError:
            raise ProviderConfigError("AICOS_ANTHROPIC_PRICE_IN/OUT deben ser números (USD por millón de tokens).")
        if pin <= 0 or pout <= 0:
            raise ProviderConfigError("AICOS_ANTHROPIC_PRICE_IN/OUT deben ser positivos.")
        return pin, pout
    if model in KNOWN_PRICES:
        return KNOWN_PRICES[model]
    raise ProviderConfigError(f"No se conoce el precio del modelo «{model}»: define AICOS_ANTHROPIC_PRICE_IN y AICOS_ANTHROPIC_PRICE_OUT (USD por millón de tokens). Sin precios no se puede acotar el gasto.")


class Budget:
    def __init__(self, eng: Engine, provider: str, model: str, price_in: float, price_out: float, day_usd: float, month_usd: float):
        if day_usd <= 0 or month_usd <= 0 or day_usd > month_usd:
            raise ProviderConfigError("Los topes de gasto deben ser positivos y el diario no puede superar al mensual.")
        self.eng, self.provider, self.model, self.pin, self.pout, self.day_cap, self.month_cap = eng, provider, model, price_in, price_out, day_usd, month_usd

    def worst_case(self, prompt_chars: int, max_tokens: int) -> float:
        """Costo máximo posible de UNA llamada: entrada estimada por exceso + toda la salida permitida."""
        return (prompt_chars / CHARS_PER_TOKEN_WORST) * self.pin / 1e6 + max_tokens * self.pout / 1e6

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens * self.pin / 1e6 + output_tokens * self.pout / 1e6

    def _keys(self) -> tuple[str, str, str]:
        now = clock.utcnow()
        return now.isoformat(timespec="milliseconds"), now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def reserve(self, worst_usd: float) -> int | None:
        """Reserva el peor caso si (gastado + reservado + peor caso) cabe en el tope del día Y del mes; devuelve el id de la reserva o None si NO cabe (no se debe llamar)."""
        ts, day, month = self._keys()
        with self.eng.begin() as c:
            r = c.execute(text("""INSERT INTO llm_usage(ts,day,month,provider,model,input_tokens,output_tokens,cost_usd,status)
                                  SELECT :ts,:d,:m,:p,:mo,0,0,:w,'reserved'
                                  WHERE (SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE day=:d)+:w <= :dc
                                    AND (SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE month=:m)+:w <= :mc
                                  RETURNING id"""),
                          {"ts": ts, "d": day, "m": month, "p": self.provider, "mo": self.model, "w": worst_usd, "dc": self.day_cap, "mc": self.month_cap})
            row = r.first()
            return int(row[0]) if row else None

    def settle(self, rid: int, input_tokens: int, output_tokens: int, status: str) -> float:
        """Cambia la reserva por el costo REAL (0 si la llamada falló antes de gastar tokens)."""
        cost = self.cost(input_tokens, output_tokens)
        with self.eng.begin() as c:
            c.execute(text("UPDATE llm_usage SET input_tokens=:i,output_tokens=:o,cost_usd=:c,status=:s WHERE id=:id"), {"i": input_tokens, "o": output_tokens, "c": cost, "s": status, "id": rid})
        return cost

    def spent(self) -> dict:
        _, day, month = self._keys()
        with self.eng.connect() as c:
            d = c.execute(text("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE day=:d"), {"d": day}).scalar()
            m = c.execute(text("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE month=:m"), {"m": month}).scalar()
        return {"day": float(d), "month": float(m), "day_cap": self.day_cap, "month_cap": self.month_cap}
