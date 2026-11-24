"""V0.6 P4 (Issue #13) — persistent, per-company rate limits for the PUBLIC customer channel (`/api/public/*`).

The public channel has no session and no CSRF by design, so an anonymous caller could start unlimited engine runs, read conversations at will and guess the 24-bit visit
confirmation code. The login throttle (security.py) does not cover it. Full rationale, numbers and limits: docs/v06/LIMITES_CANAL_PUBLICO.md.

WRITTEN HERE, ON PURPOSE (no library): `limits`/`slowapi` keep counters in memory or Redis; this prototype is one SQLite file shared by several processes (active + standby), and the
limit must survive a restart and be shared by every process. What IS reused: SQLite's single-writer lock and `INSERT … ON CONFLICT DO UPDATE … WHERE` (SQLite ≥ 3.24).

Algorithm — GCRA (a token bucket kept as ONE number per bucket, `tat` = theoretical arrival time):
    rule "N/W":  T = W/N seconds per request, tolerance tau = (N-1)·T  → a burst of N, then one more every T seconds.
    take:        base = max(min(tat, now+W), now);  allowed iff base <= now + tau;  then tat = base + T.
The whole decision is ONE conditional upsert, so «check» and «spend» cannot be separated by a race, between threads or between processes. A chain of buckets (source, company…)
is spent inside ONE write transaction: all or none. A refused request spends nothing (it does not extend a lock, and cannot be used to exhaust someone else's budget).
Failed lookups (`outbox.miss`) are spent BEFORE the lookup and refunded when it finds something — so once the budget is gone a valid and an invalid token answer the same (no oracle).

Source identity (who is «the same caller»): the TCP peer address, with IPv6 collapsed to its /64 and IPv4-mapped IPv6 to IPv4. `X-Forwarded-For` is believed ONLY when the peer is
a proxy listed in AICOS_TRUSTED_PROXIES, and then the right-most address that is not itself a trusted proxy wins (what a client prepends on the left is ignored). Nothing else
(`Forwarded`, `X-Real-IP`, `True-Client-IP`, …) is ever read. Addresses are never stored: bucket names are HMAC-SHA256 under a per-database random key.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from .config import settings

log = logging.getLogger("aicos.ratelimit")
clock = time.time                      # the single time source of the limiter (a seam: windows are exercised by moving it, never by sleeping or patching time globally)

PURGE_EVERY = 30.0                     # seconds (of `clock`) between sweeps of expired buckets
ALERT_EVERY = 60.0                     # at most one log line per (kind, company) per this many wall-clock seconds
KINDS = {"chat.src": "rl_chat_src", "chat.co": "rl_chat_co", "outbox.src": "rl_outbox_src", "outbox.co": "rl_outbox_co", "outbox.miss": "rl_outbox_miss",
         "confirm.src": "rl_confirm_src", "confirm.target": "rl_confirm_target", "confirm.co": "rl_confirm_co", "unknown.src": "rl_unknown_src", "unknown.all": "rl_unknown_all",
         "idp.invite.co": "rl_idp_invite_co", "idp.start.src": "rl_idp_start_src", "idp.start.all": "rl_idp_start_all"}
ROUTES = {("POST", "chat"): "chat", ("GET", "outbox"): "outbox", ("POST", "visits/confirm"): "confirm"}
PUBLIC = re.compile(r"^/api/public/([^/]{1,64})/(chat|outbox|visits/confirm)$")


# ------------------------------------------------------------------ rules
@dataclass(frozen=True)
class Rule:
    limit: int
    window: float

    @property
    def T(self) -> float:
        return self.window / self.limit

    @property
    def tau(self) -> float:
        return (self.limit - 1) * self.T


@lru_cache(maxsize=512)
def parse_rule(value: str, name: str) -> Rule:
    m = re.fullmatch(r"\s*(\d{1,6})\s*/\s*(\d{1,6})\s*", value or "")
    if not m or int(m.group(1)) < 1 or not 1 <= int(m.group(2)) <= 86400:
        raise ValueError(f"{name}={value!r} no es una regla válida: se espera «<peticiones>/<ventana en segundos>» (p. ej. 20/600; peticiones ≥ 1, ventana 1–86400)")
    return Rule(int(m.group(1)), float(m.group(2)))


def rule_of(kind: str) -> Rule:
    attr = KINDS[kind]
    return parse_rule(getattr(settings, attr), "AICOS_" + attr.upper())


@lru_cache(maxsize=64)
def _proxies(spec: tuple) -> tuple:
    try:
        return tuple(ipaddress.ip_network(x, strict=False) for x in spec)
    except ValueError as e:
        raise ValueError(f"AICOS_TRUSTED_PROXIES={','.join(spec)!r} contiene una dirección o red inválida: {e}") from None


def validate() -> None:
    """Called at startup: a bad limit or proxy list must stop the server, never silently fall back to something looser."""
    for kind in KINDS:
        rule_of(kind)
    _proxies(tuple(settings.trusted_proxies))


# ------------------------------------------------------------------ who is calling
def _ip(raw: str):
    raw = (raw or "").strip().strip("[]")
    if raw.count(":") == 1 and "." in raw:                       # 1.2.3.4:5678
        raw = raw.split(":")[0]
    try:
        return ipaddress.ip_address(raw.split("%")[0])
    except ValueError:
        return None


def _canon(ip) -> str:
    if ip.version == 6:
        if ip.ipv4_mapped:
            return "v4:" + str(ip.ipv4_mapped)
        return "v6:" + str(ipaddress.IPv6Network((ip, 64), strict=False))     # one customer controls a whole /64: rotating inside it must not mint «new» callers
    return "v4:" + str(ip)


def _in(ip, nets) -> bool:
    ip = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip
    return any(ip.version == n.version and ip in n for n in nets)


_warned = {"xff": False}


def _resolve(scope):
    """→ (address object or None, text). The address believed to be the caller: the TCP peer, unless the peer is a configured trusted proxy."""
    peer = scope.get("client")
    host = peer[0] if peer else "-"
    ip = _ip(host)
    if ip is None:
        return None, str(host)[:64]                              # not an address (an in-process client, a unix socket): one shared source, never a header
    ip = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip
    xff = ",".join(v.decode("latin-1") for k, v in scope.get("headers", ()) if k == b"x-forwarded-for")
    nets = _proxies(tuple(settings.trusted_proxies))
    if xff and nets and _in(ip, nets):
        for part in reversed([p for p in xff.split(",") if p.strip()]):
            hop = _ip(part)
            if hop is None:
                break                                            # garbage where the proxy chain should be: do not guess, keep the proxy as the source
            hop = hop.ipv4_mapped if hop.version == 6 and hop.ipv4_mapped else hop
            if not _in(hop, nets):
                return hop, str(hop)
    elif xff and not _warned["xff"] and (ip.is_loopback or ip.is_private):
        _warned["xff"] = True
        log.warning("X-Forwarded-For recibida de un par local o privado que no está en AICOS_TRUSTED_PROXIES: se IGNORA. Si hay un proxy inverso delante, añádelo a "
                    "AICOS_TRUSTED_PROXIES y arranca uvicorn con --no-proxy-headers; si no, todos los visitantes compartirán un solo presupuesto.")
    return ip, str(ip)


def client_address(scope) -> str:
    """The caller as plain text (sign-in throttle, audit lines). Same trust rules as the public channel: uvicorn's own proxy handling is switched OFF everywhere."""
    return _resolve(scope)[1]


