"""Single-live-instance lease on the database (table `instance_lease`, one row).

Why: after a crash, runs left `running` are orphans (their thread died with the process) and must be surfaced; but a SECOND process that merely opens the same
database (a second launcher, a preflight, a tool) must never declare the first process's live runs dead, nor run a second plan reconciler beside it.
Rule: the lease is held by the newest instance whose process is alive AND whose heartbeat is fresh. A dead pid is taken over at once (no waiting for the TTL);
a hung or pid-reused owner is taken over when its heartbeat is older than TTL. The same pid is never "another instance" (tests restart apps in-process).
No new dependency: one row, compare-and-set.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

TTL_SECONDS = 30
HEARTBEAT_SECONDS = 10.0
log = logging.getLogger("aicos.lease")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True                      # exists, owned by someone else
    except (ProcessLookupError, OverflowError, OSError):
        return False
    return True


def _owner_alive(row, host: str, pid: int) -> bool:
    if datetime.fromisoformat(row["heartbeat_at"]) < datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS):
        return False
    if row["host"] == host:
        return row["pid"] != pid and _pid_exists(row["pid"])
    return True                          # another machine on a shared filesystem: only the heartbeat can say


def acquire(eng, boot_id: str, *, pid: int | None = None, host: str | None = None) -> bool:
    pid, host = pid or os.getpid(), host or socket.gethostname()
    try:
        with eng.begin() as c:
            row = c.execute(text("SELECT * FROM instance_lease WHERE id=1")).mappings().first()
            if row is None:
                c.execute(text("INSERT INTO instance_lease(id,boot_id,host,pid,started_at,heartbeat_at) VALUES(1,:b,:h,:p,:t,:t)"), {"b": boot_id, "h": host, "p": pid, "t": _now()})
                return True
            if row["boot_id"] == boot_id:
                return True
            if _owner_alive(row, host, pid):
                return False
            n = c.execute(text("UPDATE instance_lease SET boot_id=:b,host=:h,pid=:p,started_at=:t,heartbeat_at=:t WHERE id=1 AND boot_id=:old"),
                          {"b": boot_id, "h": host, "p": pid, "t": _now(), "old": row["boot_id"]}).rowcount
            return n == 1                # compare-and-set: two simultaneous takeovers cannot both win
    except IntegrityError:
        return False                     # someone inserted the row between our read and write


def heartbeat(eng, boot_id: str) -> bool:
    with eng.begin() as c:
        return c.execute(text("UPDATE instance_lease SET heartbeat_at=:t WHERE id=1 AND boot_id=:b"), {"t": _now(), "b": boot_id}).rowcount == 1


def release(eng, boot_id: str) -> bool:
    with eng.begin() as c:
        return c.execute(text("DELETE FROM instance_lease WHERE id=1 AND boot_id=:b"), {"b": boot_id}).rowcount == 1


class LeaseLost(Exception):
    """Raised inside a run/plan thread that must stop: this instance no longer holds the lease (or it lost and regained it: a new epoch)."""


class LeaseKeeper:
    """Owns this instance's role. ACTIVE = holds the lease (renews it, recovers on gain, runs work); STANDBY = serves reads only and tries to take over.

    * every `every` seconds: the holder renews; a standby tries `acquire` (a dead pid or a stale heartbeat makes that succeed at once);
    * losing the lease bumps `epoch`: threads started in an older epoch see it at their next step and stop (`check`), so a hung-then-replaced holder cannot keep
      producing effects beside the new one;
    * gaining it (also at start) runs `on_gain` (recover orphan runs + re-run plan reconcilers) exactly once per gain.
    """

    def __init__(self, eng, boot_id: str, on_gain=None, on_lost=None, every: float = HEARTBEAT_SECONDS):
        self.eng, self.boot_id, self.on_gain, self.on_lost, self.every = eng, boot_id, on_gain, on_lost, every
        self.held, self.epoch = False, 0
        self._stop, self._mu, self._t = threading.Event(), threading.Lock(), None

    def start(self, background: bool) -> "LeaseKeeper":
        self._tick()                                           # first acquisition is synchronous: create_app knows its role before serving
        if background:
            self._t = threading.Thread(target=self._loop, daemon=True, name="lease-keeper")
            self._t.start()
        return self

    def _tick(self) -> None:
        with self._mu:
            if self.held:
                if not heartbeat(self.eng, self.boot_id):
                    self.held, self.epoch = False, self.epoch + 1
                    log.error("el lease de la base pasó a otra instancia: esta instancia queda en espera y detiene su trabajo en curso")
                    if self.on_lost:
                        self.on_lost()
            elif acquire(self.eng, self.boot_id):
                self.held, self.epoch = True, self.epoch + 1
                if self.on_gain:
                    self.on_gain()

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            try:
                self._tick()
            except Exception:                                  # database momentarily gone/locked: the next beat retries; never kill the server for this
                log.warning("latido del lease omitido", exc_info=True)

    def check(self, epoch: int) -> None:
        if not self.held or epoch != self.epoch:
            raise LeaseLost("esta instancia ya no tiene el lease de la base de datos")

    def stop(self) -> None:
        self._stop.set()
        with self._mu:
            if self.held:
                self.held = False
                release(self.eng, self.boot_id)
