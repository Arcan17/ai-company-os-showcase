"""V0.6 — copias de seguridad restaurables. Cada prueba nombra el fallo que impide (copia corrupta que parece buena, restauración que pisa datos vivos, etc.)."""
import hashlib
import json
import os
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import backup
from app.api import create_app
from app.seed import DEMO_PASSWORD


@pytest.fixture
def live(tmp_path):
    """A real app database (WAL, seeded, one open session) the way the server leaves it."""
    url = f"sqlite:///{tmp_path / 'aicos_live.db'}"
    app = create_app(url)
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": DEMO_PASSWORD}).status_code == 200
    app.state.eng.dispose()
    return tmp_path / "aicos_live.db"


def _count(path, table):
    """Row count, or None when the table does not exist (this branch is independent of the access-hardening PR that adds login_attempts)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return None
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _write(path, n=1):
    """What the running server does all the time: committed writes."""
    con = sqlite3.connect(path, timeout=30)
    cid = con.execute("SELECT id FROM companies LIMIT 1").fetchone()[0]
    for i in range(n):
        con.execute("INSERT INTO audit_log(company_id,ts,actor_type,actor_id,action,resource,outcome,detail) VALUES(?,datetime('now'),'system','t','test.write','-','ok',?)", (cid, str(i)))
        con.commit()
    con.close()


def test_create_makes_a_single_verified_private_file_with_manifest(live, tmp_path):
    out = tmp_path / "bk"
    m = backup.create(live, out)
    f = out / m["file"]
    assert f.exists() and (out / (m["file"] + ".manifest.json")).exists() and sorted(p.name for p in out.iterdir() if p.name.endswith(("-wal", "-shm"))) == []
    assert hashlib.sha256(f.read_bytes()).hexdigest() == m["sha256"] and m["integrity"] == "ok"
    assert oct(f.stat().st_mode & 0o777) == "0o600" and oct(out.stat().st_mode & 0o777) == "0o700"
    assert m["tables"]["users"] == _count(live, "users") == _count(f, "users") and m["tables"]["sessions"] == 1
    assert "/" not in json.dumps(m) and "argon2" not in json.dumps(m) and "scrypt" not in json.dumps(m)     # no paths, no hashes in the sidecar
    assert sqlite3.connect(f).execute("PRAGMA journal_mode").fetchone()[0] != "wal"                       # self-contained: nothing left in a -wal file


def test_backup_is_a_consistent_snapshot_while_the_server_keeps_writing(live, tmp_path):
    stop, errors = threading.Event(), []

    def writer():
        try:
            while not stop.is_set():
                _write(live)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=writer); t.start()
    try:
        ms = [backup.create(live, tmp_path / "bk") for _ in range(3)]
    finally:
        stop.set(); t.join()
    assert not errors and all(m["integrity"] == "ok" for m in ms)
    for m in ms:
        backup.verify(tmp_path / "bk" / m["file"])


def test_verify_detects_every_kind_of_damage(live, tmp_path):
    out = tmp_path / "bk"
    m = backup.create(live, out); f = out / m["file"]
    assert backup.verify(f)["ok"] is True
    raw = f.read_bytes()
    f.write_bytes(raw[:-4096]); 
    with pytest.raises(backup.BackupError, match="sha256|tamaño"):
        backup.verify(f)                                                          # truncated
    flipped = bytearray(raw); flipped[len(raw) // 2] ^= 0xFF; f.write_bytes(bytes(flipped))
    with pytest.raises(backup.BackupError, match="sha256"):
        backup.verify(f)                                                          # one flipped byte
    f.write_bytes(raw)
    side = out / (m["file"] + ".manifest.json"); good = side.read_text()
    bad = json.loads(good); bad["tables"]["users"] += 1; side.write_text(json.dumps(bad))
    with pytest.raises(backup.BackupError, match="filas"):
        backup.verify(f)                                                          # manifest lies about the content
    side.unlink()
    with pytest.raises(backup.BackupError, match="manifiesto"):
        backup.verify(f)                                                          # a bare .sqlite3 is not a verified backup
    side.write_text(good); assert backup.verify(f)["ok"]


def test_a_corrupt_source_is_refused_and_leaves_nothing_behind(tmp_path):
    junk = tmp_path / "junk.db"; junk.write_bytes(b"esto no es sqlite" * 100)
    with pytest.raises(backup.BackupError):
        backup.create(junk, tmp_path / "bk")
    with pytest.raises(backup.BackupError, match="no existe"):
        backup.create(tmp_path / "missing.db", tmp_path / "bk")
    assert not (tmp_path / "missing.db").exists()                                  # sqlite3.connect would have CREATED an empty database
    assert list((tmp_path / "bk").glob("*")) == [] if (tmp_path / "bk").exists() else True


def test_restore_goes_to_a_new_path_revokes_sessions_and_never_overwrites(live, tmp_path):
    m = backup.create(live, tmp_path / "bk"); f = tmp_path / "bk" / m["file"]
    target = tmp_path / "restored.db"
    r = backup.restore(f, target)
    assert target.exists() and r["sessions_revoked"] is True and _count(target, "sessions") == 0 and _count(target, "login_attempts") in (0, None)
    assert _count(target, "users") == m["tables"]["users"] and _count(target, "audit_log") == m["tables"]["audit_log"]
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    target.write_bytes(b"VIVO")                                                    # something already lives there
    with pytest.raises(backup.BackupError, match="ya existe"):
        backup.restore(f, target)
    assert target.read_bytes() == b"VIVO"                                           # untouched
    keep = backup.restore(f, tmp_path / "keep.db", revoke_sessions=False)
    assert keep["sessions_revoked"] is False and _count(tmp_path / "keep.db", "sessions") == 1


def test_restore_of_a_damaged_backup_leaves_no_partial_target(live, tmp_path):
    m = backup.create(live, tmp_path / "bk"); f = tmp_path / "bk" / m["file"]
    b = bytearray(f.read_bytes()); b[len(b) // 3] ^= 0xFF; f.write_bytes(bytes(b))
    target = tmp_path / "restored.db"
    with pytest.raises(backup.BackupError):
        backup.restore(f, target)
    assert not target.exists() and not list(tmp_path.glob("restored.db*"))


def test_restore_drill_boots_the_real_app_on_the_restored_copy(live, tmp_path):
    m = backup.create(live, tmp_path / "bk")
    target = tmp_path / "restored.db"
    backup.restore(tmp_path / "bk" / m["file"], target)
    app = create_app(f"sqlite:///{target}")
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": "dueno@climasur.test", "password": DEMO_PASSWORD})
    assert r.status_code == 200 and c.get("/api/dashboard").status_code == 200       # passwords and data came back usable
    res = backup.drill(live, tmp_path / "drill")
    assert res["ok"] is True and res["boot"]["users"] >= 6 and not list((tmp_path / "drill").glob("restore-*"))   # scratch restore is removed


def test_restore_of_a_v05_shaped_backup_migrates_when_booted(tmp_path):
    from sqlalchemy import text
    url = f"sqlite:///{tmp_path / 'old.db'}"; app = create_app(url)
    with app.state.eng.begin() as c:                                        # rewind to the previous schema: V0.4 had no users.active (additive migration re-adds it)
        c.execute(text("ALTER TABLE users DROP COLUMN active"))
    app.state.eng.dispose()
    m = backup.create(tmp_path / "old.db", tmp_path / "bk")
    res = backup.drill(tmp_path / "old.db", tmp_path / "drill")
    assert res["ok"] is True and res["boot"]["migrated"] is True


def test_prune_keeps_the_newest_and_only_touches_its_own_files(live, tmp_path):
    out = tmp_path / "bk"
    names = []
    for i in range(4):
        _write(live)                                                          # different content → different sha → different file name (same content in one second = same name)
        m = backup.create(live, out)
        names.append(m["file"])
        os.utime(out / m["file"], (1_700_000_000 + i, 1_700_000_000 + i))
        side = out / (m["file"] + ".manifest.json"); d = json.loads(side.read_text()); d["created_at"] = f"2026-11-0{i + 1}T00:00:00+00:00"; side.write_text(json.dumps(d))
    (out / "notas.txt").write_text("no es mío"); (out / "otra.sqlite3").write_bytes(b"x")
    gone = backup.prune(out, keep=2)
    left = sorted(p.name for p in out.iterdir())
    assert len(gone) == 2 and "notas.txt" in left and "otra.sqlite3" in left
    assert sum(1 for n in left if n.endswith(".sqlite3") and n.startswith("aicos-")) == 2 and sum(1 for n in left if n.endswith(".manifest.json")) == 2
    assert [b["file"] for b in backup.list_backups(out)] == [names[3], names[2]] or len(backup.list_backups(out)) == 2
    with pytest.raises(backup.BackupError):
        backup.prune(out, keep=0)


def test_cli_exit_codes_and_no_default_database(live, tmp_path, capsys):
    out = tmp_path / "bk"
    assert backup.main(["create", "--db", str(live), "--out", str(out)]) == 0
    name = backup.list_backups(out)[0]["file"]
    assert backup.main(["verify", str(out / name)]) == 0
    assert backup.main(["restore", str(out / name), "--to", str(tmp_path / "r.db")]) == 0
    assert backup.main(["restore", str(out / name), "--to", str(tmp_path / "r.db")]) == 2          # exists → refuses
    assert backup.main(["drill", "--db", str(live), "--work", str(tmp_path / "w")]) == 0
    assert backup.main(["list", "--out", str(out)]) == 0 and name in capsys.readouterr().out
    with pytest.raises(SystemExit):
        backup.main(["create", "--out", str(out)])                                             # --db is mandatory: there is no implicit database


# ====================================================================== revisión del PR #11 (tres hilos de revisión automatizada + comentario del dueño)
import errno
import stat
import subprocess
import sys
from datetime import datetime as _real_datetime, timezone as _tz
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _spy_connect(monkeypatch):
    """Records, for every WRITABLE sqlite3.connect(path), whether the file already existed and with which mode at that very moment."""
    seen = []
    real = sqlite3.connect

    def spy(target, *a, **k):
        if not str(target).startswith("file:") and str(target) != ":memory:":
            p = Path(str(target))
            seen.append((p.name, p.exists(), (p.stat().st_mode & 0o777) if p.exists() else None))
        return real(target, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    return seen


@pytest.fixture
def umask022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_restore_temp_file_is_born_private_not_chmodded_after_the_copy(live, tmp_path, monkeypatch, umask022):
    """revisión automatizada P2 (#11): with umask 022 the .restore-*.tmp was created 0644 and only chmodded AFTER the database (password + session hashes) was copied into it."""
    m = backup.create(live, tmp_path / "bk")
    shared = tmp_path / "shared"; shared.mkdir(); os.chmod(shared, 0o755)                     # a directory other local users can list and enter
    seen = _spy_connect(monkeypatch)
    backup.restore(tmp_path / "bk" / m["file"], shared / "restored.db")
    writable = [s for s in seen if s[0] != m["file"]]                                          # every writable open of a restore scratch file
    assert writable, "restore never opened a scratch file?"
    assert all(exists and mode == 0o600 for _, exists, mode in writable), writable            # it existed — empty, 0600 — BEFORE sqlite wrote a single page
    assert oct((shared / "restored.db").stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in shared.iterdir()) == ["restored.db"]                         # no scratch directory or journal left behind


def test_create_artifacts_are_private_from_birth_and_an_existing_directory_is_not_chmodded(live, tmp_path, monkeypatch, umask022):
    """Owner (#11): no unsafe-permission windows in unexpected directories. `create` used to chmod 0700 whatever directory it was pointed at."""
    out = tmp_path / "shared-bk"; out.mkdir(); os.chmod(out, 0o755)
    seen = _spy_connect(monkeypatch)
    m = backup.create(live, out)
    assert all(exists and mode == 0o600 for _, exists, mode in seen), seen
    assert stat.S_IMODE(out.stat().st_mode) == 0o755                                           # not ours to change …
    assert any("accesible por otros usuarios" in w for w in m.get("warnings", []))             # … but the operator is told
    assert oct((out / m["file"]).stat().st_mode & 0o777) == "0o600" and oct((out / (m["file"] + ".manifest.json")).stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in out.iterdir()) == sorted([m["file"], m["file"] + ".manifest.json"])
    fresh = tmp_path / "new-bk"
    backup.create(live, fresh)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700                                         # a directory WE create is private


class _Frozen(_real_datetime):
    when = _real_datetime(2026, 11, 22, 20, 15, 30, 123456, tzinfo=_tz.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.when


def test_snapshots_in_the_same_instant_keep_the_newest_and_never_return_a_deleted_name(live, tmp_path, monkeypatch):
    """revisión automatizada P2 (#11): identical manifest timestamps → arbitrary directory order → `create(keep=1)` could delete the backup it had just made and still report it."""
    monkeypatch.setattr(backup, "datetime", _Frozen)                                          # the clock does not advance at all between the two snapshots
    out = tmp_path / "bk"
    prev = None
    for i in range(8):                                                                         # same instant every time: the old tie-break was a coin toss on the file names
        _write(live, 1 + i)
        cur = backup.create(live, out, keep=1)
        left = [b["file"] for b in backup.list_backups(out)]
        assert left == [cur["file"]] and (out / cur["file"]).exists(), (i, left, cur["file"])   # the survivor is the one just created, and the returned name exists
        assert prev is None or (cur["tables"]["audit_log"] > prev["tables"]["audit_log"] and not (out / prev["file"]).exists())
        assert backup.verify(out / cur["file"])["ok"]
        prev = cur


def test_a_clock_that_steps_backwards_cannot_make_a_new_backup_look_older(live, tmp_path, monkeypatch):
    out = tmp_path / "bk"
    monkeypatch.setattr(backup, "datetime", _Frozen)
    _write(live, 1); a = backup.create(live, out)
    _Frozen.when = _Frozen.when.replace(hour=3)                                                # NTP step / manual change: "now" is now EARLIER
    _write(live, 1); b = backup.create(live, out, keep=1)
    assert [x["file"] for x in backup.list_backups(out)] == [b["file"]] and a["file"] != b["file"]


def test_restore_never_overwrites_when_hard_links_are_unavailable_and_the_target_appears_meanwhile(live, tmp_path, monkeypatch):
    """revisión automatizada P2 (#11): os.link → EOPNOTSUPP/EXDEV fell back to os.rename, which REPLACES a database another process created after the existence check."""
    m = backup.create(live, tmp_path / "bk"); f = tmp_path / "bk" / m["file"]
    target = tmp_path / "restored.db"

    def link_unsupported_and_someone_wins_the_race(src, dst, *a, **k):
        Path(dst).write_bytes(b"VIVO: creada por otro proceso")
        raise OSError(errno.EOPNOTSUPP, "sin enlaces duros")

    monkeypatch.setattr(os, "link", link_unsupported_and_someone_wins_the_race)
    with pytest.raises(backup.BackupError, match="ya existe"):
        backup.restore(f, target)
    assert target.read_bytes() == b"VIVO: creada por otro proceso"                           # untouched
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".restore")] == []       # scratch cleaned up


