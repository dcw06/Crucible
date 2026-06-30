"""
state/db.py — Shared State (PostgreSQL or SQLite)
==================================================
Written by FastAPI webhook worker; read by Streamlit dashboard.

On Render (or any cloud deploy), set DATABASE_URL to a PostgreSQL connection
string — both the webhook and dashboard services connect to the same DB.

Locally, leave DATABASE_URL unset and SQLite is used instead
(file path from STATE_DB env var, defaults to /tmp/crucible_state.db).

Render sets DATABASE_URL automatically when you attach a PostgreSQL database
to a service — just add the same env var to both the webhook and dashboard
services in the Render dashboard.
"""

import json
import os
import sqlite3
from pathlib import Path

# ── Backend detection ─────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH      = Path(os.environ.get("STATE_DB", "/tmp/crucible_state.db"))

# Render's DATABASE_URL uses "postgres://" — psycopg2 requires "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    print(f"[DB] Using PostgreSQL ({DATABASE_URL[:40]}...)")
else:
    print(f"[DB] Using SQLite ({DB_PATH})")


PIPELINE_STEPS = [
    "Queued",
    "PR files fetched",
    "Requirements analyzed",
    "Tests generated (Writer)",
    "Tests reviewed (Critic)",
    "Security scanned (Semgrep)",
    "pytest executed",
    "Results uploaded to TC",
    "TC run polled & scored",
    "Complete",
]

# ── DDL ───────────────────────────────────────────────────────────────────

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        pr_number     INTEGER PRIMARY KEY,
        step          INTEGER DEFAULT 0,
        step_label    TEXT    DEFAULT 'Queued',
        confidence    REAL,
        tc_pass_rate  REAL,
        findings      TEXT,
        req_coverage  TEXT,
        fragile_count INTEGER DEFAULT 0,
        security_summary TEXT,
        gate          TEXT,
        error_message TEXT,
        updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""


# ── Connection helpers ────────────────────────────────────────────────────

def _pg_conn():
    """Open a new psycopg2 connection."""
    return psycopg2.connect(DATABASE_URL)


def _sqlite_conn():
    """Open a new sqlite3 connection."""
    return sqlite3.connect(DB_PATH)


# ── Public API ────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create the pipeline_runs table if it doesn't exist."""
    if USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_CREATE_TABLE)
    else:
        with _sqlite_conn() as conn:
            conn.execute(_CREATE_TABLE)


def upsert_run(pr_number: int, **kwargs) -> None:
    """Insert or update a pipeline run record. Pass any column=value kwargs."""
    # Serialize JSON fields
    for key in ("findings", "req_coverage"):
        if key in kwargs and not isinstance(kwargs[key], str):
            kwargs[key] = json.dumps(kwargs[key])

    if USE_POSTGRES:
        cols        = list(kwargs.keys())
        vals        = list(kwargs.values())
        col_list    = ", ".join(cols)
        placeholder = ", ".join(["%s"] * len(cols))
        # Use EXCLUDED.col syntax so we don't need to repeat values
        set_clause  = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
        sql = f"""
            INSERT INTO pipeline_runs (pr_number, {col_list})
            VALUES (%s, {placeholder})
            ON CONFLICT (pr_number) DO UPDATE SET
                {set_clause},
                updated_at=CURRENT_TIMESTAMP
        """
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [pr_number] + vals)
    else:
        set_clause = ", ".join(f"{k}=?" for k in kwargs)
        values     = list(kwargs.values())
        sql = f"""
            INSERT INTO pipeline_runs (pr_number, {', '.join(kwargs.keys())})
            VALUES (?, {', '.join('?' * len(kwargs))})
            ON CONFLICT(pr_number) DO UPDATE SET
                {set_clause},
                updated_at=CURRENT_TIMESTAMP
        """
        with _sqlite_conn() as conn:
            conn.execute(sql, [pr_number] + values + values)


def update_step(pr_number: int, step: int, label: str) -> None:
    """Advance the pipeline step indicator. Label is always required —
    the PIPELINE_STEPS list uses 0-based indexing and must not be used
    as a fallback for 1-based step numbers (would give wrong labels)."""
    upsert_run(pr_number, step=step, step_label=label)


def get_run(pr_number: int) -> dict | None:
    if USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM pipeline_runs WHERE pr_number = %s",
                    (pr_number,)
                )
                row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
    else:
        with _sqlite_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM pipeline_runs WHERE pr_number = ?",
                (pr_number,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)

    # Deserialize JSON fields — use type-correct fallbacks on parse failure
    _fallbacks = {"findings": [], "req_coverage": {}}
    for key, fallback in _fallbacks.items():
        raw = d.get(key)
        if raw:
            try:
                d[key] = json.loads(raw)
            except Exception:
                d[key] = fallback
    return d


def list_runs() -> list[dict]:
    if USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM pipeline_runs ORDER BY updated_at DESC LIMIT 20"
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]
    else:
        with _sqlite_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM pipeline_runs ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
        return [dict(r) for r in rows]


# Auto-init on import
init_db()
