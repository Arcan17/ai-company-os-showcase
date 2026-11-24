"""Issue #15 — destino externo CIFRADO con restic (herramienta mantenida; no se escribe cifrado propio). Las pruebas usan un repositorio restic LOCAL en un directorio temporal:
ninguna cuenta, ninguna red, ningún costo. En CI restic se instala y `AICOS_REQUIRE_RESTIC=1` hace que su ausencia FALLE en vez de saltarse en silencio."""
import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from app import backup
from app import backup_external as ext
from app import backup_routine as routine

from .bkutil import bloat, fingerprint, make_live, write_rows

RESTIC = shutil.which("restic")


@pytest.fixture(autouse=True)
def _need_restic():
    if not RESTIC:
        if os.environ.get("AICOS_REQUIRE_RESTIC") == "1":
            pytest.fail("restic no está instalado y este entorno exige probarlo (AICOS_REQUIRE_RESTIC=1)")
        pytest.skip("restic no está instalado en este equipo")


PASSWORD = "contraseña-de-prueba-0123456789-no-es-real"


def _pw_file(path: Path, text: str = PASSWORD, mode: int = 0o600) -> Path:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


@pytest.fixture
def live(tmp_path):
    return make_live(tmp_path)


@pytest.fixture
def repo(tmp_path):
    """A brand-new local restic repository, initialised the way the operator will (explicitly), with its own cache."""
    r = ext.Restic(str(tmp_path / "repo"), password_file=_pw_file(tmp_path / "pw"), cache_dir=tmp_path / "cache", forbidden_roots=())
    r.init()
    return r


def _backup(live, out):
    m = backup.create(live, out)
    return [out / m["file"], out / (m["file"] + ".manifest.json")], m


def _all_bytes(directory: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(Path(directory).rglob("*")) if p.is_file())


def test_the_repository_is_encrypted_while_the_local_backup_is_not(live, repo, tmp_path):
    files, m = _backup(live, tmp_path / "bk")
    plain = files[0].read_bytes()
    assert b"SQLite format 3" in plain and b"dueno@climasur.test" in plain and b"CREATE TABLE" in plain      # control: el archivo local SÍ es legible
    repo.push(files)
    blob = _all_bytes(tmp_path / "repo")
    for marker in (b"SQLite format 3", b"dueno@climasur.test", b"CREATE TABLE", b"audit_log", PASSWORD.encode(), m["file"].encode()):
        assert marker not in blob, f"{marker!r} aparece en claro dentro del repositorio externo"


def test_what_was_pushed_comes_back_and_verifies_and_boots(live, repo, tmp_path):
    files, m = _backup(live, tmp_path / "bk")
    snap = repo.push(files)
    assert snap["snapshot"] and snap["seconds"] >= 0
    got = repo.fetch(snap["snapshot"], tmp_path / "back")
    data = next(p for p in got if p.name.endswith(".sqlite3"))
    assert backup.verify(data)["ok"] is True and fingerprint(data)[0] == m["sha256"]                         # el mismo contenido, bit a bit
    scratch = tmp_path / "boot.db"
    backup.restore(data, scratch)
    assert backup.boot_check(scratch)["active"] is True


def test_a_wrong_or_missing_password_fails_closed_and_changes_nothing(live, repo, tmp_path):
    files, _ = _backup(live, tmp_path / "bk")
    before = sorted(str(p.relative_to(tmp_path / "repo")) for p in (tmp_path / "repo").rglob("*"))
    wrong = ext.Restic(str(tmp_path / "repo"), password_file=_pw_file(tmp_path / "pw2", "otra-contraseña-distinta-9876543210"), cache_dir=tmp_path / "cache", forbidden_roots=())
    with pytest.raises(ext.ExternalError) as e:
        wrong.push(files)
    assert e.value.code == "wrong-password"
    with pytest.raises(ext.ExternalError) as e2:
        ext.Restic(str(tmp_path / "repo"), password_file=tmp_path / "no-existe", cache_dir=tmp_path / "cache", forbidden_roots=()).push(files)
    assert e2.value.code == "config"
    vanished = _pw_file(tmp_path / "pw3", "contraseña-que-desaparece-0123456789")
    r3 = ext.Restic(str(tmp_path / "repo"), password_file=vanished, cache_dir=tmp_path / "cache", forbidden_roots=())
    vanished.unlink()                                                                                          # el archivo desaparece entre la construcción y el uso
    with pytest.raises(ext.ExternalError) as e3:
        r3.push(files)
    assert e3.value.code == "config"
    assert sorted(str(p.relative_to(tmp_path / "repo")) for p in (tmp_path / "repo").rglob("*") if "locks" not in p.parts) == [x for x in before if "locks" not in x.split(os.sep)]


