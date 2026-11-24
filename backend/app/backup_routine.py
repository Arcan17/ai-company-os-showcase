"""Rutina de respaldos reproducible (Issue #15): UNA pasada = copia consistente → verificación → restauración a un lugar nuevo → arranque de la app real sobre lo restaurado →
retención → (opcional) copia CIFRADA fuera de la máquina → diario. Más el estado (RPO/RTO medidos), la re-verificación periódica y la preparación —sin instalar— de la agenda.

  python -m app.backup_routine run    --db P --out D [--keep-last 3] [--keep-daily 14] [--drill-every-hours 0] [--external --env-file F] [--json]
  python -m app.backup_routine status --out D [--max-backup-age-hours 26] [--max-drill-age-hours 26] [--external-required] [--json]      0 ok · 1 aviso · 2 crítico
  python -m app.backup_routine scrub  --out D [--count 3] [--external --env-file F]
  python -m app.backup_routine schedule --kind launchd|systemd|cron --db P --out D --dest DIR_NUEVO     (solo escribe archivos de texto; NO instala nada)
  python -m app.backup_routine external-init | external-rotate-key --env-file F ...

Exit codes de `run`: 0 ok · 1 terminó con avisos (p. ej. el destino externo falló; la copia local está bien) · 2 falló · 75 otra ejecución en curso (EX_TEMPFAIL).

Reglas (docs/v06/BACKUP_RUTINA_Y_DESTINO_EXTERNO.md):
 * Nada de lo que se programa puede correr dos veces a la vez: un cerrojo `flock` del kernel (se suelta solo si el proceso muere, incluso con SIGKILL).
 * La retención y la subida externa solo ocurren tras una copia verificada Y un ensayo de restauración exitoso. Lo que no se pudo restaurar no sale de la máquina ni borra nada.
 * El estado NO se fía del diario: la «última copia buena» sale de verificar los archivos ahora. El diario solo aporta lo que los archivos no pueden decir (ensayos, destino externo).
 * Falla cerrado: sin copias, copia vieja, copia dañada o ensayo fallido → nivel crítico (exit 2). Un destino externo caído es aviso (exit 1), no pérdida local.
 * Nada de secretos en el diario ni en la salida: todo texto pasa por `redact`.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import backup
from .backup import BackupError
from .backup_external import ExternalError, Restic, redact

clock = time.time                                                    # seam: las pruebas lo reemplazan por un reloj falso
JOURNAL_MAX = 1000
_TRIM_SLACK = 100
EXIT_BUSY = 75


class RoutineBusy(Exception):
    pass


# ====================================================================================================================== diario
def routine_dir(out) -> Path:
    return Path(out) / ".routine"


def journal_path(out) -> Path:
    return routine_dir(out) / "journal.jsonl"


def _scrub(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def journal_append(out, entry: dict) -> None:
    """One JSON line per event, appended with a single `write` (O_APPEND: lines from different processes never interleave). Born 0600. Bounded: old lines are dropped."""
    rd = routine_dir(out)
    rd.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = json.dumps({"v": 1, "ts": _iso(clock()), **_scrub(entry)}, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(journal_path(out), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    _trim(out)


def _trim(out) -> None:
    j = journal_path(out)
    try:
        if j.stat().st_size < JOURNAL_MAX * 40:                       # a line is never shorter than ~45 bytes: it cannot hold more than the limit, do not even read it
            return
        lines = j.read_bytes().splitlines(keepends=True)
        if len(lines) <= JOURNAL_MAX + _TRIM_SLACK:
            return
        tmp = j.with_name(f"journal.{secrets.token_hex(3)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.writelines(lines[-JOURNAL_MAX:])
        os.replace(tmp, j)
    except OSError:
        pass                                                           # a journal that cannot be trimmed is a nuisance, never a failed backup


def read_journal(out) -> list[dict]:
    try:
        raw = journal_path(out).read_text(errors="replace").splitlines()
    except OSError:
        return []
    entries = []
    for line in raw[-(JOURNAL_MAX + _TRIM_SLACK):]:
        try:
            e = json.loads(line)
        except ValueError:
            continue                                                   # a line cut by a power loss: ignore it, never trust half of it
        if isinstance(e, dict):
            entries.append(e)
    return entries[-JOURNAL_MAX:]


# ====================================================================================================================== cerrojo
@contextlib.contextmanager
def exclusive(out):
    """Only one routine at a time per backup directory. `flock` belongs to the open file, so the kernel releases it if the process dies (even by SIGKILL): no stale lock files."""
    out = Path(out)
    if not out.exists():
        out.mkdir(parents=True, mode=0o700)
        os.chmod(out, 0o700)                                           # a directory WE create is private (an existing one is never chmodded)
    rd = routine_dir(out)
    rd.mkdir(exist_ok=True, mode=0o700)
    fd = os.open(rd / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RoutineBusy("otra ejecución en curso sobre este directorio de copias") from e
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ====================================================================================================================== ensayo de restauración
def prove_restorable(backup_file, work_parent) -> dict:
    """The only proof a backup is usable: restore it to a NEW file, boot the real application on it, then delete the scratch copy. Everything is measured (that IS the RTO)."""
    scratch_dir = Path(tempfile.mkdtemp(prefix=".drill-", dir=work_parent))          # 0700, same filesystem
    try:
        scratch = scratch_dir / "restore.sqlite3"
        t = time.monotonic(); backup.restore(backup_file, scratch); restore_s = round(time.monotonic() - t, 4)
        t = time.monotonic(); boot = backup.boot_check(scratch); boot_s = round(time.monotonic() - t, 4)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    return {"ok": True, "sessions": boot["sessions"], "active": boot["active"], "migrated": boot["migrated"], "restore_seconds": restore_s, "boot_seconds": boot_s,
            "total_seconds": round(restore_s + boot_s, 4)}


def _clear_scratch(out) -> None:
    """Scratch copies of a drill that was killed halfway (a restored database is a full copy of the data). We hold the exclusive lock, so no live drill can own them: all are stale."""
    for p in routine_dir(out).glob(".*drill-*"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def _last(entries, pred):
    for e in reversed(entries):
        if pred(e):
            return e
    return None


def _drill_due(entries, every_hours: float) -> bool:
    if every_hours <= 0:
        return True
    last = _last(entries, lambda e: e.get("event") == "run" and (e.get("drill") or {}).get("ok"))
    return last is None or (clock() - float(last.get("t1") or 0)) >= every_hours * 3600


def _external_drill_due(entries, every_hours: float) -> bool:
    last = _last(entries, lambda e: e.get("event") == "run" and ((e.get("external") or {}).get("restore_drill") or {}).get("ok"))
    return last is None or every_hours <= 0 or (clock() - float(last.get("t1") or 0)) >= every_hours * 3600


# ====================================================================================================================== una pasada
def run(db, out, *, keep_last: int = 3, keep_daily: int = 14, drill_every_hours: float = 0.0, external=None, external_error: str | None = None,
        external_drill_every_hours: float = 168.0) -> dict:
    """One scheduled pass. Never raises for an operational failure (it is journaled and returned with exit 2); raises RoutineBusy if another pass holds the lock."""
    out = Path(out)
    res = {"event": "run", "run_id": secrets.token_hex(4), "status": "failed", "exit": 2, "t0": clock(), "t1": None, "backup": None, "drill": None,
           "retention": {"removed": []}, "external": None, "warnings": [], "error": None}
    try:
        with exclusive(out):
            _locked_pass(Path(db), out, res, keep_last, keep_daily, drill_every_hours, external, external_error, external_drill_every_hours)
    except RoutineBusy as e:
        res.update(status="skipped", exit=EXIT_BUSY, error=str(e), t1=clock())
        _journal(out, res)
        raise
    except BackupError as e:
        res.update(status="failed", exit=2, error=str(e))
    except OSError as e:
        res.update(status="failed", exit=2, error=str(backup._os_failure(e, "la rutina no pudo escribir")))
    except Exception as e:                                             # a bug must not look like success, and must leave a trace
        res.update(status="failed", exit=2, error=f"error inesperado: {type(e).__name__}: {e}")
    res["t1"] = clock()
    res["error"] = redact(res["error"]) if res["error"] else None
    _journal(out, res)
    return res


def _journal(out, res) -> None:
    try:
        journal_append(out, res)
    except OSError as e:
        res["warnings"].append(f"no se pudo escribir el diario ({os.strerror(e.errno) if e.errno else 'error'})")


def _locked_pass(db, out, res, keep_last, keep_daily, drill_every_hours, external, external_error, external_drill_every_hours) -> None:
    entries = read_journal(out)
    backup.cleanup_orphans(out, now=clock())
    _clear_scratch(out)
    t = time.monotonic()
    m = backup.create(db, out)                                          # online snapshot of a LIVE database (source opened read-only)
    v = backup.verify(out / m["file"])                                  # and re-read from where it was published
    res["warnings"] += list(m.get("warnings", []))
    res["backup"] = {"file": m["file"], "created_at": m["created_at"], "size": m["size"], "rows": v["rows"], "seconds": round(time.monotonic() - t, 3)}

    if _drill_due(entries, drill_every_hours):
        try:
            res["drill"] = prove_restorable(out / m["file"], routine_dir(out))
        except Exception as e:
            res["drill"] = {"ok": False, "error": redact(str(e))}
            raise BackupError(f"el ensayo de restauración falló: {e}") from e

    try:
        res["retention"]["removed"] = backup.prune(out, keep_last, protect=m["file"], keep_daily=keep_daily)
    except BackupError as e:
        res["warnings"].append(f"la retención falló y no se borró nada: {e}")

    if external is not None or external_error:
        res["external"] = _push_external(out, m, external, external_error, entries, external_drill_every_hours, res)
    ext = res["external"]
    ext_trouble = bool(ext) and (not ext["ok"] or (ext.get("restore_drill") is not None and not ext["restore_drill"].get("ok")))
    res["status"] = "warn" if res["warnings"] or ext_trouble else "ok"
    res["exit"] = 1 if res["status"] == "warn" else 0


def _push_external(out, m, external, external_error, entries, drill_every_hours, res) -> dict:
    ext = {"ok": False, "snapshot": None, "seconds": None, "forget": None, "restore_drill": None, "code": None, "error": None}
    if external is None:
        ext.update(code="config", error=redact(external_error or "destino externo sin configurar"))
        res["warnings"].append("destino externo no disponible: " + ext["error"])
        return ext
    try:
        pushed = external.push([out / m["file"], out / (m["file"] + ".manifest.json")])
        ext.update(ok=True, snapshot=pushed.get("snapshot"), seconds=pushed.get("seconds"))
    except ExternalError as e:
        ext.update(code=e.code, error=redact(str(e)))
        res["warnings"].append(f"la copia externa falló ({e.code}); la copia local está intacta")
        return ext
    except Exception as e:
        ext.update(code="failed", error=redact(f"{type(e).__name__}: {e}"))
        res["warnings"].append("la copia externa falló (error inesperado); la copia local está intacta")
        return ext
    try:
        ext["forget"] = external.forget()                              # retention AFTER a successful push, never before
    except Exception as e:
        res["warnings"].append(f"la retención externa falló ({getattr(e, 'code', 'failed')}); no se borró nada")
    if _external_drill_due(entries, drill_every_hours):
        work = Path(tempfile.mkdtemp(prefix=".extdrill-", dir=routine_dir(out)))
        try:
            fetched = external.fetch(ext["snapshot"], work)
            data = next(p for p in fetched if backup.NAME_RE.match(p.name))
            ext["restore_drill"] = prove_restorable(data, routine_dir(out))
        except Exception as e:
            ext["restore_drill"] = {"ok": False, "error": redact(f"{getattr(e, 'code', type(e).__name__)}: {e}")}
            res["warnings"].append("lo subido NO se pudo restaurar desde el destino externo")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return ext


# ====================================================================================================================== estado
def _problem(level, code, message, action):
    return {"level": level, "code": code, "message": message, "action": action}


RUN_HINT = "python -m app.backup_routine run --db <base> --out <directorio de copias>"


def status(out, *, max_backup_age_hours: float = 26.0, max_drill_age_hours: float = 26.0, external_required: bool = False, max_external_age_hours: float = 26.0) -> dict:
    """What an operator (or a monitor) needs to know, computed from the FILES now plus the journal. Read-only. Contains no paths, no full hashes, no credentials."""
    now = clock()
    out = Path(out)
    problems: list[dict] = []
    st = {"level": "ok", "exit": 0, "checked_at": _iso(now), "targets": {"max_backup_age_hours": max_backup_age_hours, "max_drill_age_hours": max_drill_age_hours},
          "backups": {"count": 0, "newest": None, "newest_verified": None}, "rpo": {}, "drill": {}, "external": {"configured": False}, "last_run": None, "problems": problems}

    def finish():
        order = {"critical": 2, "warn": 1}
        top = max((order[p["level"]] for p in problems), default=0)
        st["exit"] = top
        st["level"] = {0: "ok", 1: "warn", 2: "critical"}[top]
        return st

    if not out.is_dir():
        problems.append(_problem("critical", "out-missing", "el directorio de copias no existe", f"Cree la primera copia: {RUN_HINT}"))
        return finish()
    listed = backup.list_backups(out)
    st["backups"]["count"] = len(listed)
    entries = read_journal(out)
    runs = [e for e in entries if e.get("event") == "run" and e.get("status") != "skipped"]
    if not listed:
        problems.append(_problem("critical", "no-backups", "no hay ninguna copia de seguridad", f"Cree la primera copia: {RUN_HINT}"))
    else:
        newest = listed[0]
        good = backup.newest_verified(out)

        def brief(b, verified=None):
            d = {"file": b["file"], "created_at": b["created_at"], "age_hours": round(max(0.0, now - b["created_ns"] / 1e9) / 3600, 2), "size": b["size"]}
            if verified is not None:
                d["verified"] = verified
            return d

        st["backups"]["newest"] = brief(newest, good is not None and good["file"] == newest["file"])
        if good is None:
            problems.append(_problem("critical", "no-verified-backup", "ninguna copia verifica (hash, tamaño o integridad)", f"Haga una copia nueva ahora: {RUN_HINT}; investigue el disco"))
        else:
            st["backups"]["newest_verified"] = brief(good)
            age_h = st["backups"]["newest_verified"]["age_hours"]
            if good["file"] != newest["file"]:
                problems.append(_problem("warn", "newest-corrupt", f"la copia más reciente está dañada; la última buena tiene {age_h} h",
                                         "Revise el disco y haga una copia nueva; no borre las copias a mano"))
            if age_h > max_backup_age_hours:
                problems.append(_problem("critical", "backup-stale", f"la última copia buena tiene {age_h} h (objetivo ≤ {max_backup_age_hours:g} h)",
                                         f"Compruebe que la tarea programada corre; ejecute ahora: {RUN_HINT}"))
            week = [b for b in sorted(listed, key=lambda b: b["created_ns"]) if now - b["created_ns"] / 1e9 <= 7 * 86400]
            gaps = [(b["created_ns"] - a["created_ns"]) / 1e9 / 3600 for a, b in zip(week, week[1:])]
            worst = round(max(gaps), 2) if gaps else 0.0
            st["rpo"] = {"current_age_hours": age_h, "worst_gap_hours_7d": worst, "worst_case_hours": round(max(worst, age_h), 2), "target_hours": max_backup_age_hours}

    ok_drill = _last(runs, lambda e: (e.get("drill") or {}).get("ok"))
    bad_drill = _last(runs, lambda e: e.get("drill") is not None and not e["drill"].get("ok"))
    if ok_drill:
        d = ok_drill["drill"]
        st["drill"] = {"last_ok_at": _iso(float(ok_drill.get("t1") or 0)), "last_ok_age_hours": round((now - float(ok_drill.get("t1") or 0)) / 3600, 2),
                       "measured_rto_seconds": d.get("total_seconds"), "restore_seconds": d.get("restore_seconds"), "boot_seconds": d.get("boot_seconds"),
                       "sessions_revoked": d.get("sessions") == 0, "active_after_restore": bool(d.get("active"))}
    if bad_drill and (not ok_drill or float(bad_drill.get("t1") or 0) >= float(ok_drill.get("t1") or 0)):
        problems.append(_problem("critical", "drill-failed", "el último ensayo de restauración FALLÓ: no está comprobado que las copias se puedan restaurar",
                                 "Revise el último fallo en el diario de la rutina y repita la pasada; no dependa de estas copias hasta que el ensayo pase"))
    elif not ok_drill:
        problems.append(_problem("warn", "no-drill", "no hay ningún ensayo de restauración registrado", f"Ejecute una pasada completa: {RUN_HINT}"))
    elif st["drill"]["last_ok_age_hours"] > max_drill_age_hours:
        problems.append(_problem("warn", "drill-stale", f"el último ensayo de restauración tiene {st['drill']['last_ok_age_hours']} h (objetivo ≤ {max_drill_age_hours:g} h)",
                                 f"Ejecute una pasada completa: {RUN_HINT}"))

    last_run = runs[-1] if runs else None
    if last_run:
        st["last_run"] = {"at": _iso(float(last_run.get("t1") or 0)), "status": last_run.get("status"), "exit": last_run.get("exit")}
        if last_run.get("status") == "failed" and not (bad_drill and last_run is bad_drill):
            problems.append(_problem("warn", "last-run-failed", "la última pasada de la rutina falló", "Revise el error en el diario y repita la pasada"))

    scrubs = [e for e in entries if e.get("event") == "scrub"]
    if scrubs and scrubs[-1].get("bad"):
        problems.append(_problem("warn", "scrub-failed", f"la última verificación periódica (scrub) encontró {len(scrubs[-1]['bad'])} copia(s) local(es) dañada(s)", "Ejecute `scrub` otra vez y revise el disco; haga una copia nueva"))
    ext_scrub = _last(scrubs, lambda e: (e.get("external") or {}).get("requested"))
    if ext_scrub and not ext_scrub["external"].get("ok"):
        e = ext_scrub["external"]
        what = "no se pudo realizar" if not e.get("performed") else "encontró daños"
        problems.append(_problem("warn", "scrub-failed", f"la última verificación del repositorio externo {what} ({e.get('code') or 'failed'})",
                                 "Revise la configuración y las credenciales del destino y ejecute `scrub --external --env-file <archivo>`; solo una verificación externa exitosa lo despeja"))

    ext_runs = [e for e in runs if e.get("external")]
    if ext_runs or external_required:
        level = "critical" if external_required else "warn"
        ext_ok = _last(ext_runs, lambda e: e["external"].get("ok"))
        ext_bad = _last(ext_runs, lambda e: not e["external"].get("ok"))
        info = {"configured": True, "required": external_required}
        if ext_ok:
            info.update(last_ok_at=_iso(float(ext_ok.get("t1") or 0)), last_ok_age_hours=round((now - float(ext_ok.get("t1") or 0)) / 3600, 2))
        rd = _last(ext_runs, lambda e: (e["external"].get("restore_drill") or {}).get("ok"))
        if rd:
            info["last_restore_drill_age_hours"] = round((now - float(rd.get("t1") or 0)) / 3600, 2)
        st["external"] = info
        if ext_bad and (not ext_ok or float(ext_bad.get("t1") or 0) >= float(ext_ok.get("t1") or 0)):
            code = ext_bad["external"].get("code") or "failed"
            problems.append(_problem(level, "external-failed", f"la última copia externa falló ({code})", "Revise la conexión, las credenciales y el espacio del destino; la copia local sigue intacta"))
        elif not ext_ok:
            problems.append(_problem(level, "external-missing", "aún no hay ninguna copia externa", f"Ejecute: {RUN_HINT} --external --env-file <archivo>"))
        elif info["last_ok_age_hours"] > max_external_age_hours:
            problems.append(_problem(level, "external-stale", f"la última copia externa tiene {info['last_ok_age_hours']} h (objetivo ≤ {max_external_age_hours:g} h)", "Compruebe la tarea programada y el destino"))
        failed_drill = _last(ext_runs, lambda e: e["external"].get("restore_drill") is not None and not e["external"]["restore_drill"].get("ok"))
        if failed_drill and (not rd or float(failed_drill.get("t1") or 0) > float(rd.get("t1") or 0)):
            problems.append(_problem("warn", "external-restore-failed", "lo subido al destino externo no se pudo restaurar en el último ensayo", "Ejecute `scrub --external` y revise el repositorio"))
    return finish()


def scrub(out, count: int = 3, external=None, *, external_requested: bool | None = None, external_error: str | None = None, external_read_data: bool = False) -> dict:
    """Re-verify the newest `count` stored backups (bit rot, a bad sector, an editor): a backup that was good yesterday is not necessarily good today.

    If the EXTERNAL repository was asked for, it is part of the verdict: a configuration that cannot be loaded (missing, over-permissive, unreadable), an unreachable repository or a
    wrong password is a FAILURE that says the external check never happened (`performed: false`) — a scrub must never report the encrypted repository as checked because only the
    local copies were. `performed` is true only when restic actually looked (and then `ok` says what it found: `damaged` is a performed, failed check)."""
    requested = bool(external_requested) or external is not None or bool(external_error)
    bad, checked = [], 0
    for b in backup.list_backups(out)[:max(1, count)]:
        checked += 1
        try:
            backup.verify(Path(out) / b["file"])
        except BackupError as e:
            bad.append({"file": b["file"], "error": str(e)})
    ext = {"requested": requested, "performed": False, "ok": False if requested else None}
    if requested:
        if external is None:
            ext.update(code="config", error=redact(external_error or "destino externo sin configurar"))
        else:
            try:
                r = external.check(read_data=True) if external_read_data else external.check(read_data_subset="5%")
                ext.update(performed=True, ok=True, seconds=r.get("seconds"))
            except ExternalError as e:
                ext.update(code=e.code, error=redact(str(e)), performed=(e.code == "damaged"))      # restic read the repository and found damage: the check DID happen
            except Exception as e:
                ext.update(code="failed", error=redact(f"{type(e).__name__}: {e}"))
    res = {"ok": not bad and (not requested or bool(ext["ok"])), "checked": checked, "bad": bad, "external": ext}
    try:
        journal_append(out, {"event": "scrub", "status": "ok" if res["ok"] else "failed", "checked": checked, "bad": [b["file"] for b in bad], "external": ext})
    except OSError:
        pass
    return res


# ====================================================================================================================== línea de comandos
def _external_from_args(a, out):
    try:
        return Restic.from_environment(os.environ, env_file=a.env_file, cache_dir=routine_dir(out) / "restic-cache"), None
    except ExternalError as e:
        return None, str(e)


def _print_status(st) -> None:
    head = {"ok": "OK", "warn": "AVISO", "critical": "CRÍTICO"}[st["level"]]
    nv = st["backups"].get("newest_verified")
    line = f"{head}: {st['backups']['count']} copias"
    if nv:
        line += f"; última buena hace {nv['age_hours']} h"
    if st["rpo"]:
        line += f"; peor hueco 7 d: {st['rpo']['worst_gap_hours_7d']} h"
    if st["drill"]:
        line += f"; ensayo hace {st['drill']['last_ok_age_hours']} h (RTO medido {st['drill']['measured_rto_seconds']} s)"
    print(line)
    for p in st["problems"]:
        print(f"  [{p['level']}] {p['code']}: {p['message']} → {p['action']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.backup_routine", description="Rutina de respaldos: copia → verificación → restauración → arranque (+ destino externo cifrado)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--db", required=True); r.add_argument("--out", required=True)
    r.add_argument("--keep-last", type=int, default=3); r.add_argument("--keep-daily", type=int, default=14)
    r.add_argument("--drill-every-hours", type=float, default=0.0); r.add_argument("--external", action="store_true"); r.add_argument("--env-file")
    r.add_argument("--external-drill-every-hours", type=float, default=168.0); r.add_argument("--json", action="store_true")
    s = sub.add_parser("status"); s.add_argument("--out", required=True)
    s.add_argument("--max-backup-age-hours", type=float, default=26.0); s.add_argument("--max-drill-age-hours", type=float, default=26.0)
    s.add_argument("--max-external-age-hours", type=float, default=26.0); s.add_argument("--external-required", action="store_true"); s.add_argument("--json", action="store_true")
    sc = sub.add_parser("scrub"); sc.add_argument("--out", required=True); sc.add_argument("--count", type=int, default=3); sc.add_argument("--external", action="store_true"); sc.add_argument("--env-file"); sc.add_argument("--read-data", action="store_true")
    sh = sub.add_parser("schedule"); sh.add_argument("--kind", required=True, choices=["launchd", "systemd", "cron"]); sh.add_argument("--db", required=True); sh.add_argument("--out", required=True)
    sh.add_argument("--dest", required=True); sh.add_argument("--python"); sh.add_argument("--log-dir"); sh.add_argument("--hour", type=int, default=3); sh.add_argument("--minute", type=int, default=30)
    sh.add_argument("--keep-last", type=int, default=3); sh.add_argument("--keep-daily", type=int, default=14); sh.add_argument("--max-backup-age-hours", type=float, default=26.0)
    sh.add_argument("--max-drill-age-hours", type=float, default=26.0); sh.add_argument("--external", action="store_true"); sh.add_argument("--env-file")
    ei = sub.add_parser("external-init"); ei.add_argument("--env-file", required=True); ei.add_argument("--out", required=True)
    ek = sub.add_parser("external-rotate-key"); ek.add_argument("--env-file", required=True); ek.add_argument("--out", required=True); ek.add_argument("--new-password-file", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run":
            external, ext_err = (_external_from_args(a, a.out) if a.external else (None, None))
            try:
                res = run(a.db, a.out, keep_last=a.keep_last, keep_daily=a.keep_daily, drill_every_hours=a.drill_every_hours, external=external, external_error=ext_err,
                          external_drill_every_hours=a.external_drill_every_hours)
            except RoutineBusy as e:
                print(f"OMITIDA: {e}", file=sys.stderr)
                return EXIT_BUSY
            if a.json:
                print(json.dumps(res, ensure_ascii=False))
            elif res["exit"] == 2:
                print(f"FALLO: {res['error']}", file=sys.stderr)
            else:
                d, e = res["drill"], res["external"]
                print(f"{'OK' if res['exit'] == 0 else 'AVISO'} copia={res['backup']['file']} filas={res['backup']['rows']} "
                      + (f"ensayo=restaurar {d['restore_seconds']}s + arrancar {d['boot_seconds']}s " if d else "ensayo=omitido ")
                      + f"borradas={len(res['retention']['removed'])} " + (f"externo={'ok' if e['ok'] else 'FALLÓ(' + str(e['code']) + ')'}" if e else "externo=no"))
                for w in res["warnings"]:
                    print(f"AVISO: {w}", file=sys.stderr)
            return res["exit"]
        if a.cmd == "status":
            st = status(a.out, max_backup_age_hours=a.max_backup_age_hours, max_drill_age_hours=a.max_drill_age_hours, external_required=a.external_required,
                        max_external_age_hours=a.max_external_age_hours)
            print(json.dumps(st, ensure_ascii=False, indent=1)) if a.json else _print_status(st)
            return st["exit"]
        if a.cmd == "scrub":
            external, ext_err = (_external_from_args(a, a.out) if a.external else (None, None))
            res = scrub(a.out, a.count, external, external_requested=a.external, external_error=ext_err, external_read_data=a.read_data)
            ext = res["external"]
            local = f"{res['checked']} copias locales re-verificadas"
            if res["ok"]:
                print(f"OK: {local}; repositorio externo verificado" if ext["performed"] else f"OK: {local} (solo copias locales: no se pidió el destino externo)")
                return 0
            if res["bad"]:
                print(f"FALLO: copias locales dañadas: {', '.join(b['file'] for b in res['bad'])} ({local})", file=sys.stderr)
            if ext["requested"] and not ext["ok"]:
                what = "verificación externa NO realizada" if not ext["performed"] else "el repositorio externo falló la verificación"
                print(f"FALLO: {what} ({ext.get('code')}): {ext.get('error')}" + ("" if res["bad"] else f"; {local}"), file=sys.stderr)
            return 2
        if a.cmd == "schedule":
            from . import backup_schedule
            return backup_schedule.cli(a)
        if a.cmd in ("external-init", "external-rotate-key"):
            ext, err = _external_from_args(a, a.out)
            if ext is None:
                print(f"FALLO: {err}", file=sys.stderr)
                return 2
            if a.cmd == "external-init":
                ext.init()
                print("OK repositorio externo creado (cifrado). Guarde la contraseña en DOS lugares fuera de este equipo: sin ella los datos son irrecuperables.")
            else:
                ext.rotate_password(a.new_password_file)
                print("OK contraseña rotada: ya solo abre el archivo nuevo. Reemplace AICOS_BACKUP_RESTIC_PASSWORD_FILE por el archivo nuevo y destruya el anterior.")
            return 0
    except (BackupError, ExternalError) as e:
        print(f"FALLO: {redact(str(e))}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"FALLO: {backup._os_failure(e, 'error del sistema de archivos')}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
