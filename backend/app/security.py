"""Authentication primitives: argon2id password hashes, opaque DB-backed sessions, CSRF token, sign-in throttling.

The demo accounts are fictitious and flagged is_demo=1; the login screen says so explicitly.

Reused instead of written here: `argon2-cffi` (OWASP's first-choice hash). Written here on purpose (see docs/v06/ACCESO_Y_SESIONES.md): the CSRF
synchronizer token (it lives in the session row we already have) and the login throttle (it must be persistent, per-account AND per-address, and audited).
"""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from . import clock
from .config import settings
from .db import all_, now, one, q

# OWASP Password Storage Cheat Sheet: Argon2id with at least m=19 MiB, t=2, p=1. Measured ≈15 ms per hash on the dev Mac.
_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
TOUCH_EVERY_SECONDS = 60          # sliding-session writes are rate limited so SSE/polling does not turn every read into a write


# ---------------------------------------------------------------- passwords
def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def _verify_legacy_scrypt(pw: str, stored: str) -> bool:      # V0.2–V0.5 hashes ("scrypt$salt$dk", n=2^14): still accepted, upgraded at the next successful login
    _, salt, dk = stored.split("$")
    cand = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(cand.hex(), dk)


def verify_password(pw: str, stored: str) -> bool:
    try:
        if stored.startswith("$argon2"):
            return _ph.verify(stored, pw)
        return _verify_legacy_scrypt(pw, stored)
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored: str) -> bool:
    return not stored.startswith("$argon2") or _ph.check_needs_rehash(stored)


DUMMY_HASH = hash_password("aicos-dummy-password-for-constant-work")   # verified when the e-mail does not exist, so "unknown" costs the same as "known"


# ---------------------------------------------------------------- sessions
def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


AUTH_NOW = object()               # «la autenticación ocurre ahora» (contraseña); un valor ISO = lo que informó el proveedor; None = sin dato (nunca reciente)
FUTURE_SKEW_SECONDS = 60          # tolerancia de desfase de relojes: un `auth_time` más adelantado que esto no es «reciente», es sospechoso


def create_session(conn, user: dict, hours: int, auth_time=AUTH_NOW) -> str:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    n = now()
    exp = _iso(clock.utcnow() + timedelta(hours=hours))
    q(conn, "INSERT INTO sessions(token_hash,user_id,company_id,created_at,expires_at,csrf,last_seen_at,auth_time) VALUES(:h,:u,:c,:n,:e,:x,:n,:a)",
      h=_h(token), u=user["id"], c=user["company_id"], n=n, e=exp, x=csrf, a=n if auth_time is AUTH_NOW else auth_time)
    return token


def auth_fresh(s: dict, max_age_seconds: int | None = None) -> bool:
    """¿La última autenticación activa de la sesión es lo bastante reciente? Sin dato, ilegible, sin zona horaria o adelantada más de un minuto = NO."""
    raw = s.get("auth_time")
    if not raw:
        return False
    try:
        t = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    if t.tzinfo is None:
        return False
    age = (clock.utcnow() - t).total_seconds()
    return -FUTURE_SKEW_SECONDS <= age <= (max_age_seconds if max_age_seconds is not None else settings.reauth_max_age_seconds)


def set_auth_time(conn, token: str, iso: str) -> None:
    q(conn, "UPDATE sessions SET auth_time=:a WHERE token_hash=:h", a=iso, h=_h(token))


def session_hash(token: str) -> str:
    return _h(token)


def lookup_session(conn, token: str | None):
    """→ (session_row, user) or None. A session dies by absolute expiry OR by inactivity; a deactivated user has no session."""
    if not token:
        return None
    s = one(conn, "SELECT * FROM sessions WHERE token_hash=:h", h=_h(token))
    if not s or s["expires_at"] < now():
        return None
    idle_cut = _iso(clock.utcnow() - timedelta(minutes=settings.session_idle_minutes))
    if (s.get("last_seen_at") or s["created_at"]) < idle_cut:
        return None
    u = one(conn, "SELECT * FROM users WHERE id=:i AND company_id=:c AND COALESCE(active,1)=1", i=s["user_id"], c=s["company_id"])
    return (s, u) if u else None


