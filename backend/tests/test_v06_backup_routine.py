"""Issue #15 — rutina de respaldos: backup → verificación → restauración → arranque de la app → retención → (destino externo) → estado.
Cada prueba nombra el fallo que impide. Todo sobre bases TEMPORALES de pytest, jamás sobre aicos.db ni demo_comercial.sqlite3."""
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import backup
from app import backup_routine as routine
from app.api import create_app

from .bkutil import BACKEND, bloat, child_env, cli, fingerprint, flip_byte, install_clock, make_live, write_rows


@pytest.fixture
def live(tmp_path):
    return make_live(tmp_path)


@pytest.fixture
def out(tmp_path):
    return tmp_path / "bk"


class FakeExternal:
    """A duck-typed off-machine destination: records what the routine asks of it and fails on demand."""

    def __init__(self, fail_push=None):
        self.calls, self.fail_push, self.snapshot = [], fail_push, 0

    def push(self, files):
        self.calls.append(("push", sorted(Path(f).name for f in files)))
        if self.fail_push:
            from app.backup_external import ExternalError
            raise ExternalError(self.fail_push, "destino simulado caído")
        self.snapshot += 1
        self.files = [Path(f) for f in files]
        return {"snapshot": f"snap{self.snapshot:04d}", "seconds": 0.01}

    def forget(self):
        self.calls.append(("forget",))
        return {"removed": 0}

    def fetch(self, snapshot, target):
        self.calls.append(("fetch", snapshot))
        import shutil
        for f in self.files:
            shutil.copy2(f, Path(target) / f.name)
        return [Path(target) / f.name for f in self.files]


# =============================================================================================== una pasada completa
def test_one_pass_creates_verifies_restores_boots_and_prunes_without_touching_the_source(live, out):
    before = fingerprint(live)
    r = routine.run(live, out)
    assert r["status"] == "ok" and r["exit"] == 0 and r["error"] is None and r["warnings"] == []
    f = out / r["backup"]["file"]
    assert backup.verify(f)["ok"] is True
    d = r["drill"]
    assert d["ok"] is True and d["sessions"] == 0 and d["active"] is True                    # sesión revocada, instancia ACTIVA (lease limpiado) en la copia restaurada
    assert d["restore_seconds"] > 0 and d["boot_seconds"] > 0 and abs(d["total_seconds"] - (d["restore_seconds"] + d["boot_seconds"])) < 1e-3
    assert r["external"] is None and r["retention"]["removed"] == []
    assert fingerprint(live) == before                                                       # el origen solo se abre de lectura
    assert stat.S_IMODE(out.stat().st_mode) == 0o700 and stat.S_IMODE((out / ".routine").stat().st_mode) == 0o700
    assert stat.S_IMODE((out / ".routine" / "journal.jsonl").stat().st_mode) == 0o600
    assert sorted(p.name for p in out.iterdir()) == sorted([f.name, f.name + ".manifest.json", ".routine"])
    assert [p.name for p in (out / ".routine").iterdir() if p.name not in ("journal.jsonl", "run.lock")] == []     # ni copia temporal ni restos del ensayo
    j = routine.read_journal(out)
    assert len(j) == 1 and j[0]["event"] == "run" and j[0]["status"] == "ok" and j[0]["backup"]["file"] == f.name


def test_the_backup_file_stays_an_exact_copy_while_the_restored_copy_is_the_one_that_is_cleaned(live, out):
    app = create_app(f"sqlite:///{live}")                                                    # un servidor VIVO: dueño del lease y con sesión abierta
    try:
        r = routine.run(live, out)
    finally:
        app.state.orch.keeper and app.state.orch.keeper.stop(); app.state.eng.dispose()
    assert r["status"] == "ok" and r["drill"]["active"] is True and r["drill"]["sessions"] == 0
    import sqlite3
    con = sqlite3.connect(f"file:{out / r['backup']['file']}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT count(*) FROM instance_lease").fetchone()[0] >= 1         # la copia guardada es EXACTA (con el lease del origen) …
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 1
    finally:
        con.close()                                                                          # … la RESTAURADA (la del ensayo) no lo traía: por eso `active` es True arriba