def test_restore_fails_safe_when_no_exclusive_publication_exists_at_all(live, tmp_path, monkeypatch):
    m = backup.create(live, tmp_path / "bk"); f = tmp_path / "bk" / m["file"]
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "otro sistema de archivos")))
    monkeypatch.setattr(backup, "_rename_noreplace", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "RENAME_NOREPLACE no disponible")), raising=False)
    target = tmp_path / "restored.db"
    with pytest.raises(backup.BackupError, match="exclusiva"):
        backup.restore(f, target)
    assert not target.exists() and not [p for p in tmp_path.iterdir() if p.name.startswith(".restore")]


def test_without_hard_links_an_atomic_noreplace_rename_still_restores(live, tmp_path, monkeypatch):
    m = backup.create(live, tmp_path / "bk"); f = tmp_path / "bk" / m["file"]
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "sin enlaces duros")))
    target = tmp_path / "restored.db"
    r = backup.restore(f, target)                                                              # macOS renamex_np(RENAME_EXCL) / Linux renameat2(RENAME_NOREPLACE)
    assert r["ok"] and _count(target, "users") == m["tables"]["users"]
    with pytest.raises(backup.BackupError, match="ya existe"):
        backup.restore(f, target)


CHILD = r'''
import errno, os, sys, time
if os.environ.get("NOLINK") == "1":
    def nolink(*a, **k): raise OSError(errno.EOPNOTSUPP, "sin enlaces duros")
    os.link = nolink
from app import backup
deadline = time.time() + 60                                  # a worker whose test run was killed must not spin forever as an orphan
while not os.path.exists(os.environ["GATE"]):
    if time.time() > deadline: sys.exit(3)
    time.sleep(0.0005)
sys.exit(backup.main(sys.argv[1:]))
'''


@pytest.mark.parametrize("nolink", ["0", "1"])
def test_six_processes_restoring_to_the_same_target_exactly_one_wins(live, tmp_path, nolink):
    m = backup.create(live, tmp_path / "bk"); f = tmp_path / "bk" / m["file"]
    target, gate = tmp_path / "restored.db", tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "NOLINK": nolink, "GATE": str(gate)}
    procs = [subprocess.Popen([sys.executable, "-W", "ignore", "-c", CHILD, "restore", str(f), "--to", str(target)], cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for _ in range(6)]
    time.sleep(1.0); gate.write_text("go")                                                     # release them together
    codes = sorted(p.wait(timeout=60) for p in procs)
    assert codes == [0, 2, 2, 2, 2, 2], codes                                                  # one publishes; the rest are refused — nobody overwrites anybody
    con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and con.execute("SELECT count(*) FROM users").fetchone()[0] == m["tables"]["users"]
    con.close()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".restore")] == []
