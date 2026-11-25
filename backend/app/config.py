"""Runtime settings. Local-only by default; every knob is an env var (no secrets in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    env: str = os.environ.get("AICOS_ENV", "")                                          # "" / local = comportamiento de siempre | pilot = V0.7 P1 (arranque seguro, sin demostración; ver pilot.py)
    db_url: str = os.environ.get("AICOS_DB_URL", f"sqlite:///{ROOT / 'data' / 'aicos.db'}")
    step_delay: float = float(os.environ.get("AICOS_STEP_DELAY", "0.55"))  # pacing so the graph is watchable
    sync_runs: bool = os.environ.get("AICOS_SYNC", "0") == "1"  # tests: run engine inline
    llm_provider: str = os.environ.get("AICOS_LLM_PROVIDER", "deterministic")  # deterministic | ollama
    # V0.10 (spec 005, fase 2) — IA de pago (API de Anthropic). La CLAVE solo se lee del entorno (AICOS_ANTHROPIC_API_KEY, deliberadamente NO la variable estándar para no gastar por accidente con la de otra herramienta).
    anthropic_api_key: str = os.environ.get("AICOS_ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.environ.get("AICOS_ANTHROPIC_MODEL", "claude-opus-5-5")
    anthropic_price_in: str = os.environ.get("AICOS_ANTHROPIC_PRICE_IN", "")     # USD por millón de tokens de ENTRADA; vacío = tabla conocida del modelo; modelo desconocido sin precio = se niega a arrancar
    anthropic_price_out: str = os.environ.get("AICOS_ANTHROPIC_PRICE_OUT", "")   # USD por millón de tokens de SALIDA
    llm_budget_day_usd: float = float(os.environ.get("AICOS_LLM_BUDGET_DAY_USD", "1"))      # tope DURO por día (UTC)
    llm_budget_month_usd: float = float(os.environ.get("AICOS_LLM_BUDGET_MONTH_USD", "10"))  # tope DURO por mes (UTC)
    llm_plan_timeout: float = float(os.environ.get("AICOS_LLM_PLAN_TIMEOUT", "60"))   # plazo duro (s) para que un modelo proponga un plan: pasado el plazo, se usan las reglas (rotulado)
    ollama_url: str = os.environ.get("AICOS_OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.environ.get("AICOS_OLLAMA_MODEL", "qwen3.5:4b-q4_K_M")
    static_dir: Path = Path(os.environ.get("AICOS_STATIC_DIR", str(ROOT.parent / "frontend" / "dist")))
    session_hours: int = 12                                                             # absolute lifetime of a session
    session_idle_minutes: int = int(os.environ.get("AICOS_SESSION_IDLE_MINUTES", "120"))   # inactivity limit (sliding)
    cookie_secure: str = os.environ.get("AICOS_COOKIE_SECURE", "auto")                  # auto = Secure when the request arrived over https | 1 = always | 0 = never (plain-http lab)
    trusted_origins: tuple = tuple(x.strip().lower() for x in os.environ.get("AICOS_TRUSTED_ORIGINS", "").split(",") if x.strip())   # extra browser origins (host[:port]) allowed to write
    login_window_seconds: int = int(os.environ.get("AICOS_LOGIN_WINDOW_SECONDS", "900"))   # rolling window for failed-login counters
    login_max_pair: int = int(os.environ.get("AICOS_LOGIN_MAX_PAIR", "5"))              # failures per (email, address) before that pair is locked
    login_max_email: int = int(os.environ.get("AICOS_LOGIN_MAX_EMAIL", "20"))           # failures per email from ANY address (distributed guessing)
    login_max_ip: int = int(os.environ.get("AICOS_LOGIN_MAX_IP", "50"))                 # failures per address across all emails (credential stuffing)
    # V0.6 P4 — limits of the PUBLIC customer channel (/api/public/*). Each rule is "<requests>/<window seconds>": a burst of <requests>, refilled evenly over the window.
    # `src` = one source address inside one company, `co` = the whole company, `target` = one visit request, `miss` = lookups that find nothing. A malformed rule stops the server
    # at startup. There is deliberately NO switch that turns the limits off and no test/demo mode that relaxes them: loosen them here, explicitly, if you must.
    rl_chat_src: str = os.environ.get("AICOS_RL_CHAT_SRC", "20/600")                 # new conversations (each one starts an engine run)
    rl_chat_co: str = os.environ.get("AICOS_RL_CHAT_CO", "200/3600")
    rl_outbox_src: str = os.environ.get("AICOS_RL_OUTBOX_SRC", "150/60")             # reads of a conversation; one open browser tab polls ~50/min
    rl_outbox_co: str = os.environ.get("AICOS_RL_OUTBOX_CO", "1200/60")
    rl_outbox_miss: str = os.environ.get("AICOS_RL_OUTBOX_MISS", "10/300")           # reads whose token does not exist (enumeration)
    rl_confirm_src: str = os.environ.get("AICOS_RL_CONFIRM_SRC", "10/600")           # visit-confirmation attempts: the strictest
    rl_confirm_target: str = os.environ.get("AICOS_RL_CONFIRM_TARGET", "5/900")      # attempts on ONE request, from any source (the code is only 24 bits)
    rl_confirm_co: str = os.environ.get("AICOS_RL_CONFIRM_CO", "60/600")
    rl_unknown_src: str = os.environ.get("AICOS_RL_UNKNOWN_SRC", "20/60")            # unknown company slugs and unknown public paths (scanning)
    rl_unknown_all: str = os.environ.get("AICOS_RL_UNKNOWN_ALL", "200/60")
    # Reverse proxies whose X-Forwarded-For is believed: comma-separated IPs/CIDRs. EMPTY (default) = no header is ever trusted. Run uvicorn with --no-proxy-headers (see docs/v06).
    trusted_proxies: tuple = tuple(x.strip() for x in os.environ.get("AICOS_TRUSTED_PROXIES", "").split(",") if x.strip())
    demo_mode: bool = os.environ.get("AICOS_DEMO_MODE", "0") == "1"      # commercial-demo environment only (see run_demo_comercial.sh)
    allow_company_creation: bool = os.environ.get("AICOS_ALLOW_COMPANY_CREATION", "0") == "1"   # LOCAL/DEMO only: creating a company is not a public sign-up
    # V0.7 P2 — identidad. NO hay ningún valor que active el proveedor simulado: `AICOS_IDP_PROVIDER` distinto de vacío detiene el arranque (la integración real es P3).
    idp_provider: str = os.environ.get("AICOS_IDP_PROVIDER", "")
    reauth_max_age_seconds: int = int(os.environ.get("AICOS_REAUTH_MAX_AGE_SECONDS", "900"))   # antigüedad máxima de `auth_time` para operaciones sensibles (en el piloto: 1–900)
    rl_idp_start_src: str = os.environ.get("AICOS_RL_IDP_START_SRC", "30/600")       # inicios de acceso con el proveedor: por ORIGEN (cualquier tipo: sin invitación, con enlace válido o inventado, reauth)
    rl_idp_start_all: str = os.environ.get("AICOS_RL_IDP_START_ALL", "300/600")      # …y GLOBAL: acota los flujos vivos de `auth_flows` (vida 10 min) y las llamadas al proveedor
    rl_idp_invite_co: str = os.environ.get("AICOS_RL_IDP_INVITE_CO", "20/3600")      # invitaciones creadas por una empresa
    lease_heartbeat_seconds: float = float(os.environ.get("AICOS_LEASE_HEARTBEAT", "10"))   # how often the active instance renews the DB lease / a standby tries to take it (TTL is 30 s)
    commit: str = os.environ.get("AICOS_COMMIT", "")                      # shown in the demo banner / preflight (set by the launcher)


settings = Settings()
