import sys

from .pilot import EX_CONFIG, PilotConfigError

try:
    from .api import create_app
    app = create_app()
except PilotConfigError as e:                                      # configuración insegura del modo piloto: salida limpia (sin traza) y código ≠ 0, antes de abrir la base
    print(f"ERROR: {e}", file=sys.stderr)
    raise SystemExit(EX_CONFIG)
