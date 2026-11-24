"""V0.7 P2 (Issue #23) — la interfaz ESTRECHA del proveedor de identidad y sus tipos. El proveedor solo responde «quién es» (subject, correo verificado, auth_time): el ROL y la EMPRESA salen
siempre de nuestra base (docs/v07/P0_ARQUITECTURA_Y_PLAN.md §3). Los adaptadores son intercambiables: `FakeProvider` (app/idp_fake.py: SOLO pruebas, se inyecta en código, no hay configuración que lo active),
`WorkOSProvider` (P3, no existe todavía) y a futuro un `OidcProvider`. Este módulo no importa ningún adaptador."""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol


class IdentityError(Exception):
    """El proveedor rechazó o no pudo validar lo presentado. El mensaje es para el registro técnico: NUNCA se muestra a la persona (el navegador recibe siempre la misma respuesta genérica)."""


class ProviderUnavailable(IdentityError):
    """El proveedor no respondió. Falla CERRADO: no se crea ninguna sesión."""


@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    email: str
    email_verified: object          # lo declara el proveedor; la aplicación SOLO acepta el booleano `True` (nada de «true», 1, None…)
    name: str | None
    auth_time: int | None           # segundos epoch UTC de la última autenticación ACTIVA de la persona en el proveedor (None = no lo informó)


class IdentityProvider(Protocol):
    name: str
    is_fake: bool                   # un proveedor sin `is_fake = False` explícito se trata como simulado (falla cerrado en el piloto)

    def authorize_url(self, state: str, nonce: str, challenge: str, login_hint: str | None = None, force_reauth: bool = False) -> str: ...

    def exchange(self, code: str, verifier: str, nonce: str | None = None) -> Identity: ...

    def invite(self, email: str) -> None: ...

    def disable(self, subject: str) -> None: ...

    def logout_url(self) -> str: ...


def pkce_pair() -> tuple[str, str]:
    """(verificador, desafío S256) de RFC 7636: el verificador son 43 caracteres base64url (32 bytes aleatorios)."""
    verifier = secrets.token_urlsafe(32)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
