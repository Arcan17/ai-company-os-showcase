"""Destino externo CIFRADO para las copias, con restic (Issue #15). No se escribe criptografía propia: restic está mantenido (v0.20.0, 2026-11-19, BSD-2), cifra todo con AES-256-CTR +
Poly1305-AES y deriva la clave con scrypt, deduplica y puede verificar el repositorio (`check --read-data`). Este módulo solo lo invoca con reglas estrictas:

 * NUNCA crea un repositorio por su cuenta: `init` es un paso explícito del operador (un error de tipeo en la ruta no puede crear un repositorio nuevo en el lugar equivocado).
 * La contraseña va en un ARCHIVO privado (0600, del mismo usuario, no vacío, fuera del repositorio de código) o en un comando (`RESTIC_PASSWORD_COMMAND`, p. ej. el llavero de
   macOS); jamás por argumento ni por variable de entorno. Las credenciales del proveedor (B2_*, AWS_*, …) viajan en un archivo de entorno privado con lista blanca de claves.
 * El proceso hijo recibe un entorno MÍNIMO (PATH, HOME, las claves de la lista blanca): nada más del entorno del Mac llega a restic.
 * Todo comando tiene tiempo máximo; al vencer se mata el grupo de procesos completo. Ninguna salida de error sale sin pasar por `redact`.
 * El fallo del destino nunca detiene la copia local (lo decide la rutina): aquí solo se informa con un código estable
   (`config`, `binary-missing`, `timeout`, `wrong-password`, `repo-missing`, `repo-locked`, `failed`).
"""
from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TAG = "aicos"
HOST = "aicos"                                                    # nombre fijo: el del Mac no debe quedar dentro de las instantáneas

OWN_KEYS = ("AICOS_BACKUP_RESTIC_REPOSITORY", "AICOS_BACKUP_RESTIC_PASSWORD_FILE", "AICOS_BACKUP_RESTIC_PASSWORD_COMMAND", "AICOS_BACKUP_RESTIC_BINARY",
            "AICOS_BACKUP_RESTIC_TIMEOUT", "AICOS_BACKUP_RESTIC_KEEP_DAILY", "AICOS_BACKUP_RESTIC_KEEP_WEEKLY", "AICOS_BACKUP_RESTIC_KEEP_MONTHLY")
PROVIDER_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION", "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY",
                 "AZURE_ACCOUNT_NAME", "AZURE_ACCOUNT_KEY", "AZURE_ACCOUNT_SAS", "GOOGLE_PROJECT_ID", "GOOGLE_APPLICATION_CREDENTIALS")