def touch_due(s: dict) -> bool:
    last = s.get("last_seen_at") or s["created_at"]
    return last < _iso(clock.utcnow() - timedelta(seconds=TOUCH_EVERY_SECONDS))


def touch_session(conn, token: str) -> None:
    q(conn, "UPDATE sessions SET last_seen_at=:n WHERE token_hash=:h", n=now(), h=_h(token))


def csrf_for(conn, token: str) -> str:
    """The token of THIS session (created lazily for sessions that pre-date V0.6). Never a global or per-user value."""
    s = one(conn, "SELECT csrf FROM sessions WHERE token_hash=:h", h=_h(token))
    if s and s["csrf"]:
        return s["csrf"]
    tok = secrets.token_urlsafe(32)
    q(conn, "UPDATE sessions SET csrf=:x WHERE token_hash=:h AND csrf IS NULL", x=tok, h=_h(token))
    return one(conn, "SELECT csrf FROM sessions WHERE token_hash=:h", h=_h(token))["csrf"]


def delete_session(conn, token: str | None):
    if token:
        q(conn, "DELETE FROM sessions WHERE token_hash=:h", h=_h(token))


def revoke_user_sessions(conn, user_id: str, company_id: str) -> int:
    return q(conn, "DELETE FROM sessions WHERE user_id=:u AND company_id=:c", u=user_id, c=company_id).rowcount


def purge_expired(conn) -> None:
    n = now()
    q(conn, "DELETE FROM sessions WHERE expires_at<:n OR COALESCE(last_seen_at,created_at)<:i", n=n,
      i=_iso(clock.utcnow() - timedelta(minutes=settings.session_idle_minutes)))
    q(conn, "DELETE FROM login_attempts WHERE at<:o", o=_iso(clock.utcnow() - timedelta(days=1)))


# ---------------------------------------------------------------- sign-in throttling
def email_key(email: str) -> str:
    return (email or "").strip().lower()[:254]


def throttle_wait(conn, ekey: str, ip: str) -> int:
    """Seconds the caller must wait (0 = allowed). Three rolling-window counters, so one knob cannot be bypassed by changing the other axis:
    the (email, address) pair [cleared by that pair's own successful sign-in], the email from any address, the address across any email.
    Blocked attempts are NOT recorded, so hammering a locked account does not extend its lock."""
    win = settings.login_window_seconds
    n = clock.utcnow()
    since = _iso(n - timedelta(seconds=win))
    last_ok = (one(conn, "SELECT max(at) AS t FROM login_attempts WHERE ok=1 AND email_key=:e AND ip=:i", e=ekey, i=ip) or {}).get("t")
    waits = []
    for where, params, limit, floor in (
        ("email_key=:e AND ip=:i", {"e": ekey, "i": ip}, settings.login_max_pair, max(since, last_ok or since)),
        ("email_key=:e", {"e": ekey}, settings.login_max_email, since),
        ("ip=:i", {"i": ip}, settings.login_max_ip, since),
    ):
        rows = all_(conn, f"SELECT at FROM login_attempts WHERE ok=0 AND at>:s AND {where} ORDER BY at DESC LIMIT :n", s=floor, n=limit, **params)
        if len(rows) >= limit:   # the limit-th most recent failure is the one that must age out of the window before a new attempt is allowed
            waits.append(datetime.fromisoformat(rows[limit - 1]["at"]) + timedelta(seconds=win) - n)
    return max(1, math.ceil(max(waits).total_seconds())) if waits else 0


def record_attempt(conn, ekey: str, ip: str, ok: bool) -> None:
    q(conn, "INSERT INTO login_attempts(at,email_key,ip,ok) VALUES(:t,:e,:i,:o)", t=now(), e=ekey, i=ip, o=1 if ok else 0)
