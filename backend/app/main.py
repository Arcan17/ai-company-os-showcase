import sys

from .pilot import EX_CONFIG, PilotConfigError
from .public_demo import PublicDemoConfigError

try:
    import os
    if os.environ.get("AICOS_PUBLIC_DEMO", "0") == "1":     # V0.9 (spec 004): demo pública con un espacio aislado por visitante
        from .public_demo import build_public_app
        app = build_public_app()
    else:
        from .api import create_app
        app = create_app()
except (PilotConfigError, PublicDemoConfigError) as e:                                      # configuración insegura del modo piloto: salida limpia (sin traza) y código ≠ 0, antes de abrir la base
    print(f"ERROR: {e}", file=sys.stderr)
    raise SystemExit(EX_CONFIG)