def client_source(scope) -> str:
    """The caller as a bucket subject (IPv6 collapsed to its /64). Only ever stored hashed."""
    ip, text = _resolve(scope)
    return "x:" + text.lower() if ip is None else _canon(ip)


# ------------------------------------------------------------------ the store
@dataclass(frozen=True)
class Bucket:
    kind: str
    company: str | None = None       # company id (None for the «unknown company» buckets)
    subject: str = ""                # source, request id… («» = company-wide). Only ever hashed.
    slug: str = ""                   # for alert lines only; never stored


@dataclass(frozen=True)
class Ctx:
    source: str
    company_id: str | None
    slug: str
    route: str | None


class _Refused(Exception):
    pass


UPSERT = text("""INSERT INTO rate_limits(bucket,kind,company_id,tat) VALUES(:b,:k,:c,:now+:T)
                 ON CONFLICT(bucket) DO UPDATE SET tat=MAX(MIN(tat,:now+:win),:now)+:T WHERE MAX(MIN(tat,:now+:win),:now)<=:now+:tau""")


def _wait(rule: Rule, tat, now: float) -> float:
    if tat is None:
        return 0.0
    return max(0.0, max(min(tat, now + rule.window), now) - now - rule.tau)


class Limiter:
    def __init__(self, eng):
        self.eng = eng
        self._key = None
        self._lock = threading.Lock()
        self._last_purge = 0.0
        self._alerts: dict = {}

    # ---- keyed hashing: the database never sees an address
    def key(self) -> bytes:
        if self._key is None:
            with self._lock:
                if self._key is None:
                    with self.eng.begin() as c:                  # INSERT OR IGNORE first: two processes starting together end up reading the SAME key
                        c.execute(text("INSERT OR IGNORE INTO rate_limit_meta(name,value) VALUES('key',:v)"), {"v": secrets.token_hex(32)})
                        self._key = bytes.fromhex(c.execute(text("SELECT value FROM rate_limit_meta WHERE name='key'")).scalar())
        return self._key

    def name(self, b: Bucket) -> str:
        return hmac.new(self.key(), f"{b.kind}\x00{b.company or '-'}\x00{b.subject}".encode(), hashlib.sha256).hexdigest()[:32]

    # ---- decisions
    def _waits(self, buckets, names, now):
        with self.eng.connect() as c:
            ph = ",".join(f":b{i}" for i in range(len(names)))
            st = dict(c.execute(text(f"SELECT bucket,tat FROM rate_limits WHERE bucket IN ({ph})"), {f"b{i}": n for i, n in enumerate(names)}).fetchall())
        return [_wait(rule_of(b.kind), st.get(n), now) for b, n in zip(buckets, names)]

    def take(self, buckets: list) -> tuple[bool, int]:
        """Spend ONE request from every bucket, or from none. → (allowed, seconds to wait when refused)."""
        now = clock()
        names = [self.name(b) for b in buckets]
        waits = self._waits(buckets, names, now)               # read-only first: a flood of refused requests never takes the write lock
        if any(w > 0 for w in waits):
            self._alert([b for b, w in zip(buckets, waits) if w > 0])
            return False, self._secs(buckets, waits)
        refused = []
        try:
            with self.eng.begin() as c:                        # one write transaction for the whole chain: rolled back as a unit if any bucket refuses
                for b, n in zip(buckets, names):
                    r = rule_of(b.kind)
                    if c.execute(UPSERT, {"b": n, "k": b.kind, "c": b.company, "now": now, "T": r.T, "win": r.window, "tau": r.tau}).rowcount != 1:
                        refused = [b]
                        raise _Refused()
        except _Refused:
            waits = self._waits(buckets, names, now)           # someone else spent the last token between our read and our write: report the real wait
            self._alert(refused)
            return False, self._secs(buckets, waits)
        self._sweep(now)
        return True, 0

    @staticmethod
    def _secs(buckets, waits) -> int:
        worst = max((math.ceil(w) for w in waits), default=1)
        return max(1, min(worst or 1, int(max(rule_of(b.kind).window for b in buckets))))

    def refund(self, b: Bucket) -> None:
        """Give back the token of a lookup that found something. Best effort: failing to refund costs the caller one token, it must never turn a valid read into an error."""
        try:
            self._refund(b)
        except Exception:
            log.exception("no se pudo devolver una ficha del límite público")

    def _refund(self, b: Bucket) -> None:
        r = rule_of(b.kind)
        with self.eng.begin() as c:
            c.execute(text("UPDATE rate_limits SET tat=MAX(tat-:T,:now) WHERE bucket=:b"), {"T": r.T, "now": clock(), "b": self.name(b)})

    def _sweep(self, now: float) -> None:
        """Housekeeping after an accepted request. Best effort: the request already spent its tokens, a failed sweep must not fail it (the next one retries)."""
        if abs(now - self._last_purge) < PURGE_EVERY:
            return
        self._last_purge = now
        try:
            self._purge(now)
        except Exception:                                      # retried after PURGE_EVERY like any other sweep: a failing disk is not hammered on every request
            log.exception("no se pudo purgar el estado vencido del límite público")

    def _purge(self, now: float) -> None:
        with self.eng.begin() as c:                            # a bucket whose tat is in the past is a full bucket: the same as no row
            c.execute(text("DELETE FROM rate_limits WHERE tat<=:n"), {"n": now})

    def _alert(self, refused) -> None:
        """One line per (kind, company) per minute: enough for an alert, no address and no per-request noise (and no extra database writes while under attack)."""
        t = time.monotonic()
        for b in refused:
            k = (b.kind, b.slug or "-")
            with self._lock:
                last, quiet = self._alerts.get(k, (None, 0))
                if last is None or t - last >= ALERT_EVERY:
                    self._alerts[k] = (t, 0)
                    log.warning("límite público excedido: kind=%s empresa=%s (rechazos suprimidos desde el aviso anterior: %d)", b.kind, b.slug or "-", quiet)
                else:
                    self._alerts[k] = (last, quiet + 1)

    # ---- admission of a public request (called from the middleware, in a worker thread)
    def admit(self, method: str, path: str, scope) -> tuple[Ctx, bool, int]:
        src = client_source(scope)
        m = PUBLIC.match(path)
        route = ROUTES.get((method, m.group(2))) if m else None
        slug = m.group(1) if m else ""
        co = None
        if route:
            with self.eng.connect() as c:
                co = c.execute(text("SELECT id FROM companies WHERE slug=:s"), {"s": slug}).scalar()
        if route and co:
            chain = [Bucket(f"{route}.src", co, src, slug), Bucket(f"{route}.co", co, "", slug)]
        else:                                                  # unknown company or unknown public path: shared, bounded buckets (an attacker-chosen slug cannot mint buckets)
            route, co = None, None
            chain = [Bucket("unknown.src", None, src), Bucket("unknown.all", None, "")]
        ok, wait = self.take(chain)
        return Ctx(src, co, slug, route), ok, wait


