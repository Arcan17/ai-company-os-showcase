"""Fuente ÚNICA de la hora de pared (UTC) de la aplicación. Las pruebas la sustituyen por un reloj controlado (nada duerme): por eso TODO código que necesite «ahora» llama
`clock.utcnow()` (atributo del módulo, resuelto en cada llamada) y NUNCA `from .clock import utcnow` ni `datetime.now()` directamente. (El limitador público tiene su propia costura: ratelimit.clock.)"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
