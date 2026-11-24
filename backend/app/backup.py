"""Verifiable, restorable SQLite backups (stdlib only — the SQLite Online Backup API, the mechanism behind `sqlite3 .backup` and Litestream).

  python -m app.backup create  --db PATH --out DIR [--keep N]    consistent snapshot of a LIVE database → DIR/aicos-<UTC>-<sha8>.sqlite3 + .manifest.json
  python -m app.backup verify  FILE                              sha256 + size + integrity_check + row counts vs the manifest
  python -m app.backup restore FILE --to NEW_PATH [--keep-sessions]
  python -m app.backup drill   --db PATH --work DIR              create → verify → restore → boot the real app on the copy → clean up (PASS/FAIL)
  python -m app.backup list    --out DIR
  python -m app.backup prune   --out DIR --keep N

Design rules (docs/v06/BACKUP_RESTORE.md):
 * `--db` is ALWAYS explicit: there is no default database, so a typo can never back up (or restore over) the wrong one.
 * A source is opened read-only (`mode=ro`) and must already exist: `sqlite3.connect` would silently create an empty database.
 * `restore` writes ONLY to a path that does not exist. Swapping it into place is a deliberate, manual step done with the server stopped:
   SQLite cannot tell whether an idle server still holds the old file open, and renaming a file under a running process makes it keep writing to the orphan.
 * A restored copy has its sessions and login counters emptied (they come from the past; a revoked/deactivated state may not be in them). `--keep-sessions` opts out.
 * A restored copy NEVER keeps the instance lease (`instance_lease`: who was running the source database at that instant). It is runtime state, not data: a copy taken from a
   live server carries a live owner with a fresh heartbeat, and an app booted on it would sit in read-only standby behind a server that is not even using this file.
   Always cleared, `--keep-sessions` or not. The backup file itself stays an exact copy.
 * A backup holds password hashes and session hashes: every file is created 0600 FROM BIRTH (an empty 0600 file is created first and SQLite writes into it, so there is
   no window with open permissions), scratch work happens in a private 0700 directory, a directory WE create is 0700, and a pre-existing directory is never chmodded (we
   only warn if others can enter it). `backend/backups/` is git-ignored. Nothing here encrypts or ships them off-machine (see the doc, "Lo que NO cubre").
 * Ordering: backups carry a monotonic `created_ns` (strictly greater than every backup already in the directory, even if the clock stands still or steps back), and
   retention can never delete the backup it has just created.
 * `restore` publishes with an atomic "create only if absent" primitive: hard link, else renameat2(RENAME_NOREPLACE)/renamex_np(RENAME_EXCL). If the filesystem offers
   none of them it FAILS — it never falls back to an overwriting rename.
 * Retention never deletes the newest backup that still VERIFIES (Issue #15): counting the N newest files is not enough — if those are the damaged ones, the only usable copy
   would be the one deleted. With no verifiable backup at all, `prune` refuses to delete anything.
 * A failing disk or directory (no permission, no space, a size limit) is a clean `FALLO` with exit code 2, never a traceback; a crash in the middle leaves only debris that
   is invisible to `list`/`verify`/`restore` (no manifest) and that `cleanup_orphans` removes once it is old enough not to belong to a live run.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^aicos-\d{8}T\d{6}(\d{6})?Z-[0-9a-f]{8}\.sqlite3$")
CORE_TABLES = {"companies", "users", "sessions", "audit_log"}
RUNTIME_STATE = ("instance_lease", "rate_limits", "rate_limit_meta", "auth_flows")   # who is running RIGHT NOW and who has been calling lately (hashed): meaningless in a copy → always cleared by restore
PAST_STATE = ("sessions", "login_attempts")              # credentials/counters from the past → cleared unless --keep-sessions


class BackupError(Exception):
    pass


def _os_failure(e: OSError, what: str) -> BackupError:
    """A disk/permission problem is an operational failure with an actionable message, not a stack trace."""
    if e.errno in (errno.ENOSPC, errno.EFBIG, errno.EDQUOT):
        why = "sin espacio en disco o límite de tamaño alcanzado"
    elif e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        why = "sin permiso de escritura (o sistema de archivos de solo lectura)"
    else:
        why = os.strerror(e.errno) if e.errno else "error del sistema de archivos"
    return BackupError(f"{what}: {why}")


def _ro(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=30)


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(con: sqlite3.Connection) -> dict:
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}


def _check(con: sqlite3.Connection) -> str:
    res = [r[0] for r in con.execute("PRAGMA integrity_check")]
    return "ok" if res == ["ok"] else "; ".join(res[:3])


def _new_private_file(path) -> None:
    """Create an EMPTY 0600 file (exclusively). Whatever is written into it afterwards — by sqlite or by us — never existed with wider permissions."""
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))


def _private_workdir(parent: Path) -> Path:
    """A 0700 scratch directory next to the destination (same filesystem): even a mistake in a file mode is not reachable by other users."""
    return Path(tempfile.mkdtemp(prefix=".aicos-work-", dir=parent))      # mkdtemp creates it 0700


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _snapshot(src_path, dst_path) -> None:
    """Online backup of src into a NEW file (created 0600 before sqlite touches it), then converted to a single self-contained file (rollback journal, no -wal/-shm)."""
    try:
        src = _ro(src_path)
    except sqlite3.Error as e:
        raise BackupError(f"no se pudo abrir el origen: {e}") from e
    _new_private_file(dst_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    except sqlite3.Error as e:
        raise BackupError(f"la copia falló (¿no es una base SQLite?): {e}") from e
    finally:
        dst.close(); src.close()


def _private(path: Path) -> None:
    os.chmod(path, 0o600)


def _rename_noreplace(src, dst) -> None:
    """Atomic rename that FAILS (EEXIST) instead of replacing: macOS renamex_np(RENAME_EXCL) / Linux renameat2(RENAME_NOREPLACE). Raises OSError(ENOTSUP) if unavailable."""
    libc = ctypes.CDLL(None, use_errno=True)
    s_, d_ = os.fsencode(src), os.fsencode(dst)
    if hasattr(libc, "renamex_np"):
        rc = libc.renamex_np(s_, d_, ctypes.c_uint(0x4))                # RENAME_EXCL
    elif hasattr(libc, "renameat2"):
        rc = libc.renameat2(-100, s_, -100, d_, ctypes.c_uint(1))       # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "no hay rename atómico sin reemplazo en este sistema")
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


def _publish_exclusive(src: Path, dst: Path) -> None:
    """Make `src` appear at `dst` ONLY if `dst` does not exist, atomically, even against a process racing us. Never uses an overwriting rename."""
    try:
        os.link(src, dst)
        return
    except FileExistsError as e:
        raise BackupError(f"el destino ya existe ({dst.name})") from e
    except OSError:
        pass                                                           # no hard links here (EOPNOTSUPP/EXDEV/EPERM…): try the exclusive rename
    try:
        _rename_noreplace(src, dst)
    except FileExistsError as e:
        raise BackupError(f"el destino ya existe ({dst.name})") from e
    except OSError as e:
        if e.errno == errno.EEXIST:
            raise BackupError(f"el destino ya existe ({dst.name})") from e
        raise BackupError("este sistema de archivos no ofrece publicación exclusiva (ni enlace duro ni rename sin reemplazo): "
                          "restaure a un directorio local y mueva el archivo con el servidor detenido") from e


def _newest_ns(out_dir: Path) -> int:
    return max((b["created_ns"] for b in list_backups(out_dir)), default=0)


def create(db, out_dir, keep: int | None = None, keep_daily: int = 0) -> dict:
    try:
        return _create(db, out_dir, keep, keep_daily)
    except OSError as e:
        raise _os_failure(e, "no se pudo escribir la copia") from e


def _create(db, out_dir, keep: int | None, keep_daily: int) -> dict:
    db, out_dir = Path(db), Path(out_dir)
    if not db.is_file():
        raise BackupError(f"el origen no existe: {db.name}")
    warnings: list[str] = []
    if out_dir.exists():
        if stat.S_IMODE(out_dir.stat().st_mode) & 0o077:     # not ours to change: report it. The files inside are 0600 regardless.
            warnings.append(f"el directorio de copias ({out_dir.name}) es accesible por otros usuarios (los archivos son 0600, pero se ven sus nombres); no se modificó")
    else:
        out_dir.mkdir(parents=True, mode=0o700)
        os.chmod(out_dir, 0o700)                              # mkdir applies the umask; this directory is ours, so make it exactly 0700
    work = _private_workdir(out_dir)
    try:
        tmp = work / "snapshot.sqlite3"
        _snapshot(db, tmp)
        con = _ro(tmp)
        try:
            integrity, tables = _check(con), _counts(con)
            fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            con.close()
        if integrity != "ok":
            raise BackupError(f"la copia no pasa integrity_check: {integrity}")
        if not CORE_TABLES <= set(tables):
            raise BackupError("el origen no parece una base de AI Company OS (faltan tablas base)")
        sha = _sha256(tmp)
        created = datetime.now(timezone.utc)
        ns = max(int(created.timestamp() * 1_000_000) * 1000, _newest_ns(out_dir) + 1000)   # strictly newer than every backup already here (clock frozen / stepped back)
        created = datetime.fromtimestamp(ns / 1e9, timezone.utc)
        name = f"aicos-{created.strftime('%Y%m%dT%H%M%S%f')}Z-{sha[:8]}.sqlite3"
        manifest = {"version": 2, "file": name, "created_at": created.isoformat(timespec="microseconds"), "created_ns": ns, "source": db.name, "sha256": sha,
                    "size": tmp.stat().st_size, "integrity": integrity, "foreign_key_violations": fk, "sqlite": sqlite3.sqlite_version, "tables": tables}
        side_tmp = work / "manifest.json"
        _write_private(side_tmp, json.dumps(manifest, indent=1, ensure_ascii=False))
        _publish_exclusive(tmp, out_dir / name)               # data first, then manifest: a crash in between leaves an unlisted, unverifiable file — never a half-trusted one
        os.replace(side_tmp, out_dir / (name + ".manifest.json"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if keep:
        prune(out_dir, keep, protect=name, keep_daily=keep_daily)
    return {**manifest, "warnings": warnings}


def _manifest_of(f: Path) -> dict:
    side = f.with_name(f.name + ".manifest.json")
    if not side.is_file():
        raise BackupError("falta el manifiesto: un .sqlite3 suelto no es una copia verificada")
    try:
        return json.loads(side.read_text())
    except ValueError as e:
        raise BackupError("manifiesto ilegible") from e


def verify(backup_file) -> dict:
    f = Path(backup_file)
    if not f.is_file():
        raise BackupError("la copia no existe")
    m = _manifest_of(f)
    if f.stat().st_size != m.get("size"):
        raise BackupError(f"tamaño distinto al del manifiesto ({f.stat().st_size} ≠ {m.get('size')})")
    if _sha256(f) != m.get("sha256"):
        raise BackupError("sha256 distinto al del manifiesto: la copia cambió o está dañada")
    try:
        con = _ro(f)
        try:
            integrity, tables = _check(con), _counts(con)
        finally:
            con.close()
    except sqlite3.Error as e:
        raise BackupError(f"la copia no se puede abrir: {e}") from e
    if integrity != "ok":
        raise BackupError(f"integrity_check falló: {integrity}")
    if tables != m.get("tables"):
        diff = sorted(k for k in set(tables) | set(m.get("tables", {})) if tables.get(k) != m.get("tables", {}).get(k))
        raise BackupError(f"las filas no coinciden con el manifiesto en: {', '.join(diff)}")
    return {"ok": True, "file": f.name, "created_at": m["created_at"], "tables": len(tables), "rows": sum(tables.values())}


def restore(backup_file, target, revoke_sessions: bool = True) -> dict:
    try:
        return _restore(backup_file, target, revoke_sessions)
    except OSError as e:
        raise _os_failure(e, "no se pudo escribir la copia restaurada") from e


def _restore(backup_file, target, revoke_sessions: bool) -> dict:
    f, target = Path(backup_file), Path(target)
    verify(f)                                                      # never restore something that does not verify
    if target.exists() or Path(str(target) + "-wal").exists() or Path(str(target) + "-shm").exists():
        raise BackupError(f"el destino ya existe ({target.name}): restore solo escribe en una ruta nueva; muévelo tú con el servidor detenido")
    target.parent.mkdir(parents=True, exist_ok=True)
    work = _private_workdir(target.parent)
    try:
        tmp = work / "restore.sqlite3"
        _snapshot(f, tmp)
        con = sqlite3.connect(tmp)
        try:
            cleared = RUNTIME_STATE + (PAST_STATE if revoke_sessions else ())
            for t in cleared:
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                    con.execute(f'DELETE FROM "{t}"')
            con.commit()
            integrity, tables = _check(con), _counts(con)
        finally:
            con.close()
        want = {k: v for k, v in _manifest_of(f)["tables"].items() if k not in cleared}
        if integrity != "ok" or {k: v for k, v in tables.items() if k in want} != want:
            raise BackupError("la copia restaurada no coincide con el manifiesto")
        _private(tmp)
        _publish_exclusive(tmp, target)                            # atomic "create only if absent" — two restores (or a server start) can never overwrite each other
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"ok": True, "target": target.name, "sessions_revoked": revoke_sessions, "rows": sum(tables.values())}


def boot_check(db_path) -> dict:
    """Open a COPY of the restored file with the real application code (migrations + seed sync) and read from it. The restored file itself stays an exact copy.

    The restored app must come up as the ACTIVE instance (it takes the lease, runs its recovery): a copy that boots in standby could not serve a single write. The lease keeper
    is stopped before returning — it is a background thread and must not outlive the scratch copy it beats on."""
    from sqlalchemy import text
    from .api import create_app
    with tempfile.TemporaryDirectory(prefix="aicos-boot-") as d:
        probe = Path(d) / "probe.db"
        _snapshot(db_path, probe)
        def shape():
            con = sqlite3.connect(probe)
            try:
                return {t: tuple(r[1] for r in con.execute(f'PRAGMA table_info("{t}")')) for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                con.close()
        before = shape()
        app = create_app(f"sqlite:///{probe}")
        try:
            active = bool(app.state.orch.active)
            with app.state.eng.connect() as c:
                users = c.execute(text("SELECT count(*) FROM users")).scalar()
                companies = c.execute(text("SELECT count(*) FROM companies")).scalar()
                sessions = c.execute(text("SELECT count(*) FROM sessions")).scalar()
            after = shape()
        finally:
            if app.state.orch.keeper:
                app.state.orch.keeper.stop()
            app.state.eng.dispose()
    if not active:
        raise BackupError("la copia restaurada arrancó en espera (otra instancia aparece como dueña de la base): no podría atender escrituras")
    return {"users": users, "companies": companies, "sessions": sessions, "active": active, "migrated": after != before}


def drill(db, work_dir) -> dict:
    """The only proof that a backup is restorable is restoring it: create → verify → restore elsewhere → boot the app on it → remove the scratch copy."""
    work = Path(work_dir); work.mkdir(parents=True, exist_ok=True, mode=0o700)
    m = create(db, work)
    verify(work / m["file"])
    scratch = work / f"restore-{secrets.token_hex(4)}.sqlite3"
    try:
        restore(work / m["file"], scratch)
        boot = boot_check(scratch)
    finally:
        for p in (scratch, Path(str(scratch) + "-wal"), Path(str(scratch) + "-shm")):
            p.unlink(missing_ok=True)
    return {"ok": True, "backup": m["file"], "created_at": m["created_at"], "boot": boot}


def _order_key(m: dict) -> int:
    if isinstance(m.get("created_ns"), int):
        return m["created_ns"]
    try:                                                           # version-1 manifests (second precision): best effort
        return int(datetime.fromisoformat(m["created_at"]).timestamp() * 1e9)
    except (KeyError, ValueError):
        return 0


def list_backups(out_dir) -> list[dict]:
    out = []
    for p in Path(out_dir).glob("aicos-*.sqlite3"):
        if NAME_RE.match(p.name) and p.with_name(p.name + ".manifest.json").is_file():
            try:
                m = _manifest_of(p)
                out.append({"file": p.name, "created_at": m["created_at"], "created_ns": _order_key(m), "size": m["size"]})
            except (BackupError, KeyError):
                continue
    return sorted(out, key=lambda x: (x["created_ns"], x["file"]), reverse=True)   # newest first; the name only breaks exact ties deterministically


def newest_verified(out_dir) -> dict | None:
    """The newest backup that still verifies RIGHT NOW (sha256 + size + integrity_check + rows), looking past damaged newer ones. None if there is none."""
    for b in list_backups(out_dir):                                # newest first
        try:
            verify(Path(out_dir) / b["file"])
        except BackupError:
            continue
        return b
    return None


def _utc_day(b: dict):
    return datetime.fromtimestamp(b["created_ns"] / 1e9, timezone.utc).date()


def prune(out_dir, keep: int, protect: str | None = None, keep_daily: int = 0) -> list[str]:
    """Survivors = the `keep` newest ∪ the newest of each of the last `keep_daily` UTC days ∪ the newest backup that VERIFIES ∪ `protect`.

    The last two are the point: counting files protects nothing if the newest ones are the damaged ones. With no verifiable backup (and nothing `protect`ed, which the caller has
    just verified), deleting is refused — you do not tidy up evidence when you cannot tell what is good."""
    if keep < 1:
        raise BackupError("--keep debe ser ≥ 1 (nunca se borran todas las copias)")
    if keep_daily < 0:
        raise BackupError("--keep-daily no puede ser negativo")
    ordered = list_backups(out_dir)
    if not ordered:
        return []
    survivors = {b["file"] for b in ordered[:keep]}
    if protect:
        survivors.add(protect)                                     # the backup just created (verified by `create`) is never a deletion candidate
    else:
        good = newest_verified(out_dir)
        if good is None:
            raise BackupError("ninguna copia verifica: no se borra nada (con copias dañadas, podar solo destruiría evidencia)")
        survivors.add(good["file"])
    if keep_daily:
        seen: list = []
        for b in ordered:                                          # newest first: the first one seen for a day is that day's newest
            d = _utc_day(b)
            if d not in seen and len(seen) < keep_daily:
                seen.append(d)
                survivors.add(b["file"])
    gone = []
    for b in ordered:                                              # only files that match our naming AND have our manifest
        if b["file"] in survivors:
            continue
        p = Path(out_dir) / b["file"]
        p.unlink(missing_ok=True); p.with_name(p.name + ".manifest.json").unlink(missing_ok=True)
        gone.append(b["file"])
    return gone


def cleanup_orphans(out_dir, older_than_s: float = 3600, now: float | None = None) -> list[str]:
    """Remove what a process that died in the middle of `create` leaves behind: our `.aicos-work-*` scratch directories and `aicos-*.sqlite3` files that never got a manifest.
    Only when they are OLD — a recent one may belong to a `create` running right now in another terminal. Anything that is not ours is never touched."""
    now = time.time() if now is None else now
    removed = []
    for p in sorted(Path(out_dir).iterdir()) if Path(out_dir).is_dir() else []:
        try:
            if p.is_dir() and p.name.startswith(".aicos-work-"):
                newest = max([p.stat().st_mtime] + [c.stat().st_mtime for c in p.iterdir()])
                if now - newest > older_than_s:
                    shutil.rmtree(p, ignore_errors=True); removed.append(p.name)
            elif p.is_file() and NAME_RE.match(p.name) and not p.with_name(p.name + ".manifest.json").exists():
                if now - p.stat().st_mtime > older_than_s:
                    p.unlink(missing_ok=True); removed.append(p.name)
        except OSError:
            continue                                               # vanished under us (a live run finished): nothing to clean
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.backup", description="Copias de seguridad verificables de la base SQLite")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--db", required=True); c.add_argument("--out", required=True); c.add_argument("--keep", type=int); c.add_argument("--keep-daily", type=int, default=0)
    v = sub.add_parser("verify"); v.add_argument("file")
    r = sub.add_parser("restore"); r.add_argument("file"); r.add_argument("--to", required=True); r.add_argument("--keep-sessions", action="store_true")
    d = sub.add_parser("drill"); d.add_argument("--db", required=True); d.add_argument("--work", required=True)
    ls = sub.add_parser("list"); ls.add_argument("--out", required=True)
    pr = sub.add_parser("prune"); pr.add_argument("--out", required=True); pr.add_argument("--keep", type=int, required=True); pr.add_argument("--keep-daily", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "create":
            res = create(a.db, a.out, a.keep, a.keep_daily); print(f"OK {res['file']}  sha256={res['sha256'][:16]}…  filas={sum(res['tables'].values())}")
            for w in res["warnings"]:
                print(f"AVISO: {w}", file=sys.stderr)
        elif a.cmd == "verify":
            res = verify(a.file); print(f"OK {res['file']}  {res['tables']} tablas, {res['rows']} filas, integrity_check ok")
        elif a.cmd == "restore":
            res = restore(a.file, a.to, revoke_sessions=not a.keep_sessions); print(f"OK restaurada en {res['target']} (sesiones revocadas: {res['sessions_revoked']})")
        elif a.cmd == "drill":
            res = drill(a.db, a.work); print(f"PASS copia={res['backup']} arranque: {res['boot']}")
        elif a.cmd == "list":
            for b in list_backups(a.out):
                print(f"{b['created_at']}  {b['size']:>10}  {b['file']}")
        elif a.cmd == "prune":
            print("borradas:", prune(a.out, a.keep, keep_daily=a.keep_daily) or "ninguna")
        return 0
    except BackupError as e:
        print(f"FALLO: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
