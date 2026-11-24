#!/usr/bin/env python3
"""Comprobación del sitio (spec 004 FR-008/009/010): (1) la página publicada está al día con catalog.json y con las cifras actuales del repositorio, (2) cada cifra coincide con su fuente,
(3) toda evidencia citada existe, (4) cada módulo dice qué NO incluye, (5) no hay precios, correos personales, rutas locales, claves ni nombres del flujo interno.
Uso (raíz del repo):  backend/.venv/bin/python site/check_site.py        Sale con 1 si algo falla."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site  # noqa: E402
import facts as F  # noqa: E402

HERE = Path(__file__).resolve().parent
PRICE = re.compile(r"\$\s?\d|\bUS\$|\d[\d.,]*\s?(?:CLP|USD|UF|EUR)\b|\bCLP\b|\bUSD\b", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
ABS = re.compile(r"/Users/[A-Za-z0-9._-]+/|[A-Za-z]:\\Users\\")
# Los nombres se arman por partes a propósito: así este archivo no contiene los literales que el saneamiento de la vitrina busca (y se exporta sin reescrituras).
TOOLS = re.compile("|".join(["NEXT_EXACT" + "_STEP", "Chat" + "GPT", r"\bCo" + r"dex\b", "Claude " + "Code", "memoria-" + "conversaciones"]), re.I)
KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}|\bghp_[A-Za-z0-9]{30,}|\bAKIA[0-9A-Z]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def check(page: str | None = None, catalog: dict | None = None, facts: dict | None = None) -> list[str]:
    errs: list[str] = []
    page = (HERE / "index.html").read_text() if page is None else page
    catalog = json.loads((HERE / "catalog.json").read_text()) if catalog is None else catalog
    facts = F.compute() if facts is None else facts
    shown = {k: int(v) for k, v in re.findall(r'data-fact="([a-z0-9_]+)">(\d+)<', page)}
    for k, v in facts.items():
        if k not in shown:
            errs.append(f"cifra «{k}» ausente en la página")
        elif shown[k] != v:
            errs.append(f"cifra «{k}»: la página dice {shown[k]} y la fuente dice {v}")
    for m in catalog["modulos"]:
        if not m.get("no_incluye"):
            errs.append(f"módulo «{m['id']}» no declara qué NO incluye")
        for p in m["evidencia"]:
            if not (F.ROOT / p).exists():
                errs.append(f"módulo «{m['id']}»: la evidencia «{p}» no existe")
    text = page + json.dumps(catalog, ensure_ascii=False)
    for name, rx in (("precio", PRICE), ("correo", EMAIL), ("ruta local", ABS), ("nombre del flujo interno", TOOLS), ("clave", KEY)):
        for mt in rx.finditer(text):
            errs.append(f"{name} en el sitio: {mt.group(0)[:60]!r}")
    return errs


def main() -> int:
    facts = F.compute()
    errs = check(facts=facts)
    if (HERE / "index.html").read_text() != build_site.build():
        errs.append("site/index.html no está al día: ejecuta `python site/build_site.py`")
    for e in errs:
        print("✖", e)
    print("OK: el sitio coincide con sus fuentes" if not errs else f"{len(errs)} problema(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
