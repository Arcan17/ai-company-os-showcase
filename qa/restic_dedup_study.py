#!/usr/bin/env python3
"""Estudio de deduplicación de restic (Issue #15 / PR #16): ¿el 27,4 % del CI es una diferencia de VERSIÓN (0.18.1 vs 0.19.1) o del DISEÑO de la prueba?
Herramienta de DIAGNÓSTICO (no es una prueba): se ejecuta a mano, desde backend/, con restic instalado. Resultados y conclusión: docs/v06/BACKUP_RUTINA_Y_DESTINO_EXTERNO.md §3.

  restic_dedup_study.py random N MB          N repeticiones del escenario EXACTO de la prueba que falló (app DB + MB de blobs aleatorios + 20 filas), repositorio nuevo cada vez
                                      (restic elige un polinomio de fragmentación ALEATORIO por repositorio). Imprime la distribución del crecimiento.
  restic_dedup_study.py det TEMPLATE_B64 MB  UNA repetición DETERMINISTA: datos con semilla fija + polinomio copiado de una plantilla → mismos bytes en cualquier máquina/versión.
  restic_dedup_study.py scan N MB ARCHIVO     N polinomios × los MISMOS datos con semilla: cuánto del crecimiento es suerte del polinomio; guarda la peor plantilla en ARCHIVO
  restic_dedup_study.py template             crea la plantilla (repositorio vacío con contraseña de ensayo) y la imprime en base64
  restic_dedup_study.py same MB              la MISMA copia dos veces: lo único que debe crecer es el snapshot (no depende de ningún fragmento)
Todo en directorios temporales; contraseña de ensayo, sin red, sin cuentas.
"""
import base64, json, os, random, shutil, sqlite3, statistics, subprocess, sys, tempfile
from pathlib import Path

PW = "estudio-dedup-contraseña-de-ensayo-0123456789"
ENV = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "LC_ALL": "C"}


def restic(repo, pwf, *a, **kw):
    r = subprocess.run(["restic", "-r", str(repo), "--password-file", str(pwf), "--cache-dir", str(Path(pwf).parent / "cache"), *a], capture_output=True, text=True, env=ENV, **kw)
    if r.returncode != 0:
        raise SystemExit(f"restic {a[0]} falló: {r.stderr.strip()[-300:]}")
    return r.stdout


def size(repo):
    return sum(p.stat().st_size for p in Path(repo).rglob("*") if p.is_file())


def version():
    return subprocess.run(["restic", "version"], capture_output=True, text=True).stdout.strip()


def pw_file(d):
    p = Path(d) / "pw"
    fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600); os.write(fd, PW.encode()); os.close(fd)
    return p


def snapshot(src, dst):
    s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(dst)
    s.backup(d); d.execute("PRAGMA journal_mode=DELETE"); d.commit(); d.close(); s.close()


def push(repo, pwf, f):
    out = restic(repo, pwf, "backup", "--json", "--tag", "aicos", "--host", "aicos", str(f))
    return next(json.loads(l) for l in out.splitlines() if '"summary"' in l)


def stats(repo, pwf):
    j = json.loads(restic(repo, pwf, "stats", "--mode", "raw-data", "--json"))
    return j["total_size"], j["total_blob_count"]


def random_mode(n, mb):
    sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "backend")]
    from tests.bkutil import make_live, bloat, write_rows
    from app import backup
    ratios, polys = [], []
    print(f"STUDY {version()} | escenario de la prueba: base de la app + {mb} MB aleatorios, 20 filas entre copias | {n} repeticiones")
    for i in range(n):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); pwf = pw_file(d); live = make_live(d); bloat(live, mb)
            repo = d / "repo"; restic(repo, pwf, "init")
            poly = json.loads(restic(repo, pwf, "cat", "config"))["chunker_polynomial"]
            m1 = backup.create(live, d / "bk"); push(repo, pwf, d / "bk" / m1["file"]); first = size(repo)
            write_rows(live, 20)
            m2 = backup.create(live, d / "bk"); push(repo, pwf, d / "bk" / m2["file"]); total = size(repo)
            ratios.append((total - first) / first); polys.append(poly)
            print(f"STUDY run={i:02d} poly={poly} primero={first} total={total} crecimiento={total - first} ({(total - first) / first:.1%})", flush=True)
    r = sorted(ratios)
    print(f"STUDY RESUMEN {version()} MB={mb} N={n}: min {r[0]:.1%} · mediana {statistics.median(r):.1%} · media {statistics.mean(r):.1%} · p90 {r[int(.9 * (n - 1))]:.1%} · máx {r[-1]:.1%} · "
          f"repeticiones > 25 %: {sum(x > .25 for x in r)}/{n}")


def template_mode():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d); pwf = pw_file(d); restic(d / "t", pwf, "init")
        files = {str(p.relative_to(d / "t")): base64.b64encode(p.read_bytes()).decode() for p in (d / "t").rglob("*") if p.is_file()}
        print(json.dumps(files))