def message(wait: int) -> str:
    return f"Demasiadas solicitudes al canal público. Espera {wait} s antes de volver a intentarlo."


def ctx_of(scope) -> Ctx:
    try:
        return scope["aicos.rl"]
    except KeyError:
        raise RuntimeError("PublicRateLimit no está instalado delante de esta ruta pública") from None


def miss_bucket(scope) -> Bucket | None:
    c = ctx_of(scope)
    return Bucket("outbox.miss", c.company_id, c.source, c.slug) if c.company_id else None


def target_bucket(scope, request_id: str) -> Bucket | None:
    c = ctx_of(scope)
    return Bucket("confirm.target", c.company_id, request_id, c.slug) if c.company_id else None


# ------------------------------------------------------------------ ASGI middleware
async def _reply(send, status: int, detail: str, retry: int) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"retry-after", str(retry).encode()),
                                                                              (b"cache-control", b"no-store"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class PublicRateLimit:
    """Innermost of the three gates:  CsrfMiddleware → ActiveGate → PublicRateLimit → routes.
    Behind the standby gate on purpose: a write a standby refuses (503) never spends budget. Pure ASGI, so a refused request's body is never read."""

    def __init__(self, app, limiter: Limiter):
        self.app, self.limiter = app, limiter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/public/"):
            return await self.app(scope, receive, send)
        try:
            ctx, ok, wait = await run_in_threadpool(self.limiter.admit, scope["method"], scope["path"], scope)
        except Exception:                                      # fail CLOSED: a limiter that cannot answer must not become a way around the limit
            log.exception("el limitador del canal público falló; se rechaza la petición")
            return await _reply(send, 503, "Canal público no disponible momentáneamente. Reintenta en unos segundos.", 5)
        if not ok:
            return await _reply(send, 429, message(wait), wait)
        scope["aicos.rl"] = ctx
        return await self.app(scope, receive, send)


IDP_START_PATH = "/api/auth/idp/start"
IDP_START_DETAIL = "Demasiadas solicitudes de acceso desde este origen. Espera unos minutos antes de volver a intentarlo."


class IdpStartLimit:
    """V0.7 P2.5 (revisión del PR #26): el límite de `POST /api/auth/idp/start` vive a nivel ASGI, ANTES de que nadie lea o analice el cuerpo. Un límite puesto como dependencia de FastAPI no cobraba las
    peticiones con JSON sintácticamente inválido (FastAPI las rechaza antes de resolver dependencias): quedaban sin límite. Solo POST y solo ese path exacto; solo se instala si hay proveedor de identidad.
    Orden: CsrfMiddleware → ActiveGate → PublicRateLimit/IdpStartLimit → rutas: un 403 de CSRF o un 503 de espera nunca llegan aquí y no gastan cupo. Cupo POR ORIGEN y GLOBAL, persistentes (rate_limits)."""

    def __init__(self, app, limiter: Limiter):
        self.app, self.limiter = app, limiter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != IDP_START_PATH:
            return await self.app(scope, receive, send)
        chain = [Bucket("idp.start.src", None, client_source(scope)), Bucket("idp.start.all", None, "")]
        try:
            ok, wait = await run_in_threadpool(self.limiter.take, chain)
        except Exception:                                      # fail CLOSED: un limitador que no puede responder no puede convertirse en una forma de saltarse el límite
            log.exception("el limitador de idp/start falló; se rechaza la petición")
            return await _reply(send, 503, "Acceso no disponible momentáneamente. Reintenta en unos segundos.", 5)
        if not ok:
            return await _reply(send, 429, IDP_START_DETAIL, wait)
        return await self.app(scope, receive, send)
