#!/usr/bin/env python3
"""Comprobación de saneamiento de la vitrina (FR-007 / SC-002). Solo lectura. Sale con 1 si encuentra algo que NO debe publicarse.

Uso:  python tools/check_showcase.py [RAIZ]      (por defecto, la raíz de este repositorio)
Categorías (cada una tiene su mutación en tests/test_check_showcase.py: se planta un caso y la comprobación DEBE fallar):
  ruta_absoluta   rutas del Mac (/Users/<nombre>/…) o de otros equipos (C:\\Users\\…)
  correo_real     correos de proveedores públicos (los de la demo son *.test)
  herramienta     nombres del flujo de trabajo interno: NEXT_EXACT_STEP, ChatGPT, Codex, Claude Code, memoria-conversaciones
  clave           formas de clave/token (sk-…, ghp_…, AKIA…, bloques PRIVATE KEY, Bearer largos)
  archivo         extensiones que nunca deben publicarse (.env, .pem, .key, .p12, .sqlite, .db, .bak)
Los patrones son ESPECÍFICOS (no palabras sueltas como «handoff», que es lenguaje del dominio)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = {
    "ruta_absoluta": re.compile(r"/Users/(?!(?:ejemplo|example|usuario|user|tu-usuario)/)[A-Za-z0-9._-]+/|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+"),   # `/Users/ejemplo/…` es un MARCADOR de las pruebas
    "correo_real": re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|hotmail|outlook|live|yahoo|icloud|proton(?:mail)?)\.[a-z]{2,}", re.I),
    "herramienta": re.compile(r"NEXT_EXACT_STEP|ChatGPT|\bCodex\b|Claude Code|memoria-conversaciones", re.I),
    "clave": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}|\bghp_[A-Za-z0-9]{30,}|\bAKIA[0-9A-Z]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|[Bb]earer\s+[A-Za-z0-9._-]{32,}"),
}
BAD_NAMES = re.compile(r"(^|/)(\.env(\..*)?|[^/]+\.(pem|key|p12|pfx|sqlite3?|db|bak))$")
ALLOW_NAMES = {".env.example"}
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", "dist", "dist-v05"}
# Esta comprobación y su prueba CITAN los patrones a propósito; el informe de saneamiento y la especificación también.
SELF = {"tools/check_showcase.py", "tests/test_check_showcase.py", "tools/export_showcase.py", "SANEAMIENTO.md", "specs/001-showcase-portfolio-offer/spec.md", "specs/001-showcase-portfolio-offer/tasks.md"}
TEXT_EXT = {".py", ".md", ".ts", ".tsx", ".js", ".json", ".yml", ".yaml", ".toml", ".txt", ".sh", ".html", ".css", ".cfg", ".ini", ".example"}


def scan(root: Path) -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts) or not p.is_file():
            continue
        if BAD_NAMES.search(rel) and p.name not in ALLOW_NAMES:
            out.append((rel, "archivo", "extensión que no se publica"))
            continue
        if rel in SELF or (p.suffix.lower() not in TEXT_EXT and p.name not in ALLOW_NAMES):
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for cat, rx in PATTERNS.items():
                if cat == "clave" and rel.startswith("backend/tests/") and "secret=" in line:
                    continue                                                  # `retry_secret` ficticios de las pruebas (no tienen la forma de una clave real)
                if rx.search(line):
                    out.append((f"{rel}:{i}", cat, line.strip()[:100]))
    return out


def main(argv=None) -> int:
    root = Path((argv or sys.argv[1:] or [Path(__file__).resolve().parents[1]])[0]).resolve()
    found = scan(root)
    for where, cat, what in found:
        print(f"{cat:14} {where}  {what}")
    print(f"\n{'FALLA' if found else 'OK'}: {len(found)} hallazgo(s) en {root.name}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