def test_a_repository_that_does_not_exist_is_never_created_by_a_run(live, tmp_path):
    files, _ = _backup(live, tmp_path / "bk")
    r = ext.Restic(str(tmp_path / "typo-repo"), password_file=_pw_file(tmp_path / "pw"), cache_dir=tmp_path / "cache", forbidden_roots=())
    with pytest.raises(ext.ExternalError) as e:
        r.push(files)
    assert e.value.code == "repo-missing" and not (tmp_path / "typo-repo").exists()                           # un error de tipeo no crea un repositorio nuevo en el lugar equivocado


def test_a_missing_binary_is_reported_with_a_code(tmp_path):
    r = ext.Restic(str(tmp_path / "repo"), password_file=_pw_file(tmp_path / "pw"), binary=str(tmp_path / "no-hay-restic"), forbidden_roots=())
    with pytest.raises(ext.ExternalError) as e:
        r.snapshots()
    assert e.value.code == "binary-missing"


def _fake_restic(tmp_path, body: str) -> Path:
    p = tmp_path / "fake-restic"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o700)
    return p


def test_a_hanging_destination_is_killed_with_all_its_children_after_the_timeout(live, tmp_path):
    pidfile = tmp_path / "child.pid"
    fake = _fake_restic(tmp_path, f"sleep 300 &\necho $! > {pidfile}\nwait\n")
    r = ext.Restic(str(tmp_path / "repo"), password_file=_pw_file(tmp_path / "pw"), binary=str(fake), timeout=2, forbidden_roots=())
    files, _ = _backup(live, tmp_path / "bk")
    t0 = time.monotonic()
    with pytest.raises(ext.ExternalError) as e:
        r.push(files)
    assert e.value.code == "timeout" and time.monotonic() - t0 < 20
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("el hijo del proceso colgado sigue vivo: quedaría un huérfano por cada ejecución agendada")


SECRET = "SECRET-MARKER-9f3a1c"


def test_secrets_reach_restic_through_a_whitelist_and_never_reach_logs_journal_or_status(live, tmp_path, monkeypatch):
    envdump = tmp_path / "env.dump"
    fake = _fake_restic(tmp_path, f"env > {envdump}\n"
                                  f"echo 'Fatal: unable to open repo rest:https://usuario:{SECRET}@host.example/repo with B2_ACCOUNT_KEY={SECRET}' >&2\nexit 1\n")
    envfile = _pw_file(tmp_path / "restic.env", f"AICOS_BACKUP_RESTIC_REPOSITORY=b2:bucket:aicos\nAICOS_BACKUP_RESTIC_PASSWORD_FILE={_pw_file(tmp_path / 'pw')}\n"
                                                f"AICOS_BACKUP_RESTIC_BINARY={fake}\nB2_ACCOUNT_ID=id-123\nB2_ACCOUNT_KEY={SECRET}\n")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "no-debe-llegar-a-restic")
    monkeypatch.setenv("RESTIC_PASSWORD", "tampoco-esto")
    r = ext.Restic.from_environment({}, env_file=envfile, cache_dir=tmp_path / "cache", forbidden_roots=())
    out = tmp_path / "bk"
    res = routine.run(live, out, external=r)
    assert res["status"] == "warn" and res["external"]["ok"] is False
    dumped = envdump.read_text()
    assert f"B2_ACCOUNT_KEY={SECRET}" in dumped and "B2_ACCOUNT_ID=id-123" in dumped                         # la credencial SÍ llegó al hijo (es lo que necesita)…
    assert "UNRELATED_API_TOKEN" not in dumped and "RESTIC_PASSWORD" not in dumped                           # … y nada más del entorno del Mac
    everything = json.dumps(res) + routine.journal_path(out).read_text() + json.dumps(routine.status(out))
    assert SECRET not in everything and "usuario" not in everything, "una credencial llegó a la salida, al diario o al estado"
    assert SECRET not in repr(r) and SECRET not in str(r.__dict__)


