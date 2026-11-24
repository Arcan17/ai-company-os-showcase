"""Issue #15 — programación automática PREPARADA, nunca instalada. El generador solo escribe archivos de texto en un directorio nuevo que el operador revisa; instalarlos es un acto
manual y reversible (docs/v06/BACKUP_RUTINA_Y_DESTINO_EXTERNO.md). Estas pruebas fijan que ni el generador ni la rutina pueden instalar nada."""
import ast
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app import backup, backup_routine as routine
from app import backup_schedule as sched

from .bkutil import BACKEND, cli, make_live


@pytest.fixture
def live(tmp_path):
    return make_live(tmp_path)


def _render(kind, live, out, **kw):
    kw.setdefault("python", "/opt/aicos/venv/bin/python")
    kw.setdefault("log_dir", "/Users/ejemplo/Library/Logs")
    return sched.render(kind, backend_dir=str(BACKEND), db=str(live), out=str(out), **kw)


def test_launchd_files_are_valid_plists_with_a_calendar_schedule_and_no_secrets(live, tmp_path):
    files = _render("launchd", live, tmp_path / "bk", hour=3, minute=30)
    assert sorted(files) == ["local.aicos.backup-status.plist", "local.aicos.backup.plist"]
    job = plistlib.loads(files["local.aicos.backup.plist"].encode())
    assert job["Label"] == "local.aicos.backup" and job["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert job["ProgramArguments"][:3] == ["/opt/aicos/venv/bin/python", "-m", "app.backup_routine"] and job["ProgramArguments"][3] == "run"
    assert job["WorkingDirectory"] == str(BACKEND) and job["LowPriorityIO"] is True and job["ProcessType"] == "Background"
    assert not job.get("RunAtLoad") and not job.get("KeepAlive")                                              # no arranca al iniciar sesión ni se relanza en bucle
    assert job["StandardErrorPath"].startswith("/Users/ejemplo/Library/Logs/")
    args = job["ProgramArguments"]
    assert args[args.index("--db") + 1] == str(live) and args[args.index("--out") + 1] == str(tmp_path / "bk")
    status = plistlib.loads(files["local.aicos.backup-status.plist"].encode())
    assert status["ProgramArguments"][3] == "status" and "StartCalendarInterval" in status
    assert "EnvironmentVariables" not in job and "restic" not in "".join(files.values()).lower()               # sin variables de entorno: ni un secreto puede viajar en el plist


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("plutil"), reason="plutil solo existe en macOS")
def test_launchd_plists_pass_plutil_lint(live, tmp_path):
    for name, text in _render("launchd", live, tmp_path / "bk").items():
        f = tmp_path / name; f.write_text(text)
        p = subprocess.run(["plutil", "-lint", str(f)], capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr


def test_systemd_units_catch_up_after_downtime_and_run_with_low_privilege(live, tmp_path):
    files = _render("systemd", live, tmp_path / "bk", hour=3, minute=30)
    assert sorted(files) == ["aicos-backup-status.service", "aicos-backup-status.timer", "aicos-backup.service", "aicos-backup.timer"]
    timer = files["aicos-backup.timer"]
    assert "OnCalendar=*-*-* 03:30:00" in timer and "Persistent=true" in timer and "RandomizedDelaySec=" in timer      # Persistent: si el equipo estuvo apagado, recupera la corrida perdida
    svc = files["aicos-backup.service"]
    assert svc.count("ExecStart=") == 1 and "Type=oneshot" in svc and "NoNewPrivileges=yes" in svc and "PrivateTmp=yes" in svc and "Nice=10" in svc
    assert "SuccessExitStatus=1" in svc                                                                         # 1 = «terminó con avisos»: no es una unidad fallida
    assert "--db" in svc and "--out" in svc


def test_cron_line_is_a_single_quoted_command(live, tmp_path):
    files = _render("cron", live, tmp_path / "bk", hour=3, minute=30)
    assert list(files) == ["aicos-backup.cron"]
    lines = [ln for ln in files["aicos-backup.cron"].splitlines() if ln and not ln.startswith("#")]
    assert len(lines) == 2 and lines[0].startswith("30 3 * * * ")
    argv = shlex.split(lines[0].split(" ", 5)[5].split(" >>")[0].replace("\\%", "%"))                               # cron convierte "\%" en "%" ANTES de pasar la línea al shell
    logs = "/Users/ejemplo/Library/Logs"
    assert argv[:11] == ["umask", "077", "&&", "mkdir", "-p", "--", logs, "&&", "cd", str(BACKEND), "&&"]            # primero el directorio de logs, con permisos privados; luego el trabajo
    assert "app.backup_routine" in argv and "--db" in argv


@pytest.mark.parametrize("kind", ["launchd", "systemd", "cron"])
def test_awkward_paths_survive_quoting(live, tmp_path, kind):
    nasty = tmp_path / "mis copias $HOME 'x' %h \"y\" ;rm"
    files = _render(kind, live, nasty)
    text = "\n".join(files.values())
    assert "rm -rf" not in text
    if kind == "launchd":
        job = plistlib.loads(files["local.aicos.backup.plist"].encode())
        assert job["ProgramArguments"][job["ProgramArguments"].index("--out") + 1] == str(nasty)               # plist: texto exacto, sin intérprete de shell
    if kind == "cron":
        line = next(ln for ln in files["aicos-backup.cron"].splitlines() if "app.backup_routine run" in ln)
        assert str(nasty) in shlex.split(line.split(" ", 5)[5].split(" >>")[0].replace("\\%", "%"))             # una sola palabra para el shell (y "%" escapado para cron)
        assert not re.search(r"(?<!\\)%", line) and "\\%h" in line                                           # CADA «%» va escapado: cron convierte uno suelto en salto de línea y truncaría el comando
    if kind == "systemd":
        exec_line = next(ln for ln in files["aicos-backup.service"].splitlines() if ln.startswith("ExecStart="))
        assert "%h" not in exec_line.replace("%%h", "") and "$HOME" not in exec_line.replace("$$HOME", "")     # '%' y '$' sin duplicar serían especificadores/variables de systemd


def test_a_newline_in_any_path_is_refused(live, tmp_path):
    with pytest.raises(sched.ScheduleError, match="salto de línea"):
        _render("cron", live, tmp_path / "bk\nrm -rf ~")


def test_the_generator_writes_a_new_directory_and_never_into_an_install_location(live, tmp_path):
    files = _render("launchd", live, tmp_path / "bk")
    dest = tmp_path / "preparado"
    written = sched.write(files, dest)
    assert sorted(p.name for p in written) == sorted(files) and all(oct(p.stat().st_mode & 0o777) == "0o644" for p in written)
    assert sched.render("launchd", python="/opt/aicos/venv/bin/python", backend_dir=str(BACKEND), db=str(live), out=str(tmp_path / "bk"), log_dir="/Users/ejemplo/Library/Logs") == files      # reproducible: mismas entradas → mismos bytes
    with pytest.raises(sched.ScheduleError, match="ya existe"):
        sched.write(files, dest)                                                                                # no pisa lo que el operador ya revisó
    for bad in (tmp_path / "Library" / "LaunchAgents", tmp_path / "Library" / "LaunchDaemons", Path("/etc/systemd/system"), tmp_path / ".config" / "systemd" / "user", Path("/var/spool/cron")):
        with pytest.raises(sched.ScheduleError, match="instalaci"):
            sched.write(files, bad)
    assert not (tmp_path / "Library").exists()


def test_generating_never_runs_a_program_and_the_module_cannot_even_import_subprocess(live, tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("el generador intentó ejecutar un programa del sistema")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    sched.write(_render("launchd", live, tmp_path / "bk"), tmp_path / "preparado")
    tree = ast.parse(Path(sched.__file__).read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    calls = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "subprocess" not in imported and not calls & {"system", "popen", "Popen", "execv", "execvp", "spawnv", "run", "check_call", "check_output"}


def test_the_cli_prepares_files_and_prints_the_manual_install_and_rollback_steps(live, tmp_path):
    dest = tmp_path / "preparado"
    p = cli("schedule", "--kind", "launchd", "--db", live, "--out", tmp_path / "bk", "--dest", dest, "--python", sys.executable, "--log-dir", tmp_path / "logs")
    assert p.returncode == 0, p.stderr
    assert sorted(f.name for f in dest.iterdir()) == ["local.aicos.backup-status.plist", "local.aicos.backup.plist"]
    assert "NO se instaló nada" in p.stdout and "launchctl bootstrap" in p.stdout and "launchctl bootout" in p.stdout     # texto para copiar a mano, nunca ejecutado
    assert not (Path.home() / "Library" / "LaunchAgents" / "local.aicos.backup.plist").exists()
    again = cli("schedule", "--kind", "launchd", "--db", live, "--out", tmp_path / "bk", "--dest", dest)
    assert again.returncode == 2 and "ya existe" in again.stderr


def test_the_cli_refuses_a_relative_or_missing_database_and_a_destination_inside_the_backup_directory(live, tmp_path):
    base = ["schedule", "--kind", "cron", "--out", tmp_path / "bk"]
    assert cli(*base, "--db", "relativa.db", "--dest", tmp_path / "d1").returncode == 2
    assert cli(*base, "--db", tmp_path / "no-existe.db", "--dest", tmp_path / "d2").returncode == 2
    assert cli("schedule", "--kind", "cron", "--db", live, "--out", tmp_path / "bk", "--dest", tmp_path / "bk" / "dentro").returncode == 2
    assert not (tmp_path / "d1").exists() and not (tmp_path / "d2").exists()


def test_scheduled_thresholds_are_consistent_with_the_schedule(live, tmp_path):
    files = _render("launchd", live, tmp_path / "bk", hour=3, minute=30, max_backup_age_hours=26, max_drill_age_hours=26)
    status = plistlib.loads(files["local.aicos.backup-status.plist"].encode())["ProgramArguments"]
    assert status[status.index("--max-backup-age-hours") + 1] == "26" and status[status.index("--max-drill-age-hours") + 1] == "26"


def test_external_flags_travel_as_paths_never_as_values(live, tmp_path):
    files = _render("launchd", live, tmp_path / "bk", external=True, env_file="/Users/ejemplo/.config/aicos/backup.env")
    args = plistlib.loads(files["local.aicos.backup.plist"].encode())["ProgramArguments"]
    assert "--external" in args and args[args.index("--env-file") + 1] == "/Users/ejemplo/.config/aicos/backup.env"
    assert "EnvironmentVariables" not in plistlib.loads(files["local.aicos.backup.plist"].encode())
    with pytest.raises(sched.ScheduleError, match="--env-file"):
        _render("launchd", live, tmp_path / "bk", external=True)                                              # sin archivo de credenciales no hay destino externo


# ====================================================================================== Issue #15 · revisión del PR #16 · revisión automatizada P1: el directorio de logs de cron
def _cron_commands(files) -> list[str]:
    """Exactly what cron hands to /bin/sh: the line without its five time fields, with every "\\%" turned back into "%" (cron does that BEFORE the shell sees it)."""
    return [ln.split(" ", 5)[5].replace("\\%", "%") for ln in files["aicos-backup.cron"].splitlines() if ln and not ln.startswith("#")]


def _sh(command, tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}                                                      # el entorno mínimo que cron da a un trabajo
    return subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True, timeout=300)


def test_the_generated_cron_lines_really_run_under_bin_sh_with_a_log_directory_that_does_not_exist(live, tmp_path):
    """revisión automatizada P1: `>> LOG` abre el archivo ANTES de que arranque Python; con un directorio de logs inexistente el shell falla y ni la copia ni la alarma corren jamás."""
    logs = tmp_path / "estado" / "aicos" / "logs"                                                              # ni «estado» ni «aicos» ni «logs» existen
    out = tmp_path / "bk"
    files = _render("cron", live, out, log_dir=str(logs), python=sys.executable)
    run_cmd, status_cmd = _cron_commands(files)
    p = _sh(run_cmd, tmp_path)
    assert p.returncode == 0, p.stderr
    assert logs.is_dir() and stat.S_IMODE(logs.stat().st_mode) == 0o700 and stat.S_IMODE(logs.parent.stat().st_mode) == 0o700       # privado también en los intermedios
    log = logs / "aicos-backup.log"
    assert "OK copia=" in log.read_text() and stat.S_IMODE(log.stat().st_mode) == 0o600                         # la salida llegó al log, que nació privado
    assert len(backup.list_backups(out)) == 1                                                                   # y la copia SÍ se hizo
    p2 = _sh(status_cmd, tmp_path)
    assert p2.returncode == 0, p2.stderr
    assert "OK:" in (logs / "aicos-backup-status.log").read_text()                                              # la segunda línea (la alarma) también corre


def test_an_existing_log_directory_is_never_chmodded(live, tmp_path):
    logs = tmp_path / "logs-ajenos"; logs.mkdir(); os.chmod(logs, 0o755)                                         # no es nuestro: no se toca
    run_cmd, _ = _cron_commands(_render("cron", live, tmp_path / "bk", log_dir=str(logs), python=sys.executable))
    assert _sh(run_cmd, tmp_path).returncode == 0
    assert stat.S_IMODE(logs.stat().st_mode) == 0o755 and stat.S_IMODE((logs / "aicos-backup.log").stat().st_mode) == 0o600


def test_a_log_directory_that_cannot_be_created_fails_loudly_and_runs_nothing(live, tmp_path):
    blocker = tmp_path / "es-un-archivo"; blocker.write_text("no es un directorio")
    out = tmp_path / "bk"
    run_cmd, _ = _cron_commands(_render("cron", live, out, log_dir=str(blocker / "logs"), python=sys.executable))
    p = _sh(run_cmd, tmp_path)
    assert p.returncode != 0 and p.stderr.strip()                                                               # cron envía esto por correo: se ve, no se pierde en silencio
    assert not out.exists()                                                                                     # y no se ejecutó nada a medias


def test_the_default_log_directory_is_the_platform_one_not_macos_on_linux(live, tmp_path):
    for kind, expected in (("cron", ".local/state/aicos"), ("systemd", ".local/state/aicos"), ("launchd", "Library/Logs")):
        dest = tmp_path / f"p-{kind}"
        p = cli("schedule", "--kind", kind, "--db", live, "--out", tmp_path / f"bk-{kind}", "--dest", dest, "--python", sys.executable, env={"HOME": str(tmp_path / "casa")})
        assert p.returncode == 0, p.stderr
        text = "".join(f.read_text() for f in dest.iterdir())
        if kind != "systemd":
            assert str(tmp_path / "casa" / expected) in text, (kind, text[:300])
        assert (("Library/Logs" in text) == (kind == "launchd")), kind                                          # «~/Library/Logs» solo existe en macOS: launchd y nada más


def test_launchd_needs_the_log_directory_before_it_starts_so_the_printed_steps_create_it_privately(live, tmp_path):
    logs = tmp_path / "logs-nuevos"
    p = cli("schedule", "--kind", "launchd", "--db", live, "--out", tmp_path / "bk", "--dest", tmp_path / "prep", "--python", sys.executable, "--log-dir", logs)
    assert p.returncode == 0, p.stderr
    assert f"mkdir -p -m 700 {logs}" in p.stdout and p.stdout.index("mkdir -p -m 700") < p.stdout.index("launchctl bootstrap")     # antes de instalar, no después
    assert not logs.exists()                                                                                    # el generador sigue sin escribir fuera de --dest
