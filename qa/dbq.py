#!/usr/bin/env python3
"""Read-only SQL against the audit DB copy (never the real DB). usage: dbq.py "<select ...>" [db-path]  → JSON rows."""
import json, sqlite3, sys
path = sys.argv[2] if len(sys.argv) > 2 else "backend/data/audit.db"
sql = sys.argv[1]
assert sql.lstrip().lower().startswith(("select", "with")), "read-only"
c = sqlite3.connect(f"file:{path}?mode=ro", uri=True); c.row_factory = sqlite3.Row
print(json.dumps([dict(r) for r in c.execute(sql).fetchall()], ensure_ascii=False, default=str))
