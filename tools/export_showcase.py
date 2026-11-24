#!/usr/bin/env python3
"""Exporta la vitrina desde el repositorio privado: copia SELECTIVA (lista blanca) de los archivos que Git rastrea, SIN historial, y reescribe en la COPIA las menciones del flujo de trabajo interno.
El repositorio privado no se modifica (solo se lee con `git ls-files`). Reproducible: ejecutarlo dos veces da el mismo resultado.

Uso:  python tools/export_showcase.py --src ../ai-company-os [--dry-run]
Después:  python tools/check_showcase.py   (debe dar OK)"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

DST = Path(__file__).resolve().parents[1]

INCLUDE = (
    "backend/app/", "backend/tests/", "backend/pyproject.toml", "backend/requirements.txt",
    "frontend/src/", "frontend/e2e/", "frontend/e2e_pilot/",
    "frontend/package.json", "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.ts", "frontend/index.html",
    "frontend/postcss.config.js", "frontend/tailwind.config.js", "frontend/playwright.config.ts", "frontend/playwright.pilot.config.ts", "frontend/playwright.demo.config.ts",
    "qa/", ".github/workflows/ci.yml", ".gitignore", "run_demo.sh",
    "docs/ARCHITECTURE.md", "docs/ROADMAP_V1_PRODUCTO.md", "docs/v05/", "docs/v06/", "docs/v07/", "docs/screenshots/",
    "docs/demo/REAL_VS_SIMULADO.md", "docs/demo/RUNBOOK_DEMO.md", "docs/demo/screenshots/",
)
EXCLUDE = ("docs/demo/DEMO_COMERCIAL_V04.md", "docs/v05/v05-results.json")     # documento de planificación con el flujo interno; resultados regenerables
TEXT_EXT = {".md", ".py", ".ts", ".tsx", ".js", ".json", ".yml", ".yaml", ".toml", ".txt", ".sh", ".html", ".css"}

# Reescrituras en la COPIA (el valor son las historias de ingeniería; no el nombre de la herramienta que hizo cada revisión).
DROP_LINE = re.compile(r"NEXT_EXACT_STEP|ChatGPT|Claude Code|memoria-conversaciones")
REPLACE = [(re.compile(r"\bCodex\b"), "revisión automatizada")]


def tracked(src: Path) -> list[str]:
    return subprocess.run(["git", "-C", str(src), "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()


def wanted(rel: str) -> bool:
    return rel.startswith(INCLUDE) or rel in INCLUDE if rel not in EXCLUDE else False


def rewrite(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        if DROP_LINE.search(line):
            continue
        for rx, new in REPLACE:
            line = rx.sub(new, line)
        out.append(line)
    return "".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    src = Path(a.src).resolve()
    files = [f for f in tracked(src) if wanted(f)]
    if a.dry_run:
        print(f"{len(files)} archivos se copiarían de {src.name}")
        return 0
    n = rewritten = 0
    for rel in files:
        s, d = src / rel, DST / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if Path(rel).suffix.lower() in TEXT_EXT:
            raw = s.read_text()
            new = rewrite(raw)
            rewritten += new != raw
            d.write_text(new)
        else:
            shutil.copy2(s, d)
        n += 1
    print(f"copiados {n} archivos ({rewritten} reescritos para quitar menciones del flujo interno)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