def test_runs_are_idempotent_distinct_and_all_verify(live, out):
    names = []
    for i in range(3):
        write_rows(live, 1 + i)
        r = routine.run(live, out)
        assert r["status"] == "ok"
        names.append(r["backup"]["file"])
    assert len(set(names)) == 3 and [b["file"] for b in backup.list_backups(out)] == names[::-1]
    assert all(backup.verify(out / n)["ok"] for n in names) and len(routine.read_journal(out)) == 3


def test_a_run_while_the_server_keeps_writing_is_consistent(live, out):
    stop, errors = threading.Event(), []

    def writer():
        try:
            while not stop.is_set():
                write_rows(live)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=writer); t.start()
    try:
        results = [routine.run(live, out) for _ in range(2)]
    finally:
        stop.set(); t.join()
    assert not errors and all(r["status"] == "ok" and r["drill"]["ok"] for r in results)


def test_retention_keeps_only_the_configured_number_of_backups(live, out):
    for _ in range(5):
        write_rows(live)
        routine.run(live, out, keep_last=2, keep_daily=0)
    assert len(backup.list_backups(out)) == 2


# =============================================================================================== fallos (cada uno reproducido, no supuesto)
def test_a_failed_snapshot_deletes_nothing_and_is_reported(live, out, monkeypatch):
    good = routine.run(live, out)
    write_rows(live)

    def boom(*a, **k):
        raise backup.BackupError("la copia falló: simulada")

    monkeypatch.setattr(backup, "_snapshot", boom)
    r = routine.run(live, out, keep_last=1, keep_daily=0)
    assert r["status"] == "failed" and r["exit"] == 2 and "simulada" in r["error"]
    assert [b["file"] for b in backup.list_backups(out)] == [good["backup"]["file"]]        # la retención solo corre tras un éxito completo
    assert routine.read_journal(out)[-1]["status"] == "failed"


def test_a_drill_that_fails_blocks_retention_and_the_external_push_and_the_status_is_critical(live, out, monkeypatch):
    for _ in range(3):
        write_rows(live)
        backup.create(live, out)
    before = {b["file"] for b in backup.list_backups(out)}

    def cannot_boot(*a, **k):
        raise backup.BackupError("la copia restaurada arrancó en espera")

    monkeypatch.setattr(backup, "boot_check", cannot_boot)
    ext = FakeExternal()
    write_rows(live)
    r = routine.run(live, out, keep_last=1, keep_daily=0, external=ext)
    assert r["status"] == "failed" and r["exit"] == 2 and r["drill"]["ok"] is False
    assert before <= {b["file"] for b in backup.list_backups(out)}                            # nada se borró
    assert ext.calls == []                                                                    # y nada que no se pudo restaurar sale de la máquina
    s = routine.status(out)
    assert s["level"] == "critical" and any(p["code"] == "drill-failed" for p in s["problems"])


def test_a_real_size_limit_while_writing_the_snapshot_is_a_clean_failure(live, out):
    """Disco lleno de verdad: el kernel corta la escritura (RLIMIT_FSIZE) en mitad de la copia, sin simulaciones dentro del código."""
    good = routine.run(live, out)
    bloat(live, 3)
    code = ("import resource, signal, sys, runpy\n"
            "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
            "resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000, 1_000_000))\n"
            "sys.argv = ['app.backup_routine'] + sys.argv[1:]\n"
            "runpy.run_module('app.backup_routine', run_name='__main__')\n")
    p = subprocess.run([sys.executable, "-W", "ignore", "-c", code, "run", "--db", str(live), "--out", str(out), "--keep-last", "1", "--keep-daily", "0"],
                       cwd=BACKEND, env=child_env(), capture_output=True, text=True, timeout=120)
    assert p.returncode == 2 and "Traceback" not in p.stderr and "FALLO" in p.stderr, (p.returncode, p.stderr[-400:])
    assert [b["file"] for b in backup.list_backups(out)] == [good["backup"]["file"]]        # ni una copia nueva ni una borrada
    assert [x.name for x in out.iterdir() if x.name.startswith(".aicos-work")] == []
    last = routine.read_journal(out)[-1]
    assert last["status"] == "failed" and last["error"]
    again = routine.run(live, out)                                                           # liberado el límite, la siguiente pasada funciona sin intervención
    assert again["status"] == "ok"