def det_run(files, mb):
    """One deterministic repetition: seeded data + the chunker polynomial of the template → the same bytes on any machine and any restic version."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d); pwf = pw_file(d)
        for rel, b in files.items():
            p = d / "tpl" / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(base64.b64decode(b))
        for sub in ("data", "index", "locks", "snapshots"):
            (d / "tpl" / sub).mkdir(exist_ok=True)
        repo = d / "repo"
        restic(repo, pwf, "init", "--copy-chunker-params", "--from-repo", str(d / "tpl"), "--from-password-file", str(pwf))
        poly = json.loads(restic(repo, pwf, "cat", "config"))["chunker_polynomial"]
        rng = random.Random(20261123)
        db = d / "db.sqlite3"
        con = sqlite3.connect(db); con.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY, blob BLOB)"); con.execute("CREATE TABLE audit(id INTEGER PRIMARY KEY, detail TEXT)")
        for _ in range(mb): con.execute("INSERT INTO filler(blob) VALUES (?)", (rng.randbytes(1 << 20),))
        for i in range(200): con.execute("INSERT INTO audit(detail) VALUES (?)", (f"fila-{i}",))
        con.commit()
        snapshot(db, d / "s1.sqlite3"); push(repo, pwf, d / "s1.sqlite3"); first = size(repo); raw1 = stats(repo, pwf)
        for i in range(20): con.execute("INSERT INTO audit(detail) VALUES (?)", (f"nueva-{i}",))
        con.commit(); con.close()
        snapshot(db, d / "s2.sqlite3"); push(repo, pwf, d / "s2.sqlite3"); total = size(repo); raw2 = stats(repo, pwf)
    return {"poly": poly, "mb": mb, "first": first, "total": total, "growth": total - first, "ratio": (total - first) / first, "new_raw": raw2[0] - raw1[0], "new_blobs": raw2[1] - raw1[1],
            "raw1": raw1, "raw2": raw2}


def det_mode(tpl_b64, mb):
    r = det_run(json.loads(tpl_b64), mb)
    print(f"STUDY-DET {version()} poly={r['poly']} MB={mb} primero={r['first']} total={r['total']} crecimiento={r['growth']} ({r['ratio']:.2%}) "
          f"| datos crudos: {r['raw1'][0]}B/{r['raw1'][1]} bloques → {r['raw2'][0]}B/{r['raw2'][1]} bloques (nuevos: {r['new_raw']}B en {r['new_blobs']} bloques)")


def scan_mode(n, mb, outfile):
    """N polynomials (templates) × the SAME seeded data: how much of the growth is the polynomial's luck? Saves the worst template to `outfile`."""
    worst = None; rows = []
    for i in range(n):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); pwf = pw_file(d); restic(d / "t", pwf, "init")
            files = {str(p.relative_to(d / "t")): base64.b64encode(p.read_bytes()).decode() for p in (d / "t").rglob("*") if p.is_file()}
        r = det_run(files, mb); rows.append(r["ratio"])
        print(f"STUDY-SCAN {i:02d} poly={r['poly']} MB={mb} crecimiento={r['growth']} ({r['ratio']:.1%}) bloques nuevos={r['new_blobs']} ({r['new_raw']}B)", flush=True)
        if worst is None or r["ratio"] > worst[0]:
            worst = (r["ratio"], files, r["poly"])
    rs = sorted(rows)
    print(f"STUDY-SCAN RESUMEN {version()} MB={mb} N={n}: min {rs[0]:.1%} · mediana {statistics.median(rs):.1%} · máx {rs[-1]:.1%} · >25 %: {sum(x > .25 for x in rs)}/{n} · peor polinomio {worst[2]}")
    Path(outfile).write_text(json.dumps(worst[1]))


def same_mode(mb):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d); pwf = pw_file(d); repo = d / "repo"; restic(repo, pwf, "init")
        rng = random.Random(7)
        db = d / "db.sqlite3"; con = sqlite3.connect(db); con.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY, blob BLOB)")
        for _ in range(mb): con.execute("INSERT INTO filler(blob) VALUES (?)", (rng.randbytes(1 << 20),))
        con.commit(); con.close()
        snapshot(db, d / "s.sqlite3"); push(repo, pwf, d / "s.sqlite3"); first = size(repo)
        push(repo, pwf, d / "s.sqlite3"); total = size(repo)
        print(f"STUDY-SAME {version()} MB={mb}: primero={first} total={total} crecimiento={total - first}B ({(total - first) / first:.4%})")


if __name__ == "__main__":
    m = sys.argv[1]
    if m == "random": random_mode(int(sys.argv[2]), int(sys.argv[3]))
    elif m == "template": template_mode()
    elif m == "det": det_mode(sys.argv[2], int(sys.argv[3]))
    elif m == "scan": scan_mode(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    elif m == "same": same_mode(int(sys.argv[2]))
    else: raise SystemExit(__doc__)
