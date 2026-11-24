"""Persistence layer: SQLAlchemy Core, portable SQLite -> PostgreSQL (only AICOS_DB_URL changes).

Conventions: text primary keys, JSON stored as text, ISO-8601 UTC timestamps, and EVERY domain table
carries company_id (tenant isolation is enforced in the query layer, never trusted from the client).
"""
from __future__ import annotations

import json
import secrets

from sqlalchemy import (Column, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, event, text)
from sqlalchemy.engine import Engine

from . import clock

md = MetaData()


def now() -> str:
    return clock.utcnow().isoformat(timespec="milliseconds")


def uid(prefix: str = "") -> str:
    return prefix + secrets.token_hex(6)


def jdump(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def jload(s, default=None):
    if s in (None, ""):
        return default
    return json.loads(s)


def _t(name, *cols, **kw):
    return Table(name, md, *cols, **kw)


CID = lambda: Column("company_id", String, ForeignKey("companies.id"), nullable=False, index=True)  # noqa: E731
PK = lambda: Column("id", String, primary_key=True)  # noqa: E731
TS = lambda n="created_at": Column(n, String, nullable=False)  # noqa: E731

companies = _t("companies", PK(), Column("slug", String, unique=True, nullable=False), Column("name", String, nullable=False),
               Column("industry", String), TS())
users = _t("users", PK(), CID(), Column("email", String, unique=True, nullable=False), Column("name", String, nullable=False),
           Column("role", String, nullable=False), Column("password_hash", String, nullable=False),
           Column("is_demo", Integer, default=1), TS(), Column("active", Integer, default=1))   # V0.5: employees can be deactivated (NULL = active, for V0.4 databases)
sessions = _t("sessions", Column("token_hash", String, primary_key=True), Column("user_id", String, nullable=False),
              CID(), TS(), Column("expires_at", String, nullable=False),
              Column("csrf", String), Column("last_seen_at", String),     # V0.6: synchronizer token bound to the session + sliding idle timeout (NULL on pre-V0.6 rows)
              Column("auth_time", String))                                # V0.7 P2: instante de la última AUTENTICACIÓN activa (contraseña o proveedor); NULL = «sin dato» = nunca reciente. Sliding/touch NO lo tocan
# V0.6: failed/successful sign-in attempts. NO company_id on purpose: an unknown e-mail has no tenant, and it must be throttled exactly like a known one.
login_attempts = _t("login_attempts", Column("id", Integer, primary_key=True, autoincrement=True), Column("at", String, nullable=False, index=True),
                    Column("email_key", String, nullable=False), Column("ip", String, nullable=False), Column("ok", Integer, nullable=False))
Index("ix_login_attempts_email", login_attempts.c.email_key, login_attempts.c.at)
Index("ix_login_attempts_ip", login_attempts.c.ip, login_attempts.c.at)
# V0.6 P4: rate limits of the public channel (ratelimit.py). ONE row per bucket holding one number (`tat`, GCRA's theoretical arrival time, epoch seconds). `bucket` is a keyed hash
# (HMAC-SHA256, key in rate_limit_meta) of kind|company|subject: no address, token or request id is ever stored. A row whose `tat` is in the past is equivalent to no row and is deleted.
# Both tables are runtime state, not data: a restore clears them (backup.RUNTIME_STATE). company_id is deliberately not a foreign key (a bucket may outlive a deleted tenant for ≤ one window).
rate_limits = _t("rate_limits", Column("bucket", String, primary_key=True), Column("kind", String, nullable=False), Column("company_id", String), Column("tat", Float, nullable=False))
Index("ix_rate_limits_tat", rate_limits.c.tat)
rate_limit_meta = _t("rate_limit_meta", Column("name", String, primary_key=True), Column("value", String, nullable=False))
# V0.7 P2 (Issue #23): identidad externa. `identities` dice A QUIÉN corresponde (provider, subject): los inicios de sesión posteriores usan el subject, NUNCA el correo. El rol y la empresa salen de `users`.
identities = _t("identities", Column("provider", String, primary_key=True), Column("subject", String, primary_key=True), Column("user_id", String, ForeignKey("users.id"), nullable=False), CID(),
                TS(), Column("last_login_at", String), UniqueConstraint("provider", "user_id"))
# Invitaciones: correo + rol + empresa los fija el dueño; el secreto del enlace SOLO existe como hash; caducan y se usan una vez (UPDATE condicional). Una pendiente por correo y empresa (índice parcial).
invitations = _t("invitations", PK(), CID(), Column("email_key", String, nullable=False), Column("name", String, nullable=False), Column("role", String, nullable=False),
                 Column("token_hash", String, nullable=False, unique=True), Column("status", String, nullable=False), Column("created_by", String, nullable=False), TS(),
                 Column("expires_at", String, nullable=False), Column("accepted_at", String), Column("accepted_user_id", String), Column("revoked_at", String),
                 # V0.7 P2.5: `kind` 'employee' (NULL en bases de P2 = 'employee') | 'owner_link' (el OPERADOR vincula a un DUEÑO EXISTENTE: `target_user_id`; jamás crea otra persona ni otro dueño)
                 Column("kind", String), Column("target_user_id", String))
Index("uq_invitations_pending", invitations.c.company_id, invitations.c.email_key, unique=True, sqlite_where=text("status='pending'"))
# Flujos de acceso en curso (state + PKCE + nonce + cookie de correlación), de vida corta y un solo uso. SIN company_id a propósito (como login_attempts): un acceso normal aún no sabe su empresa.
# `state` y la cookie solo se guardan como hash; el verificador PKCE debe poder presentarse al proveedor, por eso va en claro y se borra al usarse o caducar (≤ 10 min). Estado de ejecución: una restauración lo vacía.
auth_flows = _t("auth_flows", Column("state_hash", String, primary_key=True), Column("cookie_hash", String, nullable=False), Column("nonce", String, nullable=False), Column("verifier", String, nullable=False),
                Column("purpose", String, nullable=False), Column("invitation_id", String), Column("user_id", String), Column("session_hash", String), TS(),
                Column("expires_at", String, nullable=False), Column("used_at", String))
agents = _t("agents", PK(), CID(), Column("role", String, nullable=False), Column("name", String, nullable=False),
            Column("description", Text), Column("user_id", String), Column("permissions", Text, nullable=False),
            Column("config", Text), Column("enabled", Integer, default=1))
# V0.5: a company can have SEVERAL personal assistants of the same template (one per employee). Shared agents (reception, coordinator) stay unique per company.
Index("uq_agents_shared", agents.c.company_id, agents.c.role, unique=True, sqlite_where=text("user_id IS NULL"))
Index("uq_agents_personal", agents.c.company_id, agents.c.role, agents.c.user_id, unique=True, sqlite_where=text("user_id IS NOT NULL"))
customers = _t("customers", PK(), CID(), Column("name", String, nullable=False), Column("phone", String), Column("email", String),
               Column("address", String), TS())
conversations = _t("conversations", PK(), CID(), Column("customer_id", String), Column("user_id", String),
                   Column("channel", String, nullable=False), Column("token", String), TS())
messages = _t("messages", PK(), CID(), Column("conversation_id", String, nullable=False), Column("sender", String, nullable=False),
              Column("body", Text, nullable=False), Column("status", String), TS(),
              Column("idem_key", String), UniqueConstraint("company_id", "idem_key"), Column("commitment_id", String))
requests = _t("requests", PK(), CID(), Column("customer_id", String, nullable=False), Column("conversation_id", String),
              Column("description", Text, nullable=False), Column("category", String), Column("urgency", String),
              Column("status", String, nullable=False), Column("idem_key", String, nullable=False),
              Column("assigned_user_id", String), TS(), TS("updated_at"), UniqueConstraint("company_id", "idem_key"))
work_orders = _t("work_orders", PK(), CID(), Column("request_id", String, nullable=False), Column("technician_id", String),
                 Column("status", String, nullable=False), Column("notes", Text), TS(), TS("updated_at"),
                 UniqueConstraint("company_id", "request_id"))
tasks = _t("tasks", PK(), CID(), Column("request_id", String), Column("run_id", String), Column("title", String, nullable=False),
           Column("assignee_user_id", String), Column("assignee_agent", String), Column("status", String, nullable=False),
           Column("idem_key", String, nullable=False), Column("due", String), TS(), UniqueConstraint("company_id", "idem_key"),
           Column("kind", String), Column("evidence", Text), Column("commitment_id", String), Column("completed_by", String), Column("completed_at", String))
delegations = _t("delegations", PK(), CID(), Column("run_id", String, nullable=False), Column("from_agent", String, nullable=False),
                 Column("to_agent", String, nullable=False), Column("task", Text), Column("status", String, nullable=False),
                 Column("idem_key", String, nullable=False), TS(), UniqueConstraint("company_id", "idem_key"))
approvals = _t("approvals", PK(), CID(), Column("run_id", String, nullable=False), Column("request_id", String, nullable=False),
               Column("subject", String, nullable=False), Column("summary", Text), Column("amount", Float),
               Column("status", String, nullable=False), Column("requested_by_agent", String),
               Column("decided_by_user_id", String), Column("decided_at", String), Column("comment", Text), TS(),
               UniqueConstraint("company_id", "run_id", "subject"))
tool_catalog = _t("tool_catalog", Column("name", String, primary_key=True), Column("description", Text),
                  Column("permission", String), Column("mode", String), Column("access", String), Column("enabled", Integer))
integrations = _t("integrations", PK(), CID(), Column("kind", String, nullable=False), Column("name", String, nullable=False),
                  Column("mode", String, nullable=False), Column("enabled", Integer, default=0), Column("note", Text))
runs = _t("runs", PK(), CID(), Column("request_id", String, nullable=False), Column("scenario", String),
          Column("status", String, nullable=False), Column("phase", String), Column("state", Text), Column("provider", String),
          Column("created_by", String), Column("idem_key", String, nullable=False), TS(), TS("updated_at"),
          UniqueConstraint("company_id", "idem_key"))
events = _t("events", Column("id", Integer, primary_key=True, autoincrement=True), CID(), Column("run_id", String, nullable=False),
            Column("seq", Integer, nullable=False), TS("ts"), Column("type", String, nullable=False), Column("agent", String),
            Column("mode", String), Column("title", String), Column("data", Text), UniqueConstraint("run_id", "seq"))
Index("ix_events_run", events.c.run_id, events.c.id)
results = _t("results", PK(), CID(), Column("run_id", String), Column("request_id", String), Column("kind", String),
             Column("summary", Text), Column("data", Text), TS())
audit_log = _t("audit_log", Column("id", Integer, primary_key=True, autoincrement=True), CID(), TS("ts"),
               Column("actor_type", String), Column("actor_id", String), Column("action", String), Column("resource", String),
               Column("outcome", String), Column("detail", Text))
availability = _t("availability", PK(), CID(), Column("technician_id", String, nullable=False), Column("slot_start", String, nullable=False),
                  Column("slot_end", String, nullable=False), Column("status", String, nullable=False), Column("request_id", String))
parts = _t("parts", PK(), CID(), Column("sku", String, nullable=False), Column("name", String, nullable=False), Column("stock", Integer, nullable=False),
           Column("unit_price", Float, nullable=False), Column("alt_sku", String), UniqueConstraint("company_id", "sku"))
services = _t("services", PK(), CID(), Column("code", String, nullable=False), Column("category", String, nullable=False),
              Column("name", String, nullable=False), Column("price", Float, nullable=False), UniqueConstraint("company_id", "code"))
quotes = _t("quotes", PK(), CID(), Column("request_id", String, nullable=False), Column("kind", String, nullable=False),
            Column("lines", Text, nullable=False), Column("total", Float, nullable=False), Column("status", String, nullable=False), TS(),
            UniqueConstraint("company_id", "request_id", "kind"))
visits = _t("visits", PK(), CID(), Column("request_id", String, nullable=False), Column("technician_id", String),
            Column("slot_start", String), Column("slot_end", String), Column("status", String, nullable=False),
            Column("customer_confirmed", Integer, nullable=False, default=0), Column("confirmed_at", String),
            Column("confirm_code", String), TS(),
            UniqueConstraint("company_id", "request_id"))
material_requests = _t("material_requests", PK(), CID(), Column("request_id", String), Column("sku", String, nullable=False),
                       Column("qty", Integer, nullable=False), Column("status", String, nullable=False),
                       Column("requested_by", String), Column("idem_key", String, nullable=False), TS(),
                       UniqueConstraint("company_id", "idem_key"), Column("run_id", String), Column("cost", Float), Column("reason", Text))
commitments = _t("commitments", PK(), CID(), Column("request_id", String, nullable=False), Column("run_id", String),
                 Column("owner_user_id", String, nullable=False), Column("title", String, nullable=False), Column("due_at", String, nullable=False),
                 Column("status", String, nullable=False), Column("origin", String), Column("customer_facing", Integer, default=0),
                 Column("reminder_sent", Integer, default=0), Column("last_evidence", Text), Column("idem_key", String, nullable=False),
                 TS(), TS("updated_at"), UniqueConstraint("company_id", "idem_key"))
# V0.6: which process is the live instance on this database (see lease.py). One row, id=1.
instance_lease = _t("instance_lease", Column("id", Integer, primary_key=True), Column("boot_id", String, nullable=False), Column("host", String, nullable=False),
                    Column("pid", Integer, nullable=False), Column("started_at", String, nullable=False), Column("heartbeat_at", String, nullable=False))
plans = _t("plans", PK(), CID(), Column("created_by", String, nullable=False), Column("text", Text, nullable=False), Column("status", String, nullable=False),
           Column("source", String), Column("intent", String), Column("request_id", String), Column("summary", Text), Column("proposal", Text),
           Column("excluded", Text), Column("clarification", Text), Column("rounds", Integer, default=0), Column("idem_key", String, nullable=False),
           Column("confirmed_by", String), Column("confirmed_at", String), Column("result_summary", Text), Column("status_reason", Text),
           TS(), TS("updated_at"), UniqueConstraint("company_id", "idem_key"))
plan_steps = _t("plan_steps", PK(), CID(), Column("plan_id", String, nullable=False, index=True), Column("seq", Integer, nullable=False),
                Column("action", String, nullable=False), Column("agent", String, nullable=False), Column("assignee_user_id", String),
                Column("args", Text), Column("depends_on", Integer), Column("access", String, nullable=False), Column("permission", String),
                Column("origin", String), Column("title", String), Column("status", String, nullable=False), Column("task_id", String),
                Column("result", Text), TS(), TS("updated_at"), UniqueConstraint("plan_id", "seq"))
private_notes = _t("private_notes", PK(), CID(), Column("user_id", String, nullable=False), Column("body", Text, nullable=False), TS())


def make_engine(url: str) -> Engine:
    kw = {}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(url, future=True, **kw)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _pragma(dbapi, _):  # WAL: engine thread writes while SSE/API threads read
            cur = dbapi.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    return eng


def init_db(eng: Engine) -> None:
    _rebuild_agents_if_v04(eng)   # BEFORE create_all: the V0.4 table has UNIQUE(company_id, role) which forbids two admin assistants
    md.create_all(eng)
    migrate(eng)


def _backup_before_migration(eng: Engine) -> str | None:
    """A V0.4 database file is copied (SQLite backup API, consistent even with WAL) to `<file>.pre-v05-<timestamp>.bak` BEFORE the one non-additive migration."""
    import os, sqlite3, time
    path = eng.url.database
    if not path or path == ":memory:" or not os.path.exists(path):
        return None
    dest = f"{path}.pre-v05-{time.strftime('%Y%m%d%H%M%S')}.bak"
    src, dst = sqlite3.connect(f"file:{path}?mode=ro", uri=True), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    return dest


def _rebuild_agents_if_v04(eng: Engine) -> bool:
    """V0.4 → V0.5, the only non-additive step: SQLite cannot drop a table constraint, so the `agents` table is rebuilt (same columns, same rows, new unique
    indexes) inside ONE transaction. Idempotent; a database that is already V0.5 (or brand new) is left alone. Back the file up before the first start."""
    from sqlalchemy import inspect
    insp = inspect(eng)
    if "agents" not in insp.get_table_names():
        return False
    old = any(set(u["column_names"]) == {"company_id", "role"} for u in insp.get_unique_constraints("agents"))
    if not old:
        return False
    _backup_before_migration(eng)
    cols = [c["name"] for c in insp.get_columns("agents")]
    old_idx = [i["name"] for i in insp.get_indexes("agents") if i["name"] and not i["name"].startswith("sqlite_")]
    with eng.begin() as c:
        c.execute(text("ALTER TABLE agents RENAME TO agents_v04"))
        for n in old_idx:   # indexes keep their names when a table is renamed → free them for the new table
            c.execute(text(f'DROP INDEX IF EXISTS "{n}"'))
        md.tables["agents"].create(c)
        keep = ",".join(x for x in cols if x in md.tables["agents"].c)
        c.execute(text(f"INSERT INTO agents({keep}) SELECT {keep} FROM agents_v04"))
        c.execute(text("DROP TABLE agents_v04"))
    return True


def migrate(eng: Engine) -> None:
    """Additive, non-destructive migration: new tables come from create_all; new NULLABLE columns are ALTERed in.
    Existing data is never dropped (the dev DB survives version upgrades)."""
    from sqlalchemy import inspect
    insp = inspect(eng)
    with eng.begin() as c:
        for t in md.sorted_tables:
            have = {col["name"] for col in insp.get_columns(t.name)}
            for col in t.columns:
                if col.name not in have:
                    c.execute(text(f'ALTER TABLE {t.name} ADD COLUMN {col.name} {col.type.compile(dialect=eng.dialect)}'))
                    if (t.name, col.name) == ("users", "active"):
                        c.execute(text("UPDATE users SET active=1 WHERE active IS NULL"))


def q(conn, sql: str, **p):
    return conn.execute(text(sql), p)


def one(conn, sql: str, **p):
    r = q(conn, sql, **p).mappings().first()
    return dict(r) if r else None


def all_(conn, sql: str, **p):
    return [dict(r) for r in q(conn, sql, **p).mappings().all()]