def test_an_unwritable_destination_fails_closed_with_exit_2(live, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignora los permisos")
    ro = tmp_path / "ro"; ro.mkdir(); os.chmod(ro, 0o500)
    try:
        p = cli("run", "--db", live, "--out", ro)
    finally:
        os.chmod(ro, 0o700)
    assert p.returncode == 2 and "FALLO" in p.stderr and "Traceback" not in p.stderr
    assert list(ro.iterdir()) == []
    assert routine.status(ro)["level"] == "critical"                                         # sin copias → crítico, aunque nadie haya podido escribir el diario


CRASH = r'''
import os, runpy, signal, sys
point = os.environ["CRASH_AT"]
from app import backup
if point == "snapshot":                      # el proceso muere con la copia A MEDIO ESCRIBIR
    real = backup._snapshot
    def snap(src, dst):
        real(src, dst)
        with open(dst, "r+b") as f:
            f.truncate(os.path.getsize(dst) // 2)
        os.kill(os.getpid(), signal.SIGKILL)
    backup._snapshot = snap
elif point == "between-data-and-manifest":   # el archivo de datos ya es visible, el manifiesto todavía no
    real = os.replace
    def rep(src, dst, *a, **k):
        if str(dst).endswith(".manifest.json"):
            os.kill(os.getpid(), signal.SIGKILL)
        return real(src, dst, *a, **k)
    os.replace = rep
sys.argv = ["app.backup_routine"] + sys.argv[1:]
runpy.run_module("app.backup_routine", run_name="__main__")
'''


@pytest.mark.parametrize("point", ["snapshot", "between-data-and-manifest"])
def test_sigkill_in_the_middle_leaves_nothing_trusted_and_the_next_run_heals(live, out, point):
    good = routine.run(live, out)
    write_rows(live)
    p = subprocess.run([sys.executable, "-W", "ignore", "-c", CRASH, "run", "--db", str(live), "--out", str(out)], cwd=BACKEND, env=child_env({"CRASH_AT": point}),
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == -signal.SIGKILL, (p.returncode, p.stderr[-300:])
    assert [b["file"] for b in backup.list_backups(out)] == [good["backup"]["file"]]        # lo que murió a medias NO es una copia: sin manifiesto no se lista ni se restaura
    debris = [x for x in out.iterdir() if x.name.startswith((".aicos-work", "aicos-")) and x.name not in (good["backup"]["file"], good["backup"]["file"] + ".manifest.json")]
    assert debris, "el cierre brusco debía dejar restos (si no, la prueba no prueba nada)"
    s = routine.status(out)
    assert s["backups"]["newest_verified"]["file"] == good["backup"]["file"]                  # el estado ignora los restos
    healed = routine.run(live, out)                                                           # el cerrojo lo suelta el kernel al morir el proceso: la siguiente pasada entra de inmediato
    assert healed["status"] == "ok"
    assert {x.name for x in out.iterdir() if x.name.startswith((".aicos-work", "aicos-"))} >= {x.name for x in debris}, "restos recientes: podrían ser de un `create` vivo, no se tocan"
    old = time.time() - 7200
    for x in debris:
        for y in ([x] + list(x.iterdir()) if x.is_dir() else [x]):
            os.utime(y, (old, old))                                                            # ya son viejos (también lo de dentro): restos seguros de limpiar
    routine.run(live, out)
    assert not [x for x in debris if x.exists()]
    assert all(backup.verify(out / b["file"])["ok"] for b in backup.list_backups(out))


def test_a_drill_killed_halfway_does_not_leave_a_full_copy_of_the_data_behind(live, out):
    routine.run(live, out)
    for name in (".drill-muerto", ".extdrill-muerto"):                                          # lo que dejaría un ensayo (local o externo) muerto con SIGKILL
        d = out / ".routine" / name; d.mkdir(mode=0o700); (d / "restore.sqlite3").write_bytes(b"copia completa de los datos")
    (out / ".routine" / "restic-cache").mkdir()
    routine.run(live, out)
    assert sorted(p.name for p in (out / ".routine").iterdir()) == ["journal.jsonl", "restic-cache", "run.lock"]       # restos fuera; lo demás intacto


# =============================================================================================== tarea duplicada / concurrencia
def test_a_second_invocation_while_one_is_running_is_skipped_with_exit_75(live, out):
    routine.run(live, out)
    with routine.exclusive(out):                                                              # "otra ejecución en curso" (el cerrojo es del kernel: lo ve cualquier proceso)
        p = cli("run", "--db", live, "--out", out)
        assert p.returncode == 75 and "en curso" in p.stderr and "Traceback" not in p.stderr
        with pytest.raises(routine.RoutineBusy):
            with routine.exclusive(out):
                pass
    assert len(backup.list_backups(out)) == 1
    last = routine.read_journal(out)[-1]
    assert last["status"] == "skipped" and last["event"] == "run"
    assert routine.run(live, out)["status"] == "ok"                                           # liberado, vuelve a funcionar


GATED = r'''
import os, runpy, sys, time
deadline = time.time() + 60                       # un trabajador cuya prueba murió no debe girar para siempre como huérfano
while not os.path.exists(os.environ["GATE"]):
    if time.time() > deadline:
        sys.exit(3)
    time.sleep(0.0005)
sys.argv = ["app.backup_routine"] + sys.argv[1:]
runpy.run_module("app.backup_routine", run_name="__main__")
'''


def test_five_simultaneous_scheduler_invocations_never_overlap(live, out, tmp_path):
    bloat(live, 12)                                                                           # cada pasada tarda lo bastante para que las demás choquen con ella
    routine.run(live, out)
    gate = tmp_path / "go"
    env = child_env({"GATE": str(gate)})
    procs = [subprocess.Popen([sys.executable, "-W", "ignore", "-c", GATED, "run", "--db", str(live), "--out", str(out), "--keep-last", "20"], cwd=BACKEND, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(5)]
    time.sleep(1.5); gate.write_text("go")
    codes = sorted(p.wait(timeout=180) for p in procs)
    assert set(codes) <= {0, 75} and codes.count(0) >= 1, codes
    entries = routine.read_journal(out)
    ran = sorted((e for e in entries if e["status"] in ("ok", "warn")), key=lambda e: e["t0"])
    assert len(ran) == 1 + codes.count(0)
    for a, b in zip(ran, ran[1:]):
        assert a["t1"] <= b["t0"], "dos pasadas se solaparon: el cerrojo no exclutye"
    assert sum(1 for e in entries if e["status"] == "skipped") == codes.count(75)
    assert len(backup.list_backups(out)) == len(ran)
    assert all(backup.verify(out / b["file"])["ok"] for b in backup.list_backups(out))


# =============================================================================================== destino externo (dobles: la prueba con restic real está en test_v06_backup_external.py)
def test_a_good_run_pushes_then_forgets_and_does_a_first_external_restore_drill(live, out):
    ext = FakeExternal()
    r = routine.run(live, out, external=ext)
    assert r["status"] == "ok" and r["external"]["ok"] is True and r["external"]["snapshot"] == "snap0001"
    kinds = [c[0] for c in ext.calls]
    assert kinds == ["push", "forget", "fetch"]                                                # sube → aplica retención externa → comprueba que lo subido se restaura
    assert r["external"]["restore_drill"]["ok"] is True
    r2 = routine.run(live, out, external=ext, external_drill_every_hours=168)
    assert [c[0] for c in ext.calls[3:]] == ["push", "forget"]                                 # la restauración externa cuesta descarga: no se repite en cada pasada


def test_a_failing_destination_is_a_warning_the_local_copy_stays_and_forget_does_not_run(live, out):
    ext = FakeExternal(fail_push="timeout")
    r = routine.run(live, out, external=ext)
    assert r["status"] == "warn" and r["exit"] == 1 and r["external"]["ok"] is False and r["external"]["code"] == "timeout"
    assert [c[0] for c in ext.calls] == ["push"]                                               # no se aplica retención remota si la subida falló
    assert backup.verify(out / r["backup"]["file"])["ok"] and r["drill"]["ok"] is True
    s = routine.status(out)
    assert s["level"] == "warn" and s["exit"] == 1 and any(p["code"] == "external-failed" for p in s["problems"])
    strict = routine.status(out, external_required=True)
    assert strict["level"] == "critical" and strict["exit"] == 2


# =============================================================================================== estado (RPO / RTO medidos, alertas)
def test_status_after_a_good_run_is_ok_and_reports_measured_rpo_and_rto(live, out):
    r = routine.run(live, out)
    s = routine.status(out)
    assert s["level"] == "ok" and s["exit"] == 0 and s["problems"] == []
    assert s["backups"]["newest_verified"]["file"] == r["backup"]["file"] and s["rpo"]["current_age_hours"] < 0.05
    assert s["drill"]["last_ok_age_hours"] < 0.05 and s["drill"]["measured_rto_seconds"] > 0
    assert s["drill"]["sessions_revoked"] is True and s["drill"]["active_after_restore"] is True


def test_status_measures_the_worst_gap_between_good_backups_not_just_the_current_age(live, out, monkeypatch):
    clk = install_clock(monkeypatch)
    for gap in (0, 8, 8, 24):                                                                 # un fin de semana sin copia: el peor hueco real fue de 24 h
        clk.advance(hours=gap)
        write_rows(live)
        routine.run(live, out, keep_last=10)
    s = routine.status(out)
    assert 23.9 <= s["rpo"]["worst_gap_hours_7d"] <= 24.1 and s["rpo"]["current_age_hours"] < 0.01
    assert s["rpo"]["worst_case_hours"] >= 23.9


def test_status_fails_closed_when_there_is_nothing_to_restore(tmp_path):
    missing = tmp_path / "nada"
    s = routine.status(missing)
    assert s["level"] == "critical" and s["exit"] == 2 and s["problems"][0]["code"] == "out-missing" and not missing.exists()       # no inventa el directorio
    missing.mkdir()
    s = routine.status(missing)
    assert s["level"] == "critical" and s["problems"][0]["code"] == "no-backups"


def test_status_goes_critical_when_the_last_good_backup_is_older_than_the_target(live, out, monkeypatch):
    clk = install_clock(monkeypatch)
    routine.run(live, out)
    clk.advance(hours=25.9)
    assert routine.status(out)["level"] == "ok"
    clk.advance(hours=0.2)
    s = routine.status(out)
    assert s["level"] == "critical" and s["exit"] == 2 and any(p["code"] == "backup-stale" for p in s["problems"])
    assert "app.backup_routine run" in next(p for p in s["problems"] if p["code"] == "backup-stale")["action"]


def test_status_warns_when_backups_are_fresh_but_the_restore_drill_is_overdue(live, out, monkeypatch):
    clk = install_clock(monkeypatch)
    routine.run(live, out)
    clk.advance(hours=20)
    write_rows(live)
    backup.create(live, out)                                                                   # copia manual: reciente, pero sin ensayo
    clk.advance(hours=10)
    write_rows(live)
    backup.create(live, out)
    s = routine.status(out)
    assert s["level"] == "warn" and s["exit"] == 1 and [p["code"] for p in s["problems"]] == ["drill-stale"]


def test_status_does_not_trust_the_journal_over_the_files(live, out):
    first = routine.run(live, out)
    write_rows(live)
    second = routine.run(live, out)
    flip_byte(out / second["backup"]["file"])                                                  # el diario dice "ok", el archivo ya no
    s = routine.status(out)
    assert s["backups"]["newest"]["verified"] is False
    assert s["backups"]["newest_verified"]["file"] == first["backup"]["file"]
    assert any(p["code"] == "newest-corrupt" for p in s["problems"]) and s["level"] in ("warn", "critical")
    flip_byte(out / first["backup"]["file"])
    s = routine.status(out)
    assert s["level"] == "critical" and any(p["code"] == "no-verified-backup" for p in s["problems"])


def test_status_without_a_journal_is_a_warning_never_a_pass(live, out):
    routine.run(live, out)
    (out / ".routine" / "journal.jsonl").unlink()
    s = routine.status(out)
    assert s["level"] == "warn" and any(p["code"] == "no-drill" for p in s["problems"])


def test_status_output_carries_no_paths_hashes_or_internal_details(live, out):
    routine.run(live, out)
    text = json.dumps(routine.status(out))
    assert str(out) not in text and str(live) not in text and str(BACKEND) not in text
    assert "argon2" not in text and "scrypt" not in text and "password" not in text.lower().replace("passwords", "")
    assert '"sha256"' not in text                                                              # el hash completo no sale del directorio de copias


def test_cli_status_exit_codes_match_the_levels(live, out):
    assert cli("status", "--out", out).returncode == 2                                         # nada que restaurar
    assert cli("run", "--db", live, "--out", out).returncode == 0
    ok = cli("status", "--out", out)
    assert ok.returncode == 0 and "OK" in ok.stdout
    js = cli("status", "--out", out, "--json")
    assert json.loads(js.stdout)["level"] == "ok"
    (out / ".routine" / "journal.jsonl").unlink()
    assert cli("status", "--out", out).returncode == 1


def test_scrub_reverifies_stored_backups_and_flags_bit_rot(live, out):
    for _ in range(3):
        write_rows(live)
        routine.run(live, out, keep_last=5)
    names = [b["file"] for b in backup.list_backups(out)]
    assert routine.scrub(out, count=3)["ok"] is True
    flip_byte(out / names[1])
    res = routine.scrub(out, count=3)
    assert res["ok"] is False and [b["file"] for b in res["bad"]] == [names[1]]
    assert cli("scrub", "--out", out).returncode == 2


# =============================================================================================== diario
def test_the_journal_is_bounded_private_and_tolerates_garbage(out):
    for i in range(routine.JOURNAL_MAX + 200):
        routine.journal_append(out, {"event": "run", "status": "ok", "i": i})
    j = routine.journal_path(out)
    with open(j, "a") as f:
        f.write('{"event": "run", "status": "ok", "i": truncado por un corte de luz\n')
    routine.journal_append(out, {"event": "run", "status": "ok", "i": 99999})
    entries = routine.read_journal(out)
    assert len(entries) <= routine.JOURNAL_MAX and entries[-1]["i"] == 99999 and all(isinstance(e, dict) for e in entries)
    assert sum(1 for _ in open(j)) <= routine.JOURNAL_MAX + 150                                  # el archivo tampoco crece sin fin
    assert stat.S_IMODE(j.stat().st_mode) == 0o600


def test_the_journal_never_stores_secrets_even_if_an_error_message_carries_them(out):
    routine.journal_append(out, {"event": "run", "status": "failed", "error": "fallo rest:https://usuario:CLAVE-SECRETA-XYZ@host.example/repo y B2_ACCOUNT_KEY=CLAVE-SECRETA-XYZ"})
    text = routine.journal_path(out).read_text()
    assert "CLAVE-SECRETA-XYZ" not in text and "usuario" not in text


# ====================================================================================== Issue #15 · revisión del PR #16 · revisión automatizada P2: `scrub --external` no puede dar un éxito falso
def _private_file(path, text, mode=0o600):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


MARKER = "MARCA-SECRETA-no-debe-salir-7c1d"


def _external_config_cases(tmp_path):
    """Every way the requested external check can be impossible: no configuration at all, a missing / over-permissive / unreadable-key file, and a password file that is gone."""
    pw = _private_file(tmp_path / "pw", "contraseña-de-prueba-0123456789-no-es-real")
    good = f"AICOS_BACKUP_RESTIC_REPOSITORY={tmp_path / 'repo'}\nAICOS_BACKUP_RESTIC_PASSWORD_FILE={pw}\nB2_ACCOUNT_KEY={MARKER}\n"
    return {
        "sin-configuracion": [],
        "archivo-de-entorno-inexistente": ["--env-file", str(tmp_path / "no-existe.env")],
        "archivo-con-permisos-abiertos": ["--env-file", str(_private_file(tmp_path / "abierto.env", good, 0o644))],
        "clave-no-permitida": ["--env-file", str(_private_file(tmp_path / "rara.env", good + "LD_PRELOAD=/tmp/x.so\n"))],
        "contrasena-inexistente": ["--env-file", str(_private_file(tmp_path / "sinpw.env", good.replace(str(pw), str(tmp_path / "se-fue.pw"))))],
    }


@pytest.mark.parametrize("case", ["sin-configuracion", "archivo-de-entorno-inexistente", "archivo-con-permisos-abiertos", "clave-no-permitida", "contrasena-inexistente"])
def test_scrub_external_with_a_configuration_that_cannot_load_fails_and_never_reports_the_external_repository_as_checked(live, out, tmp_path, case):
    """revisión automatizada P2: antes `--external` con una credencial ausente o inválida imprimía «OK: N copias re-verificadas» y salía con 0: el repositorio externo jamás se tocó."""
    routine.run(live, out)
    p = cli("scrub", "--out", out, "--external", *_external_config_cases(tmp_path)[case])
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert "verificación externa NO realizada" in p.stderr and "Traceback" not in p.stderr
    assert not p.stdout.startswith("OK")                                                                         # nunca «OK» cuando lo pedido no ocurrió
    last = routine.read_journal(out)[-1]
    assert last["event"] == "scrub" and last["status"] == "failed"
    assert last["external"]["requested"] is True and last["external"]["performed"] is False and last["external"]["ok"] is False and last["external"]["code"] == "config"
    assert last["checked"] >= 1 and last["bad"] == []                                                            # lo local SÍ se verificó, y se dice por separado
    everything = p.stdout + p.stderr + routine.journal_path(out).read_text()
    assert MARKER not in everything and "/tmp/x.so" not in everything                                             # ni el valor de una credencial ni el de una clave rechazada


def test_scrub_external_that_cannot_reach_the_repository_is_a_failure_too(live, out, tmp_path):
    routine.run(live, out)
    pw = _private_file(tmp_path / "pw", "contraseña-de-prueba-0123456789-no-es-real")
    env = _private_file(tmp_path / "ok.env", f"AICOS_BACKUP_RESTIC_REPOSITORY={tmp_path / 'repo-que-no-existe'}\nAICOS_BACKUP_RESTIC_PASSWORD_FILE={pw}\n")
    p = cli("scrub", "--out", out, "--external", "--env-file", env)
    if not shutil.which("restic"):
        assert p.returncode == 2 and "binary-missing" in p.stdout + p.stderr                                      # sin restic tampoco hay éxito falso
    else:
        assert p.returncode == 2 and "repo-missing" in p.stdout + p.stderr and not (tmp_path / "repo-que-no-existe").exists()
    last = routine.read_journal(out)[-1]
    assert last["status"] == "failed" and last["external"]["requested"] is True and last["external"]["performed"] is False and last["external"]["ok"] is False


def test_scrub_without_external_is_unchanged_and_says_it_did_not_look_outside(live, out):
    routine.run(live, out)
    p = cli("scrub", "--out", out)
    assert p.returncode == 0 and p.stdout.startswith("OK") and "solo copias locales" in p.stdout
    last = routine.read_journal(out)[-1]
    assert last["status"] == "ok" and last["external"] == {"requested": False, "performed": False, "ok": None}


def test_the_scrub_api_never_claims_an_external_check_it_did_not_run(live, out):
    routine.run(live, out)
    res = routine.scrub(out, external_requested=True, external_error="falta la configuración del destino")
    assert res["ok"] is False and res["external"]["performed"] is False and res["external"]["requested"] is True and res["external"]["code"] == "config"
    res = routine.scrub(out, external_requested=True)                                                              # pedido sin nada que lo ejecute: tampoco es un éxito
    assert res["ok"] is False and res["external"]["performed"] is False


class _OkCheck:
    """A destination whose repository check passes (the real one is exercised in test_v06_backup_external.py)."""

    def check(self, read_data=False, read_data_subset=None):
        return {"ok": True, "seconds": 0.0}


def test_status_shows_a_failed_external_scrub_until_an_EXTERNAL_scrub_succeeds(live, out):
    routine.run(live, out)
    routine.scrub(out, external_requested=True, external_error="sin configuración")
    s = routine.status(out)
    assert s["level"] == "warn" and [p["code"] for p in s["problems"]] == ["scrub-failed"]
    routine.scrub(out)                                                                                            # un scrub solo local pasa… pero NO despeja lo externo: sería el mismo éxito falso, un paso después
    assert [p["code"] for p in routine.status(out)["problems"]] == ["scrub-failed"]
    routine.scrub(out, external=_OkCheck(), external_requested=True)
    assert routine.status(out)["problems"] == []


def test_status_flags_a_scrub_that_found_a_damaged_local_copy_until_a_clean_one(live, out):
    routine.run(live, out)
    good = routine.run(live, out)
    flip_byte(out / good["backup"]["file"])
    assert routine.scrub(out)["ok"] is False
    assert [p["code"] for p in routine.status(out)["problems"] if p["code"] == "scrub-failed"] == ["scrub-failed"]
    for b in backup.list_backups(out)[:1]:
        (out / b["file"]).unlink(); (out / (b["file"] + ".manifest.json")).unlink()                               # se retira la copia dañada
    assert routine.scrub(out)["ok"] is True
    assert not [p for p in routine.status(out)["problems"] if p["code"] == "scrub-failed"]
