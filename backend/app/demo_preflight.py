"""Read-only preflight of the commercial-demo database (+ optional live server). Never writes. Exit code 0 = ready, 1 = something to fix.
  python -m app.demo_preflight --db backend/data/demo_comercial.sqlite3 [--url http://127.0.0.1:8743]"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request

from .demo_seed import DemoSafetyError, assert_demo_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--url")
    a = ap.parse_args(argv)
    path = assert_demo_path(a.db)
    rows: list[tuple[str, str, str]] = []
    def chk(name, ok, detail="", warn=False): rows.append(("PASS" if ok else ("WARN" if warn else "FAIL"), name, detail))
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip())
    except Exception:
        commit, dirty = "", False
    chk("Commit del código", bool(commit), f"{commit}{' (con cambios sin commitear)' if dirty else ''}", warn=True)
    if not os.path.exists(path):
        chk("Base de la demo existe", False, f"{os.path.basename(path)} no existe: ejecuta ./run_demo_comercial.sh start")
    else:
        lab = os.path.join(os.path.dirname(path), "aicos.db")
        chk("Es un archivo distinto de la base del laboratorio (aicos.db)", not (os.path.exists(lab) and os.path.samefile(lab, path)), f"{os.path.basename(path)} · {os.path.getsize(path)//1024} KB")
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True); c.row_factory = sqlite3.Row
        def v(sql, *p): return c.execute(sql, p).fetchone()[0]
        co = c.execute("SELECT id FROM companies WHERE slug='clima-sur'").fetchone()
        chk("Empresa ficticia «Clima Sur»", bool(co))
        if co:
            cid = co["id"]
            roles = sorted(r[0] for r in c.execute("SELECT role FROM users WHERE company_id=?", (cid,)))
            chk("Dueño, administración y técnico presentes", roles == ["admin", "owner", "technician"], ",".join(roles))
            m = v("SELECT COUNT(*) FROM requests r JOIN customers cu ON cu.id=r.customer_id WHERE r.company_id=? AND cu.name='Marta Rojas' AND r.category='mantencion' AND r.status NOT IN ('cerrada','ejecucion_fallida','presupuesto_rechazado')", cid)
            chk("EXACTAMENTE una mantención abierta de Marta Rojas", m == 1, f"{m}")
            chk("Inventario coherente (KIT-MANT ≥ 1 y GAS-R410 > 0)", v("SELECT stock FROM parts WHERE company_id=? AND sku='KIT-MANT'", cid) >= 1 and v("SELECT stock FROM parts WHERE company_id=? AND sku='GAS-R410'", cid) > 0,
                f"KIT-MANT={v('SELECT stock FROM parts WHERE company_id=? AND sku=?', cid, 'KIT-MANT')} GAS-R410={v('SELECT stock FROM parts WHERE company_id=? AND sku=?', cid, 'GAS-R410')}")
            used = v("SELECT COUNT(*) FROM plans WHERE company_id=?", cid) + v("SELECT COUNT(*) FROM tasks WHERE company_id=?", cid)
            chk("Base sin uso previo (0 planes y 0 tareas)", used == 0, "" if used == 0 else f"{used} fila(s): la demo ya se usó → ./run_demo_comercial.sh reset", warn=True)
            chk("Sin ejecuciones fallidas ni aprobaciones pendientes ni alertas históricas", v("SELECT COUNT(*) FROM runs WHERE company_id=? AND status IN ('failed','blocked')", cid) + v("SELECT COUNT(*) FROM approvals WHERE company_id=? AND status='pendiente'", cid) == 0)
            chk("Ningún mensaje ENVIADO a terceros (canal externo simulado)", v("SELECT COUNT(*) FROM messages WHERE status='enviado'") == 0)
            chk("Visita de Pedro «propuesta» y NO confirmada", v("SELECT COUNT(*) FROM visits WHERE company_id=? AND status='propuesta' AND customer_confirmed=0", cid) == 1 and v("SELECT COUNT(*) FROM visits WHERE company_id=? AND customer_confirmed=1", cid) == 0)
    if a.url:
        try:
            info = json.loads(urllib.request.urlopen(a.url.rstrip("/") + "/api/demo/info", timeout=5).read())
            chk("Servidor responde en modo DEMO", info.get("demo") is True, f"{a.url} · BD {info.get('db')}")
            chk("Motor determinista (sin LLM ni red) por defecto", info.get("mode") == "deterministic", str(info.get("mode_label")))
            chk("La BD que sirve es la de la demo (no aicos.db)", str(info.get("db", "")).startswith("demo_comercial"), str(info.get("db")))
        except Exception as e:
            chk("Servidor responde en modo DEMO", False, f"{type(e).__name__}")
    w = max(len(r[1]) for r in rows)
    for st, name, detail in rows:
        print(f"  [{st:4s}] {name.ljust(w)}  {detail}")
    bad = [r for r in rows if r[0] == "FAIL"]
    print("  → PREFLIGHT " + ("OK: lista para presentar" if not bad else f"CON {len(bad)} FALLO(S)") + (f" · {sum(1 for r in rows if r[0]=='WARN')} aviso(s)" if any(r[0] == 'WARN' for r in rows) else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DemoSafetyError as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(2)