def test_credential_files_are_validated_before_anything_runs(tmp_path):
    loose = _pw_file(tmp_path / "loose.pw", mode=0o644)
    with pytest.raises(ext.ExternalError, match="permisos") as e:
        ext.Restic(str(tmp_path / "repo"), password_file=loose, forbidden_roots=())
    assert e.value.code == "config"
    empty = _pw_file(tmp_path / "empty.pw", "")
    with pytest.raises(ext.ExternalError, match="vacío"):
        ext.Restic(str(tmp_path / "repo"), password_file=empty, forbidden_roots=())
    checkout = tmp_path / "checkout"; checkout.mkdir()
    inside = _pw_file(checkout / "restic.pw")
    with pytest.raises(ext.ExternalError, match="repositorio"):                                               # una contraseña dentro del checkout está a un `git add -A` de publicarse
        ext.Restic(str(tmp_path / "repo"), password_file=inside, forbidden_roots=(checkout,))
    with pytest.raises(ext.ExternalError, match="exactamente uno"):
        ext.Restic(str(tmp_path / "repo"), password_file=_pw_file(tmp_path / "ok.pw"), password_command="security find-generic-password -w", forbidden_roots=())
    with pytest.raises(ext.ExternalError, match="exactamente uno"):
        ext.Restic(str(tmp_path / "repo"), forbidden_roots=())


def test_the_env_file_is_private_and_only_knows_whitelisted_keys(tmp_path):
    pw = _pw_file(tmp_path / "pw")
    base = f"AICOS_BACKUP_RESTIC_REPOSITORY={tmp_path / 'repo'}\nAICOS_BACKUP_RESTIC_PASSWORD_FILE={pw}\n"
    loose = _pw_file(tmp_path / "loose.env", base, mode=0o644)
    with pytest.raises(ext.ExternalError, match="permisos"):
        ext.Restic.from_environment({}, env_file=loose, forbidden_roots=())
    weird = _pw_file(tmp_path / "weird.env", base + "LD_PRELOAD=/tmp/x.so\nVALOR_QUE_NO_SE_IMPRIME=abc123\n")
    with pytest.raises(ext.ExternalError) as e:
        ext.Restic.from_environment({}, env_file=weird, forbidden_roots=())
    assert "LD_PRELOAD" in str(e.value) and "/tmp/x.so" not in str(e.value) and e.value.code == "config"      # nombra la clave, nunca el valor
    ok = _pw_file(tmp_path / "ok.env", base + "# comentario\n\nB2_ACCOUNT_ID=abc\n")
    assert ext.Restic.from_environment({}, env_file=ok, forbidden_roots=()).repository == str(tmp_path / "repo")
    with pytest.raises(ext.ExternalError, match="AICOS_BACKUP_RESTIC_REPOSITORY"):
        ext.Restic.from_environment({}, env_file=None, forbidden_roots=())


def test_retention_forgets_old_snapshots_but_never_the_newest(live, tmp_path):
    r = ext.Restic(str(tmp_path / "repo"), password_file=_pw_file(tmp_path / "pw"), cache_dir=tmp_path / "cache", keep_daily=1, keep_weekly=0, keep_monthly=0, forbidden_roots=())
    r.init()
    ids = []
    for _ in range(4):
        write_rows(live)
        files, _m = _backup(live, tmp_path / "bk")
        ids.append(r.push(files)["snapshot"])
    assert len(r.snapshots()) == 4
    res = r.forget()
    left = r.snapshots()
    assert res["removed"] == 3 and len(left) == 1 and left[0]["id"].startswith(ids[-1][:8])                    # hoy solo queda el más nuevo, jamás uno viejo en su lugar
    got = r.fetch(ids[-1], tmp_path / "back")
    assert backup.verify(next(p for p in got if p.name.endswith(".sqlite3")))["ok"]


def _repo_bytes(path) -> int:
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


def test_pushing_identical_content_again_adds_almost_nothing(live, repo, tmp_path):
    """Deduplication that does not depend on where restic happens to cut the file: the SAME backup twice ⇒ every chunk already exists, only a snapshot and an index entry may grow."""
    bloat(live, 12)
    files, _ = _backup(live, tmp_path / "bk")
    repo.push(files)
    first = _repo_bytes(tmp_path / "repo")
    repo.push(files)
    total = _repo_bytes(tmp_path / "repo")
    assert first > 8_000_000 and (total - first) < first * 0.01, (first, total)


def test_a_small_change_on_a_large_database_stays_cheap(live, repo, tmp_path):
    """restic cuts files at positions that depend on a polynomial restic draws AT RANDOM FOR EACH REPOSITORY, and it re-uploads every chunk a change touches (≈1 MiB on average, up to
    8 MiB). With a 12 MB file and 20 new rows the growth ranged from 0.5 % to 30.6 % over 30 repositories on restic 0.19.1 — and one CI run (restic 0.18.1) measured 27.4 %: a 25 % bound
    at that size tests the polynomial's luck, not deduplication (qa/restic_dedup_study.py; docs/v06/BACKUP_RUTINA_Y_DESTINO_EXTERNO.md §3). On a 64 MB file the same change grew
    0.1 %–4.1 % over 30 repositories, so the SAME 25 % bound has a six-fold margin: a bound that can only be crossed if deduplication is really broken."""
    bloat(live, 64)
    f1, _ = _backup(live, tmp_path / "bk")
    repo.push(f1)
    first = _repo_bytes(tmp_path / "repo")
    write_rows(live, 20)
    f2, _ = _backup(live, tmp_path / "bk")
    repo.push(f2)
    total = _repo_bytes(tmp_path / "repo")
    assert first > 60_000_000 and (total - first) < first * 0.25, (first, total, (total - first) / first)


