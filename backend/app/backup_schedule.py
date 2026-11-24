"""Programación automática PREPARADA, nunca instalada (Issue #15).

Este módulo solo transforma parámetros en TEXTO (plist de launchd, unidades de systemd, líneas de cron) y lo escribe en un directorio NUEVO que el operador revisa. No importa
`subprocess`, no llama a `launchctl`/`systemctl`/`crontab` y se niega a escribir dentro de un lugar de instalación: instalar es un acto manual, reversible y explícito
(los pasos se IMPRIMEN, no se ejecutan). No se reinventa un planificador: se usa el del sistema operativo.

 * launchd (macOS): `StartCalendarInterval`. Si el Mac estaba dormido a la hora indicada, launchd ejecuta la tarea al despertar; si estaba APAGADO, no la recupera: el RPO real
   depende de que el equipo esté encendido (por eso existe el trabajo `status`, que avisa cuando la última copia envejece).
 * systemd (Linux/servidor): `OnCalendar` + `Persistent=true` (recupera la corrida perdida al volver a encender).
 * cron: línea clásica; no recupera corridas perdidas.
Los trabajos solo llevan RUTAS. Credenciales del destino externo: únicamente la ruta de un archivo de entorno privado (0600, fuera del repositorio), jamás valores.
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import sys
from pathlib import Path

LABEL = "local.aicos.backup"
STATUS_LABEL = "local.aicos.backup-status"
KINDS = ("launchd", "systemd", "cron")

# Cualquier destino que contenga una de estas secuencias de nombres es un lugar donde el sistema EJECUTA lo que se escriba: aquí no se escribe.
_INSTALL_LOCATIONS = (("Library", "LaunchAgents"), ("Library", "LaunchDaemons"), ("etc", "systemd"), ("systemd", "user"), ("systemd", "system"), ("lib", "systemd"),
                      ("var", "spool", "cron"), ("etc", "cron.d"), ("etc", "cron.daily"), ("etc", "cron.hourly"), ("etc", "periodic"))


class ScheduleError(Exception):
    pass


def _hours(x: float) -> str:
    return f"{x:g}"


def _clean(name: str, value) -> str:
    v = str(value)
    if any(c in v for c in "\n\r\0"):
        raise ScheduleError(f"{name}: contiene un salto de línea o un carácter nulo")
    return v


def _absolute(name: str, value) -> str:
    v = _clean(name, value)
    if not v.startswith("/"):
        raise ScheduleError(f"{name}: debe ser una ruta absoluta (una tarea programada no tiene directorio de trabajo conocido)")
    return v


def _run_args(python, db, out, keep_last, keep_daily, external, env_file) -> list[str]:
    a = [python, "-m", "app.backup_routine", "run", "--db", db, "--out", out, "--keep-last", str(int(keep_last)), "--keep-daily", str(int(keep_daily))]
    if external:
        a += ["--external", "--env-file", env_file]
    return a


def _status_args(python, out, max_backup, max_drill, external, env_file) -> list[str]:
    a = [python, "-m", "app.backup_routine", "status", "--out", out, "--max-backup-age-hours", _hours(max_backup), "--max-drill-age-hours", _hours(max_drill)]
    if external:
        a += ["--external-required"]
    return a


def render(kind: str, *, python, backend_dir, db, out, log_dir, hour: int = 3, minute: int = 30, keep_last: int = 3, keep_daily: int = 14, max_backup_age_hours: float = 26,
           max_drill_age_hours: float = 26, external: bool = False, env_file=None) -> dict[str, str]:
    """{file name → text}. Pure: no I/O, reproducible (same inputs → same bytes)."""
    if kind not in KINDS:
        raise ScheduleError(f"tipo desconocido: {kind} (use {', '.join(KINDS)})")
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ScheduleError("hora/minuto fuera de rango")
    if external and not env_file:
        raise ScheduleError("--external requiere --env-file (un archivo privado con la configuración del destino; nunca valores en la línea de comandos)")
    python, backend_dir, db, out, log_dir = (_absolute(n, v) for n, v in (("--python", python), ("backend", backend_dir), ("--db", db), ("--out", out), ("--log-dir", log_dir)))
    env_file = _absolute("--env-file", env_file) if env_file else None
    run = _run_args(python, db, out, keep_last, keep_daily, external, env_file)
    stat = _status_args(python, out, max_backup_age_hours, max_drill_age_hours, external, env_file)
    # `status` corre cada 6 h, empezando una hora después de la copia: es la alarma si la copia deja de ocurrir
    s_hours = sorted({(int(hour) + 1 + 6 * i) % 24 for i in range(4)})
    return {"launchd": _launchd, "systemd": _systemd, "cron": _cron}[kind](run, stat, backend_dir, log_dir, int(hour), int(minute), s_hours)


def _launchd(run, stat, backend_dir, log_dir, hour, minute, s_hours) -> dict[str, str]:
    def job(label, args, intervals, log):
        d = {"Label": label, "ProgramArguments": args, "WorkingDirectory": backend_dir, "StartCalendarInterval": intervals, "StandardOutPath": f"{log_dir}/{log}",
             "StandardErrorPath": f"{log_dir}/{log}", "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10}
        return plistlib.dumps(d, fmt=plistlib.FMT_XML, sort_keys=False).decode()

    return {f"{LABEL}.plist": job(LABEL, run, {"Hour": hour, "Minute": minute}, "aicos-backup.log"),
            f"{STATUS_LABEL}.plist": job(STATUS_LABEL, stat, [{"Hour": h, "Minute": minute} for h in s_hours], "aicos-backup-status.log")}


_SD_SAFE = re.compile(r"^[A-Za-z0-9_./:@=+,\-]+$")


def _sd_quote(arg: str) -> str:
    """systemd: `%` and `$` are expanded even inside quotes; `\\` and `"` need escaping."""
    if _SD_SAFE.match(arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def _systemd(run, stat, backend_dir, log_dir, hour, minute, s_hours) -> dict[str, str]:
    def service(desc, args, ok_codes):
        return ("# Generado por `python -m app.backup_routine schedule`. NO instalado: revíselo y copie a ~/.config/systemd/user/ (o /etc/systemd/system/) a mano.\n"
                f"[Unit]\nDescription={desc}\n\n[Service]\nType=oneshot\nWorkingDirectory={_sd_quote(backend_dir)}\nExecStart={' '.join(_sd_quote(a) for a in args)}\n"
                f"Nice=10\nIOSchedulingClass=idle\nNoNewPrivileges=yes\nPrivateTmp=yes\nTimeoutStartSec=1h\nSuccessExitStatus={ok_codes}\n")

    def timer(desc, unit, calendar, randomized):
        lines = "".join(f"OnCalendar={c}\n" for c in calendar)
        return (f"# Generado por `python -m app.backup_routine schedule`. NO instalado.\n[Unit]\nDescription={desc}\n\n[Timer]\n{lines}Persistent=true\n"
                f"RandomizedDelaySec={randomized}\nUnit={unit}\n\n[Install]\nWantedBy=timers.target\n")

    return {"aicos-backup.service": service("AI Company OS: copia de seguridad verificada", run, "1"),
            "aicos-backup.timer": timer("AI Company OS: copia diaria", "aicos-backup.service", [f"*-*-* {hour:02d}:{minute:02d}:00"], "10min"),
            "aicos-backup-status.service": service("AI Company OS: estado de las copias (falla si faltan o envejecen)", stat, "0"),
            "aicos-backup-status.timer": timer("AI Company OS: revisión del estado cada 6 h", "aicos-backup-status.service",
                                               [f"*-*-* {h:02d}:{minute:02d}:00" for h in s_hours], "1min")}


def _cron(run, stat, backend_dir, log_dir, hour, minute, s_hours) -> dict[str, str]:
    def line(sched, args, log):
        # `>> LOG` se abre ANTES de que arranque Python: si el directorio no existe el shell falla y ni la copia ni la alarma corren jamás (revisión automatizada P1, PR #16). Por eso la propia línea
        # lo crea primero, con umask 077 (también los intermedios quedan 0700 y el log nace 0600); si no se puede crear, falla a la vista (cron lo envía por correo) y no ejecuta nada a medias.
        cmd = (f"umask 077 && mkdir -p -- {shlex.quote(log_dir)} && cd {shlex.quote(backend_dir)} && {' '.join(shlex.quote(a) for a in args)} "
               f">> {shlex.quote(log_dir + '/' + log)} 2>&1")
        return f"{sched} {cmd.replace('%', chr(92) + '%')}"                       # cron convierte cada "%" sin escapar en un salto de línea

    daily = f"{minute} {hour} * * *"
    status_sched = f"{minute} {','.join(str(h) for h in s_hours)} * * *"
    text = ("# Generado por `python -m app.backup_routine schedule`. NO instalado: revíselo y agréguelo a mano con `crontab -e`.\n"
            "# cron NO recupera corridas perdidas si el equipo estaba apagado o dormido.\n"
            + line(daily, run, "aicos-backup.log") + "\n" + line(status_sched, stat, "aicos-backup-status.log") + "\n")
    return {"aicos-backup.cron": text}


# ====================================================================================================================== escritura (solo a un directorio nuevo)
def _refuse_install_location(dest: Path) -> None:
    parts = Path(os.path.abspath(dest)).parts
    for pat in _INSTALL_LOCATIONS:
        for i in range(len(parts) - len(pat) + 1):
            if tuple(parts[i:i + len(pat)]) == pat:
                raise ScheduleError(f"{dest} es un lugar de instalación del sistema ({'/'.join(pat)}): este comando solo prepara archivos; instálelos usted, a mano, tras revisarlos")


def write(files: dict[str, str], dest) -> list[Path]:
    dest = Path(dest)
    _refuse_install_location(dest)
    if dest.exists():
        raise ScheduleError(f"el destino ya existe ({dest.name}): no se pisa lo que ya revisó; elija un directorio nuevo")
    dest.mkdir(parents=True)
    written = []
    for name, text in files.items():
        p = dest / name
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(p, 0o644)                                              # plists/units no son secretos; launchd exige que no sean escribibles por otros
        written.append(p)
    return written


def _instructions(kind: str, dest: Path, files: dict, log_dir: str) -> str:
    names = " ".join(shlex.quote(str(dest / n)) for n in files)
    if kind == "launchd":
        return ("Instalar (a mano, solo si decide programar):\n"
                f"  (umask 077; mkdir -p -m 700 {shlex.quote(log_dir)})   # launchd abre el log ANTES de ejecutar nada: el directorio debe existir; no lo crea nadie más\n"
                "  mkdir -p ~/Library/LaunchAgents\n"
                f"  cp {names} ~/Library/LaunchAgents/\n"
                f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{LABEL}.plist\n"
                f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{STATUS_LABEL}.plist\n"
                f"  launchctl print gui/$(id -u)/{LABEL}          # comprobar; para probar ya mismo: launchctl kickstart gui/$(id -u)/{LABEL}\n"
                "Deshacer por completo:\n"
                f"  launchctl bootout gui/$(id -u)/{LABEL}; launchctl bootout gui/$(id -u)/{STATUS_LABEL}\n"
                f"  rm ~/Library/LaunchAgents/{LABEL}.plist ~/Library/LaunchAgents/{STATUS_LABEL}.plist\n"
                "Nota: macOS puede pedir permiso para que un trabajo en segundo plano lea ~/Documents (Privacidad y seguridad → Archivos y carpetas / Acceso total al disco).")
    if kind == "systemd":
        return ("Instalar (a mano):\n"
                f"  mkdir -p ~/.config/systemd/user && cp {names} ~/.config/systemd/user/\n"
                "  systemctl --user daemon-reload && systemctl --user enable --now aicos-backup.timer aicos-backup-status.timer\n"
                "Deshacer:\n"
                "  systemctl --user disable --now aicos-backup.timer aicos-backup-status.timer && rm ~/.config/systemd/user/aicos-backup*")
    return ("Instalar (a mano; cada línea crea su directorio de logs con permisos 0700 antes de ejecutar nada):\n"
            "  crontab -l > ~/crontab.antes-de-aicos 2>/dev/null; (cat ~/crontab.antes-de-aicos 2>/dev/null; "
            f"grep -v '^#' {shlex.quote(str(dest / 'aicos-backup.cron'))}) | crontab -\n"
            "Deshacer:\n"
            "  crontab ~/crontab.antes-de-aicos      # o crontab -r si no había ninguno")


def default_log_dir(kind: str) -> str:
    """`~/Library/Logs` exists only on macOS (launchd); cron on Linux gets a private per-user state directory that the generated line creates itself. systemd logs to the journal."""
    home = Path.home()
    return str(home / "Library" / "Logs") if kind == "launchd" else str(home / ".local" / "state" / "aicos")


def cli(a) -> int:
    """`app.backup_routine schedule …` — prepares text files; prints (never runs) the manual steps."""
    try:
        db, out = _absolute("--db", a.db), _absolute("--out", a.out)
        if not Path(db).is_file():
            raise ScheduleError("--db no existe (o no es un archivo)")
        dest = Path(os.path.abspath(a.dest))
        outp = Path(os.path.abspath(out))
        if dest == outp or outp in dest.parents:
            raise ScheduleError("--dest no puede estar dentro del directorio de copias (la retención podría confundir los archivos)")
        backend_dir = str(Path(__file__).resolve().parents[1])
        python = a.python or sys.executable
        if not Path(_absolute("--python", python)).is_file():
            raise ScheduleError("--python no existe")
        log_dir = a.log_dir or default_log_dir(a.kind)
        files = render(a.kind, python=python, backend_dir=backend_dir, db=db, out=out, log_dir=log_dir, hour=a.hour, minute=a.minute, keep_last=a.keep_last,
                       keep_daily=a.keep_daily, max_backup_age_hours=a.max_backup_age_hours, max_drill_age_hours=a.max_drill_age_hours, external=a.external, env_file=a.env_file)
        written = write(files, dest)
    except ScheduleError as e:
        print(f"FALLO: {e}", file=sys.stderr)
        return 2
    print(f"NO se instaló nada. Archivos preparados ({a.kind}):")
    for p in written:
        print(f"  {p}")
    print("\n" + _instructions(a.kind, dest, files, log_dir))
    return 0