class ExternalError(Exception):
    """`code` is stable and machine-readable; the message is human text and has already been through `redact`."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(redact(message))
        self.code = code


_URL_USERINFO = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s@]*@")
_ASSIGNED_SECRET = re.compile(r"\b([A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASS|SAS|CREDENTIALS)[A-Z0-9_]*)=\S+")


def redact(text, secrets=()) -> str:
    """Remove credentials from any text that may leave the process (errors, logs, journal): known secret values, `scheme://user:pass@`, and `SOME_KEY=value`."""
    text = str(text)
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    text = _URL_USERINFO.sub(r"\1***@", text)
    return _ASSIGNED_SECRET.sub(r"\1=***", text)


class _Secrets(dict):
    def __repr__(self):                                           # a debugger, a traceback or `vars(obj)` must not print credentials
        return "<credenciales ocultas>"

    __str__ = __repr__


def _check_private_file(path, what: str, forbidden_roots, allow_empty: bool = False) -> Path:
    p = Path(path)
    try:
        st = p.stat()
    except OSError as e:
        raise ExternalError("config", f"{what} no se puede leer ({os.strerror(e.errno) if e.errno else 'error'})") from e
    if not stat.S_ISREG(st.st_mode):
        raise ExternalError("config", f"{what} no es un archivo normal")
    if st.st_mode & 0o077:
        raise ExternalError("config", f"{what} tiene permisos demasiado abiertos ({stat.S_IMODE(st.st_mode):04o}): ejecute chmod 600")
    if st.st_uid != os.geteuid():
        raise ExternalError("config", f"{what} no pertenece al usuario que ejecuta la rutina")
    if st.st_size == 0 and not allow_empty:
        raise ExternalError("config", f"{what} está vacío")
    real = p.resolve()
    for root in forbidden_roots:
        try:
            real.relative_to(Path(root).resolve())
        except ValueError:
            continue
        raise ExternalError("config", f"{what} está dentro del repositorio de código: a un `git add -A` de publicarse. Guárdelo fuera (p. ej. ~/.config/aicos/)")
    return real


def _read_env_file(path, forbidden_roots) -> dict:
    p = _check_private_file(path, "el archivo de entorno del destino externo", forbidden_roots, allow_empty=True)
    allowed = set(OWN_KEYS) | set(PROVIDER_KEYS)
    out = {}
    for n, raw in enumerate(p.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ExternalError("config", f"archivo de entorno: la línea {n} no tiene la forma CLAVE=VALOR")
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k not in allowed:
            raise ExternalError("config", f"archivo de entorno: la clave {k!r} no está permitida (solo AICOS_BACKUP_RESTIC_* y credenciales de proveedor conocidas)")
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


class Restic:
    """One restic repository used as the encrypted off-machine destination."""

    def __init__(self, repository, *, password_file=None, password_command=None, binary: str = "restic", timeout: float = 900.0, cache_dir=None, provider_env=None,
                 keep_daily: int = 7, keep_weekly: int = 4, keep_monthly: int = 12, forbidden_roots=(REPO_ROOT,)):
        repository = str(repository or "")
        if not repository:
            raise ExternalError("config", "falta AICOS_BACKUP_RESTIC_REPOSITORY")
        if _URL_USERINFO.search(repository):
            raise ExternalError("config", "la dirección del repositorio no debe llevar usuario:contraseña; use el archivo de entorno privado")
        if bool(password_file) == bool(password_command):
            raise ExternalError("config", "se necesita exactamente uno de AICOS_BACKUP_RESTIC_PASSWORD_FILE o AICOS_BACKUP_RESTIC_PASSWORD_COMMAND")
        if password_file:
            _check_private_file(password_file, "el archivo de contraseña", forbidden_roots)
        self.repository = repository
        self.password_file = Path(password_file) if password_file else None
        self.password_command = password_command or None
        self.binary, self.timeout = str(binary), float(timeout)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.keep_daily, self.keep_weekly, self.keep_monthly = int(keep_daily), int(keep_weekly), int(keep_monthly)
        self._forbidden = tuple(forbidden_roots)
        self._provider = _Secrets({k: v for k, v in (provider_env or {}).items() if k in PROVIDER_KEYS})

    # ------------------------------------------------------------------------------------------------------------------ construction from the operator's files
    @classmethod
    def from_environment(cls, environ, *, env_file=None, cache_dir=None, forbidden_roots=(REPO_ROOT,)) -> "Restic":
        values = {k: environ[k] for k in tuple(OWN_KEYS) + tuple(PROVIDER_KEYS) if k in environ}
        if env_file:
            values.update(_read_env_file(env_file, forbidden_roots))
        if not values.get("AICOS_BACKUP_RESTIC_REPOSITORY"):
            raise ExternalError("config", "falta AICOS_BACKUP_RESTIC_REPOSITORY (en el archivo de entorno o en el entorno)")
        try:
            kw = {"timeout": float(values.get("AICOS_BACKUP_RESTIC_TIMEOUT", 900)), "keep_daily": int(values.get("AICOS_BACKUP_RESTIC_KEEP_DAILY", 7)),
                  "keep_weekly": int(values.get("AICOS_BACKUP_RESTIC_KEEP_WEEKLY", 4)), "keep_monthly": int(values.get("AICOS_BACKUP_RESTIC_KEEP_MONTHLY", 12))}
        except ValueError as e:
            raise ExternalError("config", "AICOS_BACKUP_RESTIC_TIMEOUT / KEEP_* deben ser números") from e
        return cls(values["AICOS_BACKUP_RESTIC_REPOSITORY"], password_file=values.get("AICOS_BACKUP_RESTIC_PASSWORD_FILE"),
                   password_command=values.get("AICOS_BACKUP_RESTIC_PASSWORD_COMMAND"), binary=values.get("AICOS_BACKUP_RESTIC_BINARY", "restic"),
                   cache_dir=cache_dir, provider_env={k: values[k] for k in PROVIDER_KEYS if k in values}, forbidden_roots=forbidden_roots, **kw)

    def __repr__(self):
        return f"Restic(repositorio={redact(self.repository)!r})"

    # ------------------------------------------------------------------------------------------------------------------ process plumbing
    def _env(self) -> dict:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"), "LC_ALL": "C", "LANG": "C"}
        for k in ("HOME", "TMPDIR"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        env.update(self._provider)
        return env

    def _base(self, password_file=None) -> list[str]:
        pw = password_file or self.password_file
        argv = [self.binary, "--repo", self.repository]
        if pw:
            argv += ["--password-file", str(pw)]
        else:
            argv += ["--password-command", self.password_command]
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            argv += ["--cache-dir", str(self.cache_dir)]
        return argv

    def _run(self, args: list[str], *, password_file=None, timeout=None, ok_codes=(0,)) -> subprocess.CompletedProcess:
        if self.password_file:                                                       # the file may have been replaced or loosened since construction
            _check_private_file(password_file or self.password_file, "el archivo de contraseña", self._forbidden)
        argv = self._base(password_file) + list(args)
        try:
            proc = subprocess.Popen(argv, env=self._env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        except FileNotFoundError as e:
            raise ExternalError("binary-missing", f"no se encontró el programa {Path(self.binary).name!r}; instálelo (brew install restic / apt install restic)") from e
        except PermissionError as e:
            raise ExternalError("binary-missing", f"no se puede ejecutar {Path(self.binary).name!r}") from e
        secrets = list(self._provider.values())
        try:
            out, err = proc.communicate(timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)                                  # restic y todo lo que haya lanzado (p. ej. el comando de contraseña, rclone)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise ExternalError("timeout", f"restic no terminó en {int(timeout or self.timeout)} s; se lo detuvo")
        if proc.returncode not in ok_codes:
            raise ExternalError(self._classify(proc.returncode, err), self._summary(err, secrets))
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    @staticmethod
    def _classify(rc: int, err: str) -> str:
        low = (err or "").lower()
        if rc == 12 or "wrong password" in low:
            return "wrong-password"
        if rc == 10 or "is there a repository at" in low or "repository does not exist" in low or "unable to open config file" in low:
            return "repo-missing"
        if rc == 11 or "unable to create lock" in low or "repository is already locked" in low:
            return "repo-locked"
        return "failed"

    @staticmethod
    def _summary(err: str, secrets) -> str:
        lines = [ln.strip() for ln in (err or "").strip().splitlines() if ln.strip()]
        return redact(" | ".join(lines[-3:]) or "restic terminó con error", secrets)[:400]

    # ------------------------------------------------------------------------------------------------------------------ operations
    def init(self) -> dict:
        """Create the repository. Only ever called by an explicit operator command (`external-init`), never by a scheduled run."""
        self._run(["init"])
        return {"ok": True}

    def push(self, files) -> dict:
        t0 = time.monotonic()
        res = self._run(["backup", "--json", "--tag", TAG, "--host", HOST, *map(str, files)])
        snap = None
        for line in res.stdout.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("message_type") == "summary":
                snap = msg.get("snapshot_id")
        if not snap:
            raise ExternalError("failed", "restic no informó el identificador de la instantánea")
        if not [s for s in self.snapshots() if str(s.get("id", "")).startswith(snap[:8])]:     # lo subido tiene que poder listarse
            raise ExternalError("failed", "la instantánea recién subida no aparece en el repositorio")
        return {"snapshot": snap, "seconds": round(time.monotonic() - t0, 3)}

    def snapshots(self) -> list[dict]:
        res = self._run(["snapshots", "--json", "--tag", TAG])
        try:
            data = json.loads(res.stdout or "[]")
        except ValueError as e:
            raise ExternalError("failed", "restic devolvió una lista de instantáneas ilegible") from e
        return data if isinstance(data, list) else []

    def forget(self) -> dict:
        """Apply the retention policy to OUR series (host+tags: the file names change on every backup, so grouping by path would keep everything). The newest is never forgotten."""
        args = ["forget", "--json", "--tag", TAG, "--host", HOST, "--group-by", "host,tags", "--keep-last", "1"]
        for flag, n in (("--keep-daily", self.keep_daily), ("--keep-weekly", self.keep_weekly), ("--keep-monthly", self.keep_monthly)):
            if n > 0:
                args += [flag, str(n)]
        res = self._run(args)
        try:
            groups = json.loads(res.stdout or "[]")
        except ValueError as e:
            raise ExternalError("failed", "restic devolvió una respuesta de retención ilegible") from e
        removed = sum(len(g.get("remove") or []) for g in groups if isinstance(g, dict)) if isinstance(groups, list) else 0
        if removed:
            self._run(["prune"])                                                      # recupera el espacio (y solo si algo se olvidó: cada prune cuesta operaciones en un proveedor)
        return {"removed": removed}

    def check(self, read_data: bool = False, read_data_subset: str | None = None) -> dict:
        t0 = time.monotonic()
        args = ["check"]
        if read_data:
            args.append("--read-data")
        elif read_data_subset:
            args += ["--read-data-subset", read_data_subset]
        try:
            self._run(args)
        except ExternalError as e:
            if e.code == "failed":
                raise ExternalError("damaged", f"el repositorio externo no pasa la verificación: {e}") from e
            raise
        return {"ok": True, "seconds": round(time.monotonic() - t0, 3)}

    def fetch(self, snapshot: str, target) -> list[Path]:
        """Restore one snapshot into a NEW private directory and return the backup files found in it."""
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._run(["restore", str(snapshot), "--target", str(target)])
        return sorted(p for p in target.rglob("aicos-*") if p.is_file())

    def rotate_password(self, new_password_file) -> dict:
        """Change the repository password (the data is NOT re-encrypted: restic keeps one master key and wraps it per password). The OLD file stops working at once."""
        if not self.password_file:
            raise ExternalError("config", "la rotación requiere contraseña en archivo")
        new = _check_private_file(new_password_file, "el archivo de la contraseña nueva", self._forbidden)
        if new == self.password_file.resolve():
            raise ExternalError("config", "la contraseña nueva es el mismo archivo que la actual")
        self._run(["key", "passwd", "--new-password-file", str(new)])
        self._run(["snapshots", "--json", "--tag", TAG], password_file=new)           # comprobación inmediata: la contraseña nueva ABRE el repositorio
        return {"ok": True}