def _growth_with_the_worst_polynomial(tmp_path, megabytes: int) -> tuple[int, int]:
    """(bytes after the first backup, bytes after the second) for SEEDED data in a repository created with the chunker polynomial `2a1ae808395e51` — the worst of a scan of 40, copied with
    `restic init --copy-chunker-params`. Same polynomial + same data ⇒ the same bytes on any machine and any restic version (qa/restic_dedup_study.py compares them)."""
    import base64
    import json
    import random
    import sqlite3
    import subprocess

    work = tmp_path / f"w{megabytes}"; work.mkdir()
    template = json.loads((Path(__file__).parent / "restic_worst_polynomial.json").read_text())
    tpl, study_pw = work / "tpl", _pw_file(work / "study.pw", "estudio-dedup-contraseña-de-ensayo-0123456789")                        # the template's own (test-only) password
    for rel, b in template.items():
        f = tpl / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(base64.b64decode(b))
    for sub in ("data", "index", "locks", "snapshots"):
        (tpl / sub).mkdir(exist_ok=True)
    pw, repo, env = _pw_file(work / "pw"), work / "repo", {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    base = [RESTIC, "-r", str(repo), "--password-file", str(pw), "--cache-dir", str(work / "cache")]
    init = subprocess.run(base + ["init", "--copy-chunker-params", "--from-repo", str(tpl), "--from-password-file", str(study_pw)], capture_output=True, text=True, env=env)
    assert init.returncode == 0, init.stderr
    cfg = subprocess.run(base + ["cat", "config"], capture_output=True, text=True, env=env)
    assert json.loads(cfg.stdout)["chunker_polynomial"] == "2a1ae808395e51"                                                           # the repository really uses the worst polynomial
    r = ext.Restic(str(repo), password_file=pw, cache_dir=work / "cache", forbidden_roots=())

    rng = random.Random(20261123)
    db = work / "seeded.sqlite3"
    con = sqlite3.connect(db); con.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY, blob BLOB)"); con.execute("CREATE TABLE audit(id INTEGER PRIMARY KEY, detail TEXT)")
    for _ in range(megabytes):
        con.execute("INSERT INTO filler(blob) VALUES (?)", (rng.randbytes(1 << 20),))
    for i in range(200):
        con.execute("INSERT INTO audit(detail) VALUES (?)", (f"fila-{i}",))
    con.commit()

    def snap(name):
        src, dst = sqlite3.connect(db), sqlite3.connect(work / name)
        src.backup(dst); dst.execute("PRAGMA journal_mode=DELETE"); dst.commit(); dst.close(); src.close()
        return work / name

    r.push([snap("s1.sqlite3")])
    first = _repo_bytes(repo)
    for i in range(20):
        con.execute("INSERT INTO audit(detail) VALUES (?)", (f"nueva-{i}",))
    con.commit(); con.close()
    r.push([snap("s2.sqlite3")])
    return first, _repo_bytes(repo)


def test_the_incident_reproduces_with_the_unluckiest_polynomial_and_the_new_design_passes_under_it(tmp_path):
    """The CI failure of PR #16, made deterministic. Leg 1 — the OLD design (12 MB, 20 new rows, bound 25 %) with the worst polynomial we found: the same change re-uploads ≈ 4.2 MB
    of chunks, +33.3 %, on restic 0.19.1 (Mac, measured) — the runner's 0.18.1 gave 27.4 % with a random polynomial: it is the polynomial and the chunk size, not the version.
    Leg 2 — the NEW design (64 MB, same polynomial, same data, same 25 % bound): the growth in bytes is the same (the change touches the same chunks), the ratio is +6.3 %.
    Leg 1 is a CHARACTERISATION: if a future restic cuts differently and it stops reproducing, drop it — leg 2 is the regression test."""
    first12, total12 = _growth_with_the_worst_polynomial(tmp_path, 12)
    assert (total12 - first12) > first12 * 0.25, (first12, total12, (total12 - first12) / first12)                                  # the old bound FAILS here (the incident)
    first64, total64 = _growth_with_the_worst_polynomial(tmp_path, 64)
    assert first64 > 60_000_000 and (total64 - first64) < first64 * 0.25, (first64, total64, (total64 - first64) / first64)
    assert abs((total64 - first64) - (total12 - first12)) < 500_000                                                                    # same polynomial, same change ⇒ ≈ the same bytes: growth is chunks touched


