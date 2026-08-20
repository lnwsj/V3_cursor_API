"""Database helpers + schema migrations (Phase 1.1 refactor)."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

# ---------------------------------------------------------------------------
# Config (env-driven)
# ---------------------------------------------------------------------------
PG_HOST = os.getenv("CUTDEE_PG_HOST", "127.0.0.1")
PG_PORT = int(os.getenv("CUTDEE_PG_PORT", "6432"))
PG_NAME = os.getenv("CUTDEE_PG_NAME", "v3_cursor_api")
PG_USER = os.getenv("CUTDEE_PG_USER", "v3_cursor_api")
PG_PASS = os.getenv("CUTDEE_PG_PASSWORD", "v3_cursor_api_pwd_2026")
PG_POOL_MIN = max(1, int(os.getenv("CUTDEE_PG_POOL_MIN", "1")))
PG_POOL_MAX = max(PG_POOL_MIN, int(os.getenv("CUTDEE_PG_POOL_MAX", "5")))

# Connection pool (lazily initialized; None means use per-call connect)
_PG_POOL: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def init_pool() -> None:
    """Initialize the global PG connection pool (called from app lifespan)."""
    global _PG_POOL
    if _PG_POOL is not None:
        return
    _PG_POOL = psycopg2.pool.ThreadedConnectionPool(
        PG_POOL_MIN, PG_POOL_MAX,
        host=PG_HOST, port=PG_PORT, dbname=PG_NAME, user=PG_USER, password=PG_PASS,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def close_pool() -> None:
    """Close the global PG connection pool (called from app shutdown)."""
    global _PG_POOL
    if _PG_POOL is not None:
        _PG_POOL.closeall()
        _PG_POOL = None


def _pg_conn():
    """Get a PG connection (uses the pool if available)."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2-binary is required for Gateway database access")
    if _PG_POOL is not None:
        return _PG_POOL.getconn()
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_NAME, user=PG_USER, password=PG_PASS,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _pg_release(conn) -> None:
    """Return connection to the pool (or close if no pool)."""
    if _PG_POOL is not None:
        _PG_POOL.putconn(conn)
    else:
        conn.close()


@contextmanager
def pg_conn() -> Iterator[Any]:
    """Context manager: yields a connection, auto-closes."""
    conn = _pg_conn()
    try:
        yield conn
    finally:
        _pg_release(conn)


@contextmanager
def pg_cursor(commit: bool = False) -> Iterator[Any]:
    """Context manager: yields a cursor with auto-release/commit."""
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        try:
            yield cur
            if commit:
                conn.commit()
        finally:
            cur.close()
    finally:
        _pg_release(conn)


# ---------------------------------------------------------------------------
# Schema + migrations
# ---------------------------------------------------------------------------
SCHEMA_BASE = """
CREATE TABLE IF NOT EXISTS v3_jobs (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    worker_id TEXT,
    tc TEXT NOT NULL DEFAULT 'tc01',
    status TEXT NOT NULL DEFAULT 'queued',
    progress INT NOT NULL DEFAULT 0,
    current_step TEXT,
    reserved_credits INTEGER NOT NULL DEFAULT 0,
    settled_credits INTEGER NOT NULL DEFAULT 0,
    product_path TEXT,
    background_path TEXT,
    cover_path TEXT,
    audio_path TEXT,
    settings JSONB,
    output_file TEXT,
    output_size BIGINT,
    output_files JSONB,
    log JSONB,
    result JSONB,
    error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION,
    finished_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS v3_users (
    user_id TEXT PRIMARY KEY,
    api_key_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    display_name TEXT,
    monthly_quota INT NOT NULL DEFAULT 100,
    monthly_used INT NOT NULL DEFAULT 0,
    api_key_prefix TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    last_seen_at DOUBLE PRECISION,
    last_reset_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_v3_users_role ON v3_users(role);
CREATE INDEX IF NOT EXISTS idx_v3_jobs_user ON v3_jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_v3_jobs_status ON v3_jobs(status);
"""

# Idempotent migrations — applied in order on every startup
SCHEMA_MIGRATIONS = [
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS tc TEXT DEFAULT 'tc01'",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS progress INT DEFAULT 0",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS current_step TEXT",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS priority INT DEFAULT 0",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS max_retries INT DEFAULT 0",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS retry_count INT DEFAULT 0",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS heartbeat_at DOUBLE PRECISION",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS cover_path TEXT",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS audio_path TEXT",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS output_files JSONB",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS log JSONB",
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS result JSONB",
    "CREATE INDEX IF NOT EXISTS idx_v3_jobs_tc ON v3_jobs(tc, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_v3_jobs_priority_status ON v3_jobs(priority DESC, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_v3_jobs_user ON v3_jobs(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_v3_jobs_status ON v3_jobs(status)",
    # END-USER PORTAL (FIX 2026-08-19): email + password auth
    "ALTER TABLE v3_users ADD COLUMN IF NOT EXISTS email TEXT",
    "ALTER TABLE v3_users ADD COLUMN IF NOT EXISTS password_hash TEXT",
    "ALTER TABLE v3_users ADD COLUMN IF NOT EXISTS last_login_at DOUBLE PRECISION",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_v3_users_email ON v3_users(lower(email)) WHERE email IS NOT NULL",
    # USER TIERS (FIX 2026-08-19): free / pro / enterprise
    "ALTER TABLE v3_users ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'free'",
    "ALTER TABLE v3_users ADD COLUMN IF NOT EXISTS monthly_quota_paid INT DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_v3_users_tier ON v3_users(tier)",
    # JOB PRIORITY COLUMN
    "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS priority INT DEFAULT 0",
]


def init_schema() -> None:
    """Create gateway tables + apply migrations (idempotent)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_BASE)
            for mig in SCHEMA_MIGRATIONS:
                cur.execute(mig)
        conn.commit()
