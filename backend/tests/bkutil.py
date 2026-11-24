"""Utilidades compartidas por las pruebas de la rutina de respaldos (Issue #15). Todo se hace sobre bases TEMPORALES de pytest: jamás sobre aicos.db ni demo_comercial.sqlite3."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.seed import DEMO_PASSWORD

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent


def make_live(tmp_path, name: str = "aicos_live.db") -> Path:
    """A real application database (WAL, seeded, one open session) the way the server leaves it."""
    app = create_app(f"sqlite:///{tmp_path / name}")
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": DEMO_PASSWORD}).status_code == 200
    app.state.eng.dispose()
    return tmp_path / name


def write_rows(path, n: int = 1) -> None:
    """What the running server does all the time: committed writes (it also makes consecutive backups differ in content)."""
    con = sqlite3.connect(path, timeout=30)
    cid = con.execute("SELECT id FROM companies LIMIT 1").fetchone()[0]
    for i in range(n):
        con.execute("INSERT INTO audit_log(company_id,ts,actor_type,actor_id,action,resource,outcome,detail) VALUES(?,datetime('now'),'system','t','test.write','-','ok',?)", (cid, str(i)))
        con.commit()
    con.close()


def bloat(path, megabytes: int) -> None:
    """Make the database big (incompressible blobs) so a run takes a visible amount of time and a size limit can be hit for real."""
    con = sqlite3.connect(path, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS bk_filler(id INTEGER PRIMARY KEY, blob BLOB)")
    for _ in range(megabytes):
        con.execute("INSERT INTO bk_filler(blob) VALUES (?)", (os.urandom(1 << 20),))
    con.commit()
    con.close()


def fingerprint(path) -> tuple:
    st = os.stat(path)
    return (hashlib.sha256(Path(path).read_bytes()).hexdigest(), st.st_mtime_ns, st.st_size)


def flip_byte(path, at: float = 0.5) -> None:
    raw = bytearray(Path(path).read_bytes())
    raw[int(len(raw) * at)] ^= 0xFF
    Path(path).write_bytes(bytes(raw))


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AICOS_BACKUP_", "RESTIC_"))}
    env.update({"PYTHONPATH": str(BACKEND), "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra or {})
    return env


def cli(*args, env: dict | None = None, timeout: int = 300, module: str = "app.backup_routine") -> subprocess.CompletedProcess:
    """The real command line, in a real process."""
    return subprocess.run([sys.executable, "-W", "ignore", "-m", module, *map(str, args)], cwd=BACKEND, env=child_env(env), capture_output=True, text=True, timeout=timeout)


class Clock:
    """One fake clock for the routine AND for the backup module, so file names, manifests, journal and status all agree on 'now'."""

    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, hours: float = 0.0, seconds: float = 0.0) -> float:
        self.t += hours * 3600 + seconds
        return self.t


def install_clock(monkeypatch, start: datetime | None = None) -> Clock:
    from app import backup, backup_routine

    start = start or datetime(2026, 11, 10, 6, 0, 0, tzinfo=timezone.utc)
    clk = Clock(start.timestamp())

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clk.t, tz or timezone.utc)

    monkeypatch.setattr(backup, "datetime", _Dt)
    monkeypatch.setattr(backup_routine, "clock", clk)
    return clk