def test_a_read_data_check_detects_a_damaged_pack(live, repo, tmp_path):
    files, _ = _backup(live, tmp_path / "bk")
    repo.push(files)
    assert repo.check(read_data=True)["ok"] is True
    packs = sorted(p for p in (tmp_path / "repo" / "data").rglob("*") if p.is_file())
    os.chmod(packs[0], 0o600)                                                                                  # restic crea los paquetes de solo lectura
    raw = bytearray(packs[0].read_bytes()); raw[len(raw) // 2] ^= 0xFF; packs[0].write_bytes(bytes(raw))
    with pytest.raises(ext.ExternalError):
        repo.check(read_data=True)


def test_rotating_the_password_invalidates_the_old_one_and_keeps_every_snapshot(live, repo, tmp_path):
    files, _ = _backup(live, tmp_path / "bk")
    snap = repo.push(files)["snapshot"]
    new = _pw_file(tmp_path / "new.pw", "nueva-contraseña-rotada-0987654321-de-prueba")
    repo.rotate_password(new)
    with pytest.raises(ext.ExternalError) as e:
        repo.snapshots()                                                                                       # el archivo viejo ya no abre el repositorio
    assert e.value.code == "wrong-password"
    rotated = ext.Restic(str(tmp_path / "repo"), password_file=new, cache_dir=tmp_path / "cache", forbidden_roots=())
    got = rotated.fetch(snap, tmp_path / "back")
    assert backup.verify(next(p for p in got if p.name.endswith(".sqlite3")))["ok"]


def test_the_routine_end_to_end_with_a_real_encrypted_repository(live, repo, tmp_path):
    out = tmp_path / "bk"
    r1 = routine.run(live, out, external=repo)
    assert r1["status"] == "ok" and r1["external"]["ok"] is True and r1["external"]["restore_drill"]["ok"] is True
    write_rows(live)
    r2 = routine.run(live, out, external=repo)
    assert r2["status"] == "ok" and r2["external"]["ok"] is True and r2["external"]["restore_drill"] is None       # la restauración externa cuesta descarga: no se repite cada día
    s = routine.status(out, external_required=True)
    assert s["level"] == "ok" and s["external"]["configured"] is True and s["external"]["last_ok_age_hours"] < 0.1
    assert len(repo.snapshots()) == 2
    assert b"dueno@climasur.test" not in _all_bytes(tmp_path / "repo")


def test_scrub_external_with_a_valid_repository_really_checks_it_and_says_so(live, repo, tmp_path):
    out = tmp_path / "bk"
    routine.run(live, out, external=repo)
    res = routine.scrub(out, external=repo, external_requested=True)
    assert res["ok"] is True and res["external"]["requested"] is True and res["external"]["performed"] is True and res["external"]["ok"] is True
    envfile = _pw_file(tmp_path / "restic.env", f"AICOS_BACKUP_RESTIC_REPOSITORY={tmp_path / 'repo'}\nAICOS_BACKUP_RESTIC_PASSWORD_FILE={tmp_path / 'pw'}\n")
    from .bkutil import cli
    p = cli("scrub", "--out", out, "--external", "--env-file", envfile)
    assert p.returncode == 0 and p.stdout.startswith("OK") and "repositorio externo verificado" in p.stdout, (p.stdout, p.stderr)
    last = routine.read_journal(out)[-1]
    assert last["status"] == "ok" and last["external"]["performed"] is True and last["external"]["ok"] is True


def test_scrub_external_that_finds_a_damaged_repository_fails_but_did_perform_the_check(live, repo, tmp_path):
    out = tmp_path / "bk"
    routine.run(live, out, external=repo)
    packs = sorted(p for p in (tmp_path / "repo" / "data").rglob("*") if p.is_file())
    os.chmod(packs[0], 0o600)
    raw = bytearray(packs[0].read_bytes()); raw[len(raw) // 2] ^= 0xFF; packs[0].write_bytes(bytes(raw))
    res = routine.scrub(out, external=repo, external_requested=True, external_read_data=True)
    assert res["ok"] is False and res["external"]["performed"] is True and res["external"]["ok"] is False and res["external"]["code"] == "damaged"
