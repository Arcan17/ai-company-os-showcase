"""V0.9 (spec 004, fase B) — demo PÚBLICA con un espacio aislado por visitante.

Cada visitante recibe su PROPIA aplicación con su PROPIA base de datos SQLite, copiada de una plantilla ya sembrada con datos ficticios: no comparten tablas, sesiones ni ejecuciones,
así que lo que uno haga (aprobar, anonimizar, crear solicitudes) jamás se ve en el espacio de otro. El visitante se identifica con una cookie firmada (HMAC) de 128 bits; sin ella, o con
una cookie falsa, se crea un espacio nuevo. Los espacios caducan por inactividad, hay un tope de espacios simultáneos y un límite de espacios nuevos por origen.

Solo se crea un espacio cuando un visitante llama a la API: la interfaz (`/` y `/assets`) y la salud (`/api/health`) NO consumen cupo (rastreadores y comprobaciones de salud no agotan la demo).
La demo pública NO tiene integraciones externas ni permite crear empresas, y se niega a arrancar en modo piloto o con la creación de empresas activa."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections import defaultdict, deque
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .config import settings
from .ratelimit import client_source

log = logging.getLogger("aicos.public_demo")
COOKIE = "aicos_visitor"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")


class PublicDemoConfigError(RuntimeError):
    pass


@dataclass
class Space:
    vid: str
    app: object
    stack: AsyncExitStack
    db_path: Path
    last_seen: float = field(default_factory=time.monotonic)
    active: int = 0


def _sig(secret: bytes, vid: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret, vid.encode(), hashlib.sha256).digest())[:22].decode()


class VisitorRouter:
    def __init__(self, data_dir: Path, *, secret: bytes | None = None, max_visitors: int = 20, idle_seconds: int = 1800,
                 new_per_source: int = 6, new_window: int = 600, sweep_seconds: float = 30.0, factory=None, static_dir: Path | None = None):
        if settings.env == "pilot":
            raise PublicDemoConfigError("La demo pública no puede ejecutarse con AICOS_ENV=pilot.")
        if settings.allow_company_creation:
            raise PublicDemoConfigError("La demo pública exige AICOS_ALLOW_COMPANY_CREATION=0.")
        if settings.llm_provider != "deterministic":
            raise PublicDemoConfigError("La demo pública solo usa el motor determinista (AICOS_LLM_PROVIDER=deterministic): sin costo ni riesgo de abuso de un modelo.")
        self.dir = Path(data_dir)
        self.secret = secret or os.environ.get("AICOS_VISITOR_SECRET", "").encode() or secrets.token_bytes(32)
        self.max, self.idle, self.new_per_source, self.new_window, self.sweep_every = max_visitors, idle_seconds, new_per_source, new_window, sweep_seconds
        if factory is None:
            from .api import create_app
            factory = create_app
        self.factory = factory
        self.spaces: dict[str, Space] = {}
        self._lock = asyncio.Lock()
        self._new_by_src: dict[str, deque] = defaultdict(deque)
        self._sweeper: asyncio.Task | None = None
        self.template: Path = self.dir / "template.db"
        sd = static_dir or settings.static_dir
        routes = []
        if sd.exists():
            if (sd / "assets").exists():
                routes.append(Mount("/assets", StaticFiles(directory=sd / "assets"), name="assets"))
            routes.append(Route("/", lambda request: FileResponse(sd / "index.html")))
        self.static = Starlette(routes=routes)

    # ───────────────────────── ciclo de vida ─────────────────────────
    async def startup(self) -> None:
        shutil.rmtree(self.dir / "visitors", ignore_errors=True)            # un reinicio descarta todos los espacios (son efímeros por diseño)
        (self.dir / "visitors").mkdir(parents=True, exist_ok=True)
        self.template.unlink(missing_ok=True)
        stack = AsyncExitStack()
        app = await asyncio.to_thread(self.factory, f"sqlite:///{self.template}")        # siembra los datos ficticios UNA vez
        await stack.enter_async_context(app.router.lifespan_context(app))
        await stack.aclose()
        app.state.eng.dispose()
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def shutdown(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for vid in list(self.spaces):
            await self._evict(vid)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_every)
            try:
                await self.sweep()
            except Exception:                                                          # el barrido jamás debe morir
                log.exception("fallo al barrer espacios de visitantes")

    async def sweep(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        old = [v for v, s in self.spaces.items() if s.active == 0 and now - s.last_seen > self.idle]
        for v in old:
            await self._evict(v)
        return len(old)

    async def _evict(self, vid: str) -> None:
        s = self.spaces.pop(vid, None)
        if not s:
            return
        try:
            await s.stack.aclose()
            s.app.state.eng.dispose()
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(s.db_path) + suffix).unlink(missing_ok=True)

    # ───────────────────────── espacios ─────────────────────────
    def _verify(self, raw: str | None) -> str | None:
        if not raw or "." not in raw:
            return None
        vid, sig = raw.split(".", 1)
        return vid if ID_RE.match(vid) and hmac.compare_digest(sig, _sig(self.secret, vid)) else None

    def _allow_new(self, source: str) -> bool:
        q, now = self._new_by_src[source], time.monotonic()
        while q and now - q[0] > self.new_window:
            q.popleft()
        if len(q) >= self.new_per_source:
            return False
        q.append(now)
        return True

    async def _create(self, source: str) -> Space | None | str:
        async with self._lock:
            if len(self.spaces) >= self.max:
                return "full"
            if not self._allow_new(source):
                return "rate"
            vid = secrets.token_urlsafe(16)
            path = self.dir / "visitors" / f"{vid}.db"
            shutil.copyfile(self.template, path)
            stack = AsyncExitStack()
            app = await asyncio.to_thread(self.factory, f"sqlite:///{path}")
            await stack.enter_async_context(app.router.lifespan_context(app))
            sp = Space(vid, app, stack, path)
            self.spaces[vid] = sp
            return sp

    # ───────────────────────── ASGI ─────────────────────────
    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        path = scope.get("path", "")
        if scope["type"] == "http" and scope["method"] in ("GET", "HEAD") and path == "/api/health":
            return await JSONResponse({"ok": True, "public_demo": True, "active": True})(scope, receive, send)
        if scope["type"] == "http" and scope["method"] == "GET" and path == "/api/public-demo/info":
            return await JSONResponse({"public_demo": True, "idle_minutes": self.idle // 60, "capacity": self.max, "active": len(self.spaces)})(scope, receive, send)
        if not path.startswith("/api"):
            return await self.static(scope, receive, send)            # la interfaz no consume cupo
        cookies = {}
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    n, _, val = part.strip().partition("=")
                    cookies[n] = val
        vid = self._verify(cookies.get(COOKIE))
        space, set_cookie = self.spaces.get(vid) if vid else None, False
        if space is None:
            made = await self._create(client_source(scope))
            if made == "full":
                return await self._refuse(scope, receive, send, "La demo está llena en este momento. Vuelve en unos minutos.", 503)
            if made == "rate":
                return await self._refuse(scope, receive, send, "Demasiados espacios nuevos desde tu red. Espera unos minutos.", 429)
            space, set_cookie = made, True
        space.last_seen = time.monotonic()
        space.active += 1
        secure = scope.get("scheme") == "https"

        async def send_wrap(msg):
            if set_cookie and msg["type"] == "http.response.start":
                val = f"{space.vid}.{_sig(self.secret, space.vid)}"
                msg = {**msg, "headers": [*msg.get("headers", []), (b"set-cookie", f"{COOKIE}={val}; Path=/; HttpOnly; SameSite=Lax; Max-Age={self.idle}{'; Secure' if secure else ''}".encode())]}
            await send(msg)
        try:
            await space.app(scope, receive, send_wrap)
        finally:
            space.active -= 1
            space.last_seen = time.monotonic()

    async def _refuse(self, scope, receive, send, text: str, status: int):
        return await JSONResponse({"detail": text}, status_code=status, headers={"Retry-After": "30"})(scope, receive, send)

    async def _lifespan(self, receive, send):
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                try:
                    await self.startup()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as e:                                                  # configuración insegura o plantilla imposible: salida limpia
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
                    return
            elif m["type"] == "lifespan.shutdown":
                await self.shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return


def build_public_app() -> VisitorRouter:
    return VisitorRouter(Path(os.environ.get("AICOS_PUBLIC_DEMO_DIR", "/data/public-demo")),
                         max_visitors=int(os.environ.get("AICOS_PUBLIC_MAX_VISITORS", "20")), idle_seconds=int(os.environ.get("AICOS_PUBLIC_IDLE_SECONDS", "1800")),
                         new_per_source=int(os.environ.get("AICOS_PUBLIC_NEW_PER_SOURCE", "6")))
