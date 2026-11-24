"""Issue #15 — dos peligros de la herramienta `app.backup` de V0.6 que NO dependen del módulo nuevo (fallan contra `main` 8d0c020):

 * la retención por conteo podía borrar la última copia que SÍ verifica (si las N más nuevas estaban dañadas);
 * un destino sin permiso de escritura terminaba en un *traceback*, no en un fallo limpio con código de salida 2.
"""
import json
import os
from datetime import datetime, timezone

import pytest

from app import backup

from .bkutil import flip_byte, install_clock, make_live, write_rows


@pytest.fixture
def live(tmp_path):
    return make_live(tmp_path)


def test_prune_never_deletes_the_last_good_backup(live, tmp_path):
    """3 copias; las 2 más nuevas se dañan (disco con sectores malos, ransomware, un editor): `--keep 2` conservaba las dos rotas y BORRABA la única que sirve."""
    out = tmp_path / "bk"
    made = []
    for _ in range(3):
        write_rows(live)
        made.append(backup.create(live, out))
    for m in made[1:]:
        flip_byte(out / m["file"])
    gone = backup.prune(out, keep=2)
    assert (out / made[0]["file"]).exists(), "la única copia verificable fue borrada"
    assert backup.verify(out / made[0]["file"])["ok"] is True
    assert gone == []                                                                      # las dos rotas están dentro de `keep`, la buena está protegida: no sobra nada


def test_prune_refuses_to_delete_anything_when_no_backup_verifies(live, tmp_path):
    out = tmp_path / "bk"
    made = []
    for _ in range(3):
        write_rows(live)
        made.append(backup.create(live, out))
    for m in made:
        flip_byte(out / m["file"])
    with pytest.raises(backup.BackupError, match="ninguna copia"):
        backup.prune(out, keep=1)
    assert all((out / m["file"]).exists() and (out / (m["file"] + ".manifest.json")).exists() for m in made)       # sin una copia buena, borrar es perder evidencia


def test_the_newest_good_backup_is_found_even_when_newer_ones_are_corrupt(live, tmp_path):
    out = tmp_path / "bk"
    made = []
    for _ in range(3):
        write_rows(live)
        made.append(backup.create(live, out))
    flip_byte(out / made[2]["file"])
    assert backup.newest_verified(out)["file"] == made[1]["file"]
    flip_byte(out / made[1]["file"]); flip_byte(out / made[0]["file"])
    assert backup.newest_verified(out) is None


def test_prune_keeps_the_newest_backup_of_each_recent_day_besides_the_last_n(live, tmp_path, monkeypatch):
    clk = install_clock(monkeypatch, datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc))
    out = tmp_path / "bk"
    by_day = {}
    for day in range(6):
        for k in range(2):                                                                  # 06:00 y 18:00 UTC de cada día
            write_rows(live)
            m = backup.create(live, out)
            by_day.setdefault(day, []).append(m["file"])
            clk.advance(hours=12)
    gone = backup.prune(out, keep=2, keep_daily=3)
    left = {b["file"] for b in backup.list_backups(out)}
    expected = {by_day[5][1], by_day[5][0], by_day[4][1], by_day[3][1]}                      # las 2 últimas + la más nueva de los 3 últimos días
    assert left == expected and len(gone) == 8, (sorted(left), gone)


def test_an_unwritable_destination_is_a_clean_failure_not_a_traceback(live, tmp_path, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignora los permisos")
    out = tmp_path / "ro"; out.mkdir(); os.chmod(out, 0o500)
    try:
        assert backup.main(["create", "--db", str(live), "--out", str(out)]) == 2
    finally:
        os.chmod(out, 0o700)
    assert "FALLO" in capsys.readouterr().err and list(out.iterdir()) == []


def test_a_full_disk_while_publishing_is_a_clean_failure_and_leaves_no_listed_backup(live, tmp_path, monkeypatch, capsys):
    import errno
    out = tmp_path / "bk"
    real = backup._write_private

    def full(path, text):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup, "_write_private", full)
    assert backup.main(["create", "--db", str(live), "--out", str(out)]) == 2
    assert "FALLO" in capsys.readouterr().err
    assert backup.list_backups(out) == [] and [p.name for p in out.iterdir() if p.name.startswith(".aicos-work")] == []
    monkeypatch.setattr(backup, "_write_private", real)


def test_orphan_work_directories_and_unmanifested_files_are_cleaned_only_when_old(live, tmp_path):
    out = tmp_path / "bk"
    m = backup.create(live, out)
    stale_dir = out / ".aicos-work-stale"; stale_dir.mkdir(mode=0o700); (stale_dir / "snapshot.sqlite3").write_bytes(b"x" * 100)
    young_dir = out / ".aicos-work-young"; young_dir.mkdir(mode=0o700)
    stale_data = out / "aicos-20260101T000000000000Z-deadbeef.sqlite3"; stale_data.write_bytes(b"sin manifiesto")
    young_data = out / "aicos-20260101T000001000000Z-cafebabe.sqlite3"; young_data.write_bytes(b"sin manifiesto, recien creado")
    other = out / "mis-notas.txt"; other.write_text("no es mío")
    now = 2_000_000_000
    for p in (stale_dir, stale_data):
        os.utime(p, (now - 7200, now - 7200))
    for p in (young_dir, young_data, other):
        os.utime(p, (now - 30, now - 30))
    os.utime(other, (now - 99999, now - 99999))                                              # antiguo pero NO es nuestro: jamás se toca
    removed = backup.cleanup_orphans(out, older_than_s=3600, now=now)
    assert sorted(removed) == sorted([stale_dir.name, stale_data.name])
    assert young_dir.exists() and young_data.exists() and other.exists()                     # lo reciente podría ser un `create` vivo de otra terminal
    assert (out / m["file"]).exists() and backup.verify(out / m["file"])["ok"]
