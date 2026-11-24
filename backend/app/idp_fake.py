"""PROVEEDOR DE IDENTIDAD SIMULADO — SOLO PARA PRUEBAS (V0.7 P2, Issue #23). No abre ningún socket, no tiene ninguna ruta HTTP y sus direcciones usan el TLD reservado `.invalid` (RFC 6761).

Garantías de diseño (cada una con prueba en tests/test_v07_p2_pilot_guard.py):
  · NO hay configuración que lo active: solo se inyecta en código con `create_app(..., identity_provider=FakeProvider())`.
  · `AICOS_ENV=pilot` se NIEGA a arrancar con él (`is_fake = True`) antes de abrir la base.
  · Ningún proceso normal lo importa (api.py solo conoce la interfaz de identity.py).
Es ESTRICTO a propósito, para que probar contra él no sea una demostración falsa: códigos de un solo uso y de 60 s (un intento fallido también los quema), PKCE S256 y nonce verificados.
LO QUE NO ES: no equivale a MFA ni a verificación real de correo: informa lo que la prueba le dice."""
from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

from . import clock
from .identity import Identity, IdentityError, ProviderUnavailable, pkce_challenge

CODE_TTL_SECONDS = 60


class FakeProvider:
    name = "fake"
    is_fake = True

    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.outbox: list[str] = []          # «correos» que el proveedor real habría enviado: aquí solo se anotan
        self.disabled: set[str] = set()
        self.down = False                    # simula un proveedor caído
        self.enforce_disable = True          # False: simula un proveedor que sigue autenticando a una identidad desactivada (la base de la aplicación debe decidir igual)

    # ───────── el lado del proveedor (lo maneja la prueba) ─────────
    def account(self, email: str, *, subject: str | None = None, email_verified=True, name: str | None = None) -> str:
        subject = subject or "sub_" + secrets.token_hex(8)
        self.accounts[subject] = {"email": email, "email_verified": email_verified, "name": name, "last_auth": None}
        return subject

    def change_email(self, subject: str, email: str) -> None:
        self.accounts[subject]["email"] = email

    def login(self, authorize_url: str, subject: str, *, auth_age_seconds: float | None = 0, ignore_force: bool = False) -> dict:
        """La persona «inicia sesión en el proveedor»: devuelve lo que el navegador traería de vuelta (`code` y `state`). `auth_age_seconds` = hace cuánto se autenticó (negativo = en el futuro;
        None = el proveedor no informa `auth_time`). `ignore_force=True` simula un proveedor que ignora `prompt=login` y reutiliza su sesión previa."""
        qs = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        if qs.get("code_challenge_method") != "S256" or not qs.get("code_challenge") or not qs.get("state"):
            raise IdentityError("solicitud de autorización sin PKCE S256 o sin state")
        acct = self.accounts[subject]
        now = int(clock.utcnow().timestamp())
        if ignore_force and qs.get("prompt") == "login" and acct["last_auth"] is not None:
            auth_time = acct["last_auth"]
        elif auth_age_seconds is None:
            auth_time = acct["last_auth"] = None
        else:
            auth_time = acct["last_auth"] = now - int(auth_age_seconds)
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"subject": subject, "challenge": qs["code_challenge"], "nonce": qs.get("nonce"), "exp": clock.utcnow().timestamp() + CODE_TTL_SECONDS, "auth_time": auth_time}
        return {"code": code, "state": qs["state"]}

    # ───────── la interfaz estrecha ─────────
    def authorize_url(self, state: str, nonce: str, challenge: str, login_hint: str | None = None, force_reauth: bool = False) -> str:
        qs = {"client_id": "aicos-prueba", "response_type": "code", "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        if login_hint:
            qs["login_hint"] = login_hint
        if force_reauth:
            qs["prompt"] = "login"
        return "https://fake-idp.invalid/authorize?" + urlencode(qs)

    def exchange(self, code: str, verifier: str, nonce: str | None = None) -> Identity:
        if self.down:
            raise ProviderUnavailable("proveedor simulado caído")
        rec = self.codes.pop(code, None)                                   # un código se quema al PRIMER uso, también si el intento falla (RFC 6749 §4.1.2)
        if not rec or clock.utcnow().timestamp() > rec["exp"]:
            raise IdentityError("código desconocido o vencido")
        if pkce_challenge(verifier or "") != rec["challenge"]:
            raise IdentityError("verificador PKCE incorrecto")
        if nonce is not None and nonce != rec["nonce"]:
            raise IdentityError("nonce incorrecto")
        if self.enforce_disable and rec["subject"] in self.disabled:
            raise IdentityError("identidad desactivada")
        a = self.accounts[rec["subject"]]
        return Identity(provider=self.name, subject=rec["subject"], email=a["email"], email_verified=a["email_verified"], name=a["name"], auth_time=rec["auth_time"])

    def invite(self, email: str) -> None:
        self.outbox.append((email or "").strip().lower())

    def disable(self, subject: str) -> None:
        self.disabled.add(subject)

    def logout_url(self) -> str:
        return "https://fake-idp.invalid/logout"
