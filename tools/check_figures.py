#!/usr/bin/env python3
"""Las cifras que se repiten en los textos (README, caso de estudio, guion del video, materiales) deben coincidir con las del sitio, que a su vez las recalcula desde el repositorio (`site/check_site.py`).
Uso:  python tools/check_figures.py [RAIZ]       Sale con 1 si una cifra de un texto no coincide, o si un texto que debía citar una cifra ya no la cita (para no dejar de comprobar en silencio)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

# archivo → {cifra: [patrones con UN grupo de captura numérico]}; cada patrón DEBE aparecer al menos una vez en ese archivo.
E2E = [r"\| (\d+ \+ \d+ \+ \d+ \+ \d+) \|"]
EXPECT = {
    "README.md": {"tests": [r"\| \*\*(\d+)\*\* \| `cd backend", r"collect-only -q` \((\d+)\)"], "mutaciones": [r"\*\*(\d+)\*\* \(29 "], "e2e": E2E},
    "docs/CASO_DE_ESTUDIO.md": {"tests": [r"Pruebas automáticas \| (\d+) \|"], "mutaciones": [r"\*\*(\d+)\*\* mutaciones", r"\| (\d+) \(29 \+"], "e2e": E2E},
    "docs/VIDEO_GUION.md": {"tests": [r"`(\d+) passed`"], "mutaciones": []},
    "materiales/CV_VINETAS.md": {"tests": [r"\*\*(\d+) pruebas automatizadas\*\*", r"\*\*(\d+) automated tests\*\*", r"\| (\d+) pruebas \|"], "mutaciones": [r"\*\*(\d+) mutaciones de seguridad\*\*", r"\*\*(\d+) security mutation"]},
    "materiales/DESCRIPCION_SERVICIO.md": {"tests": [r": (\d+) pruebas,"], "mutaciones": [r"(\d+) mutaciones de seguridad"]},
    "materiales/LINKEDIN_POST.md": {"tests": [r"(\d+) pruebas automáticas y"], "mutaciones": [r"(\d+) mutaciones de seguridad"]},
}


def site_facts(root: Path) -> dict[str, int]:
    page = (root / "site" / "index.html").read_text()
    return {k: int(v) for k, v in re.findall(r'data-fact="([a-z0-9_]+)">(\d+)<', page)}


def check(root: Path = ROOT, facts: dict[str, int] | None = None, texts: dict[str, str] | None = None) -> list[str]:
    facts = facts or site_facts(root)
    errs = []
    for rel, spec in EXPECT.items():
        text = (texts or {}).get(rel)
        if text is None:
            p = root / rel
            if not p.exists():
                errs.append(f"{rel}: no existe"); continue
            text = p.read_text()
        for key, pats in spec.items():
            for pat in pats:
                found = re.findall(pat, text)
                if not found:
                    errs.append(f"{rel}: ya no cita la cifra «{key}» con el patrón {pat!r} (actualiza el texto o este comprobador)"); continue
                for g in found:
                    val = sum(int(x) for x in g.split("+")) if key == "e2e" else int(g)
                    if val != facts[key]:
                        errs.append(f"{rel}: «{key}» dice {val} y la fuente dice {facts[key]}")
    return errs


if __name__ == "__main__":
    e = check()
    for x in e:
        print("✖", x)
    print("OK: las cifras de los textos coinciden con el sitio" if not e else f"{len(e)} problema(s)")
    sys.exit(1 if e else 0)
