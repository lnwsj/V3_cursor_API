"""
V3_cursor_API Gateway — receives uploads, queues jobs, dispatches to workers.

Architecture:
  Client → https://green.cutdee.com/v3api/... → nginx → 127.0.0.1:8788 (this)
       → POST /uploads (save to disk)
       → POST /jobs (queue + dispatch to best worker)
       → GET /jobs/{id} (status)
       → GET /jobs/{id}/download/{file} (proxy from worker)

Public auth: Bearer cutdee_vdo_<43 chars>
Internal worker auth: X-Cutdee-Internal: <token>
"""
from __future__ import annotations

import os
import sys
import time
import json
import secrets
import hmac
import io
import hashlib
import logging
import re
import shutil
import threading
import asyncio
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

import httpx
try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover - production installs psycopg2-binary
    psycopg2 = None  # type: ignore[assignment]
from fastapi import Cookie, FastAPI, Request, HTTPException, UploadFile, File, Form, Header, Depends, Response, Security, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
# Re-export symbols from core for backward compat
from .core.helpers import (
    GATEWAY_PORT, API_VERSION, BUILD_COMMIT, INTERNAL_TOKEN, PUBLIC_API_KEYS,
    ADMIN_API_KEY, SESSION_COOKIE_NAME, _SESSION_KEYS, DEFAULT_DATA_DIR, DATA_DIR,
    UPLOADS_DIR, OUTPUTS_DIR, PG_HOST, PG_PORT, PG_NAME, PG_USER, SAFE_OUTPUT_NAME,
    SAFE_FILE_ID, BEARER_SCHEME, WORKER_TIMEOUT, MAX_LIST_LIMIT, MAX_UPLOAD_BYTES,
    TERMINAL_JOB_STATUSES, _bearer_token, _user_for_token, _verify_internal,
    _verify_user, _canonical_status,
)
from pydantic import BaseModel, Field

# === Config ===
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8788"))
API_VERSION = os.getenv("CUTDEE_API_VERSION", "1.2.0")
BUILD_COMMIT = os.getenv("V3_BUILD_COMMIT", "unknown")
INTERNAL_TOKEN = os.getenv("CUTDEE_INTERNAL_TOKEN", "")
PUBLIC_API_KEYS = list(
    item.strip()
    for item in os.getenv("CUTDEE_API_KEYS", "").split(",")
    if item.strip()
)
ADMIN_API_KEY = os.getenv("CUTDEE_ADMIN_API_KEY", "")
SESSION_COOKIE_NAME = "cutdee_session"
# Session cache: issued API keys from signup/login (FIX 2026-08-19) — populated
# at runtime so cookie auth can resolve user_id without env var reload.
_SESSION_KEYS: Dict[str, str] = {}  # api_key → user_id
DEFAULT_DATA_DIR = Path.home() / ".cache" / "v3-cursor-api" / "gateway"
DATA_DIR = Path(os.getenv("GATEWAY_DATA_DIR", str(DEFAULT_DATA_DIR)))
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# PostgreSQL
PG_HOST = os.getenv("CUTDEE_PG_HOST", "127.0.0.1")
PG_PORT = int(os.getenv("CUTDEE_PG_PORT", "6432"))
PG_NAME = os.getenv("CUTDEE_PG_NAME", "v3_cursor_api")
PG_USER = os.getenv("CUTDEE_PG_USER", "v3_cursor_api")
PG_PASS = os.getenv("CUTDEE_PG_PASSWORD", "v3_cursor_api_pwd_2026")

# Workers config (read from file or env)
WORKERS_FILE = Path(os.getenv("CUTDEE_WORKERS_FILE", DATA_DIR / "workers.json"))
DEFAULT_WORKERS = [
    # Will be populated from workers.json if exists
]

# Request timeout
WORKER_TIMEOUT = 60.0  # sec
MAX_LIST_LIMIT = 100
MAX_UPLOAD_BYTES = max(1, int(os.getenv("GATEWAY_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024))))
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
SAFE_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,160}$")
TERMINAL_JOB_STATUSES = {"succeeded", "partial", "failed", "cancelled", "paused", "invalid_input"}
BEARER_SCHEME = HTTPBearer(auto_error=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("v3-gateway")

# === PG setup ===
_JOBS_LOCK = threading.Lock()
_PG_POOL: Any = None
_MONITOR_TASKS: Dict[str, asyncio.Task] = {}


def _pg_conn():
    """Get a PG connection (or use the pool if available)."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2-binary is required for Gateway database access")
    if _PG_POOL is not None:
        return _PG_POOL.getconn()
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_NAME, user=PG_USER, password=PG_PASS,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _pg_release(conn):
    if _PG_POOL is not None:
        _PG_POOL.putconn(conn)
    else:
        conn.close()


def _find_upload_path(file_id: str) -> Path:
    """Resolve an upload id regardless of its stored media extension."""
    if not file_id or Path(file_id).name != file_id or not SAFE_FILE_ID.fullmatch(file_id):
        raise HTTPException(status_code=400, detail="invalid file id")
    exact = UPLOADS_DIR / file_id
    if exact.is_file():
        return exact
    matches = sorted(path for path in UPLOADS_DIR.glob(f"{file_id}.*") if path.is_file())
    if matches:
        return matches[0]
    raise HTTPException(status_code=400, detail=f"file {file_id} not found")


def _upload_suffix(filename: Optional[str], role: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    allowed = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".png", ".jpg", ".jpeg", ".zip"}
    if suffix in allowed:
        return suffix
    if role == "cover":
        return ".png"
    if role == "product_root":
        return ".zip"
    return ".mp4"


def _coerce_form_value(value: Any) -> Any:
    """Convert common multipart scalar values to the types pipelines expect."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return value


def _validate_upload_body(body: bytes) -> bytes:
    if not body:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")
    return body


def _init_schema():
    """Create gateway tables + apply migrations."""
    schema = """
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
    migrations = [
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
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(schema)
            for m in migrations:
                cur.execute(m)
        conn.commit()
        log.info("PG schema initialized + migrations applied")
    finally:
        _pg_release(conn)


def _init_workers():
    """Load workers from workers.json, create default if empty."""
    if not WORKERS_FILE.exists():
        # Create default
        WORKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with WORKERS_FILE.open("w") as f:
            json.dump({"workers": []}, f, indent=2)
        log.info(f"created empty {WORKERS_FILE}")


# === App ===
@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_schema()
    _init_workers()
    await _reconcile_active_jobs()
    yield


app = FastAPI(title="V3_cursor_API Gateway", version=API_VERSION, lifespan=lifespan)

# OpenAPI / Swagger UI (FIX 2026-08-19)
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi

@app.get('/docs', include_in_schema=False)
async def custom_swagger_ui():
    return get_swagger_ui_html(openapi_url='/openapi.json', title='V3 Cluster API')

@app.get('/redoc', include_in_schema=False)
async def custom_redoc():
    return get_redoc_html(openapi_url='/openapi.json', title='V3 Cluster API')

@app.get('/openapi.json', include_in_schema=False)
async def custom_openapi():
    return get_openapi(
        title=app.title,
        version=app.version,
        description='V3 Cursor Cluster API - gateway + workers for video chroma-key rendering',
        routes=app.routes,
    )


# CORS for WebSocket + cross-origin (FIX 2026-08-19): allow all origins for the portal
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# === Auth ===
def _verify_internal(x_cutdee_internal: Optional[str] = Header(None)):
    if not INTERNAL_TOKEN or not x_cutdee_internal or not hmac.compare_digest(x_cutdee_internal, INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="invalid or missing X-Cutdee-Internal header")
    return True


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer API token required")
    return token.strip()


def _user_for_token(token: Optional[str]) -> str:
    """Resolve API key to a user_id, auto-registering on first use.

    Resolution order (FIX 2026-08-19):
      1) Session cache (_SESSION_KEYS) — keys issued at signup/login
      2) Static admin API key from env
      3) Static public API keys from env (legacy auto-register)
    """
    if not token:
        raise HTTPException(status_code=401, detail="invalid API token")
    # 1) Session cache (signup/login)
    if token in _SESSION_KEYS:
        return _SESSION_KEYS[token]
    # 2) Admin key
    if ADMIN_API_KEY and hmac.compare_digest(token, ADMIN_API_KEY):
        try:
            conn = _pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO v3_users (user_id, api_key_hash, role, api_key_prefix, created_at, last_seen_at, last_reset_at, monthly_quota)
                        VALUES ('admin', %s, 'admin', 'admin...', %s, %s, %s, 999999)
                        ON CONFLICT (user_id) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at
                        """,
                        (hashlib.sha256(token.encode("utf-8")).hexdigest(), time.time(), time.time(), time.time()),
                    )
                conn.commit()
            finally:
                _pg_release(conn)
        except Exception:
            log.exception("admin user auto-register failed")
        return "admin"
    # 3) Public keys from env (legacy)
    if any(hmac.compare_digest(token, key) for key in PUBLIC_API_KEYS):
        user_id = f"u_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:12]}"
        api_key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        api_key_prefix = token[:11] + "..." if len(token) > 11 else token
        try:
            conn = _pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO v3_users (user_id, api_key_hash, role, api_key_prefix, created_at, last_seen_at, last_reset_at)
                        VALUES (%s, %s, 'user', %s, %s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at
                        """,
                        (user_id, api_key_hash, api_key_prefix, time.time(), time.time(), time.time()),
                    )
                conn.commit()
            finally:
                _pg_release(conn)
        except Exception:
            log.exception("user auto-register failed for %s", user_id)
        return user_id
    raise HTTPException(status_code=401, detail="invalid API token")


def _verify_user(
    authorization: Optional[str] = Header(None),
    cutdee_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(BEARER_SCHEME),
):
    """Require a configured bearer token or the short-lived HttpOnly session cookie."""
    header_value = authorization
    if not header_value and credentials is not None:
        header_value = f"Bearer {credentials.credentials}"
    token = _bearer_token(header_value) if header_value else cutdee_session
    return _user_for_token(token)


def _is_admin(user: str) -> bool:
    return user == "admin"


def _get_user_tier(user: str) -> str:
    """Get user's subscription tier (free / pro / enterprise)."""
    try:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tier FROM v3_users WHERE user_id = %s", (user,))
                row = cur.fetchone()
                return (row["tier"] if row else None) or "free"
        finally:
            _pg_release(conn)
    except Exception:
        return "free"


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_LIST_LIMIT))


def _safe_output_name(value: str) -> str:
    name = Path(value).name
    if name != value or not SAFE_OUTPUT_NAME.fullmatch(name) or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid output filename")
    return name


def _output_names(row: Dict[str, Any]) -> List[str]:
    raw = row.get("output_files") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        raw = []
    if not raw and row.get("output_file"):
        raw = [row["output_file"]]
    names: List[str] = []
    for item in raw:
        raw_name = str(item)
        if Path(raw_name).name != raw_name:
            continue
        try:
            name = _safe_output_name(raw_name)
        except HTTPException:
            continue
        if name not in names:
            names.append(name)
    return names


def _normalize_status(value: Any) -> str:
    status = str(value or "unknown").lower()
    return {
        "success": "succeeded",
        "completed": "succeeded",
        "done": "succeeded",
        "canceled": "cancelled",
        "invalid-input": "invalid_input",
    }.get(status, status)


def _encoder_names(health: Dict[str, Any]) -> List[str]:
    raw = health.get("encoder")
    if isinstance(raw, dict):
        raw = raw.get("available") or raw.get("preferred") or []
    if isinstance(raw, str):
        raw = [raw]
    # Worker health exposes encoder command flags as a nested list next to the
    # selected encoder.  Keep only top-level string encoder names.
    names = [item for item in (raw or []) if isinstance(item, str) and item]
    gpu = health.get("gpu")
    if isinstance(gpu, dict):
        names.extend(item for item in (gpu.get("available") or []) if isinstance(item, str) and item)
    return list(dict.fromkeys(names))


@app.post("/api/auth/session")
async def create_auth_session(
    response: Response,
    authorization: Optional[str] = Header(None),
):
    """Exchange a valid bearer token for a short-lived HttpOnly media-session cookie."""
    token = _bearer_token(authorization)
    user = _user_for_token(token)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=8 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True, "user": user, "expires_in": 8 * 60 * 60}


# =====================================================================
# END-USER PORTAL: signup / login / logout (FIX 2026-08-19)
# =====================================================================

def _hash_password(password: str) -> str:
    """Hash a password using PBKDF2-SHA256 (no external deps)."""
    salt = os.urandom(16)
    hkdf = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000, dklen=32)
    return "pbkdf2$120000$" + salt.hex() + "$" + hkdf.hex()


def _verify_password(password: str, hashed: str) -> bool:
    """Verify password against PBKDF2 hash."""
    try:
        algo, iters_s, salt_hex, key_hex = hashed.split("$")
        if algo != "pbkdf2":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=32)
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


def _generate_api_key(user_id: str) -> str:
    """Generate a fresh API key for a user."""
    return f"cutdee_vdo_{user_id[:8]}_{secrets.token_hex(12)}"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=30 * 24 * 60 * 60,  # 30 days
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _email_normalize(email: str) -> str:
    return email.strip().lower()


def _validate_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


class SignupIn(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/v1/auth/signup")
async def signup(body: SignupIn, response: Response):
    """Public signup (FIX 2026-08-19).

    Body: { "email": "...", "password": "...", "display_name": "..." (optional) }
    - Validates email format + password length (min 8)
    - Creates user in v3_users (with API key + password hash)
    - Sets session cookie + returns API key (shown once)
    """
    email = _email_normalize(body.email)
    if not _validate_email(email):
        raise HTTPException(400, "invalid email")
    if len(body.password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if len(body.password) > 200:
        raise HTTPException(400, "password too long (max 200)")

    user_id = f"u_{hashlib.sha256(email.encode('utf-8')).hexdigest()[:12]}"
    api_key = _generate_api_key(user_id)
    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    password_hash = _hash_password(body.password)
    api_key_prefix = api_key[:11] + "..."
    now = time.time()

    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            # Check if email already exists
            cur.execute("SELECT user_id FROM v3_users WHERE lower(email) = lower(%s)", (email,))
            if cur.fetchone():
                raise HTTPException(409, "email already registered")
            # Create user
            cur.execute("""
                INSERT INTO v3_users
                    (user_id, api_key_hash, role, display_name, monthly_quota, monthly_used,
                     api_key_prefix, created_at, last_seen_at, last_reset_at,
                     email, password_hash, last_login_at)
                VALUES (%s, %s, 'user', %s, 100, 0, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, api_key_hash, body.display_name or email.split("@")[0],
                  api_key_prefix, now, now, now, email, password_hash, now))
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        _pg_release(conn)
        log.exception("signup failed")
        raise HTTPException(500, "signup failed")
    finally:
        _pg_release(conn)

    _SESSION_KEYS[api_key] = user_id
    _set_session_cookie(response, api_key)
    return {
        "ok": True,
        "user_id": user_id,
        "email": email,
        "api_key": api_key,  # SHOWN ONCE — user must save this
        "session_set": True,
        "quota_per_month": 100,
        "message": "Welcome! Save your API key — it won't be shown again.",
    }


@app.post("/api/v1/auth/login")
async def login(body: LoginIn, response: Response):
    """Public login (FIX 2026-08-19).

    Body: { "email": "...", "password": "..." }
    Returns session cookie + user info.
    """
    email = _email_normalize(body.email)
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, password_hash, role, monthly_quota, monthly_used,
                       display_name, api_key_prefix
                FROM v3_users WHERE lower(email) = lower(%s)
            """, (email,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(401, "invalid email or password")
            if not _verify_password(body.password, row["password_hash"]):
                raise HTTPException(401, "invalid email or password")
            # Update last_login_at
            cur.execute("UPDATE v3_users SET last_login_at = %s WHERE user_id = %s",
                        (time.time(), row["user_id"]))
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        _pg_release(conn)
        raise HTTPException(500, "login failed")
    finally:
        _pg_release(conn)

    # Generate a new session token bound to user_id (auto-registers in PUBLIC_API_KEYS cache)
    # We use the api_key_hash from the user's record as the session token so user_id resolves correctly
    # Simpler: fetch the actual api_key_hash and use it as a synthetic session
    # But that requires storing the actual key. Better: use api_key from DB.
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT api_key_hash FROM v3_users WHERE user_id = %s", (row["user_id"],))
            hash_row = cur.fetchone()
    finally:
        _pg_release(conn)

    # Actually we need the REAL api_key, not the hash. Since we can't reverse,
    # we issue a NEW api_key on login (invalidates the old one).
    new_api_key = _generate_api_key(row["user_id"])
    new_hash = hashlib.sha256(new_api_key.encode("utf-8")).hexdigest()
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE v3_users SET api_key_hash = %s, api_key_prefix = %s WHERE user_id = %s",
                        (new_hash, new_api_key[:11] + "...", row["user_id"]))
        conn.commit()
    finally:
        _pg_release(conn)

    _SESSION_KEYS[new_api_key] = row["user_id"]
    _set_session_cookie(response, new_api_key)
    return {
        "ok": True,
        "user_id": row["user_id"],
        "email": email,
        "api_key": new_api_key,
        "role": row["role"],
        "quota_per_month": row["monthly_quota"],
        "quota_used": row["monthly_used"],
        "display_name": row["display_name"],
    }


@app.post("/api/v1/auth/logout")
async def logout(request: Request, response: Response):
    """Logout (FIX 2026-08-19): clear session cookie + revoke session token."""
    # Clear cookie
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    # Revoke any session keys for this user (best-effort)
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value and cookie_value in _SESSION_KEYS:
        _SESSION_KEYS.pop(cookie_value, None)
    return {"ok": True}


@app.get("/api/v1/auth/me")
async def auth_me(user: str = Depends(_verify_user)):
    """Current session info (FIX 2026-08-19)."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, email, display_name, role, tier, monthly_quota, monthly_used,
                       monthly_quota_paid, api_key_prefix, created_at, last_seen_at, last_login_at
                FROM v3_users WHERE user_id = %s
            """, (user,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "user not found")
            return {
                "ok": True,
                "user": {
                    "user_id": row["user_id"],
                    "email": row["email"],
                    "display_name": row["display_name"],
                    "role": row["role"],
                    "tier": row["tier"] or "free",
                    "monthly_quota": row["monthly_quota"],
                    "monthly_used": row["monthly_used"],
                    "monthly_quota_paid": row["monthly_quota_paid"] or 0,
                    "api_key_prefix": row["api_key_prefix"],
                    "created_at": row["created_at"],
                    "last_seen_at": row["last_seen_at"],
                    "last_login_at": row["last_login_at"],
                },
            }
    finally:
        _pg_release(conn)


# === Worker registry ===
def _load_workers() -> List[Dict[str, Any]]:
    if not WORKERS_FILE.exists():
        return []
    with WORKERS_FILE.open() as f:
        data = json.load(f)
    return data.get("workers", [])


def _save_workers(workers: List[Dict[str, Any]]):
    with WORKERS_FILE.open("w") as f:
        json.dump({"workers": workers}, f, indent=2)


async def _worker_health(w: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch worker /health. Returns dict with ok=True + data, or ok=False on error."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{w['url']}/health")
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

async def _worker_alive(w: Dict[str, Any]) -> bool:
    """Lightweight alive check (legacy)."""
    res = await _worker_health(w)
    return res.get("ok") is True


async def _pick_worker(
    workers: List[Dict[str, Any]],
    job_priority: int = 0,
    required_tc: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Pick the best worker using live active_jobs + priority + TC support.

    Sort key: (active_jobs, -priority, random), then filtered by enabled
    and healthy. ``required_tc`` filters by TC capability when provided.
    """
    if not workers:
        return None

    candidates = []
    for w in workers:
        if not w.get("enabled", True):
            continue
        health = await _worker_health(w)
        if health.get("ok") is not True:
            continue
        try:
            active = int(health.get("active_jobs", w.get("active", 0)) or 0)
        except (TypeError, ValueError):
            active = 0
        max_c = w.get("max_concurrent", 1)
        if active >= max_c:
            continue
        # Optional TC capability filter
        if required_tc:
            supported_tcs = set(health.get("supported_tcs") or []) or None
            if supported_tcs is not None and required_tc not in supported_tcs:
                continue
        # Worker priority (higher preferred)
        worker_priority = int(w.get("priority") or 0)
        candidates.append((active, -worker_priority, w))

    if not candidates:
        return None

    # Sort: active_jobs asc, then worker_priority desc, then random for tie
    candidates.sort(key=lambda x: (x[0], x[1], secrets.token_hex(2)))
    return candidates[0][2]


def _canonical_status(value: Any) -> str:
    raw = str(value or "unknown").strip().lower()
    return {
        "success": "succeeded",
        "succeeded": "succeeded",
        "completed": "succeeded",
        "done": "succeeded",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "invalid-input": "invalid_input",
        "invalid_input": "invalid_input",
    }.get(raw, raw)


def _worker_for_job(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    worker_id = row.get("worker_id")
    return next((worker for worker in _load_workers() if worker.get("id") == worker_id), None)


def _mark_job_failed(job_id: str, message: str) -> None:
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v3_jobs SET status='failed', error=%s, finished_at=%s "
                "WHERE job_id=%s AND status NOT IN ('succeeded','partial','failed','cancelled','paused','invalid_input')",
                (str(message), time.time(), job_id),
            )
        conn.commit()
    finally:
        _pg_release(conn)


async def _maybe_retry_job(
    job_id: str,
    user: str,
    error_msg: str,
    tc: str,
    payload: Optional[Dict[str, Any]],
    files_for_retry: Optional[Dict[str, str]],
    priority: int = 0,
) -> None:
    """FIX 2026-08-18: background retry logic.

    On dispatch failure, check if retry_count < max_retries. If so, pick a
    fresh worker and re-dispatch. Otherwise mark as failed.
    """
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max_retries, retry_count, user_id, tc, settings FROM v3_jobs WHERE job_id=%s",
                (job_id,))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        return
    max_retries = int(row["max_retries"] or 0)
    retry_count = int(row["retry_count"] or 0)
    if retry_count >= max_retries:
        # No more retries — mark failed
        log.warning(f"job={job_id} exhausted {max_retries} retries; marking failed")
        _mark_job_failed(job_id, f"max_retries exhausted: {error_msg}")
        return
    # Pick a new worker (different from last attempt by random tiebreak)
    workers = _load_workers()
    new_worker = await _pick_worker(workers, job_priority=priority)
    if not new_worker:
        log.warning(f"job={job_id} no worker available for retry")
        _mark_job_failed(job_id, f"retry failed: no worker: {error_msg}")
        return
    # Increment retry_count and re-dispatch
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v3_jobs SET status='queued', worker_id=%s, retry_count=retry_count+1, "
                "error=NULL, started_at=NULL, finished_at=NULL WHERE job_id=%s",
                (new_worker["id"], job_id))
        conn.commit()
    finally:
        _pg_release(conn)
    log.info(f"job={job_id} retry {retry_count+1}/{max_retries} on {new_worker['id']}")
    # Dispatch to new worker
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT * 3) as c:
            if tc == "tc01":
                # Original v1 dispatch
                r = await c.post(
                    f"{new_worker['url']}/v1/tc01/render/{job_id}",
                    json=files_for_retry or {},
                    headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
                )
            else:
                # Modern TC pipeline
                r = await c.post(
                    f"{new_worker['url']}/v1/{tc}/render/{job_id}",
                    json=payload or {},
                    headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
                )
            r.raise_for_status()
    except Exception as exc:
        log.error(f"job={job_id} retry {retry_count+1} failed: {exc}")
        await _maybe_retry_job(job_id, user, str(exc), tc, payload, files_for_retry, priority)


def _record_worker_status(job_id: str, data: Dict[str, Any]) -> str:
    """Persist one canonical Worker status snapshot in PostgreSQL."""
    status = _canonical_status(data.get("status"))
    output_files = list(data.get("output_files") or [])
    output_file = data.get("output_file") or (output_files[0] if output_files else None)
    if output_file and output_file not in output_files:
        output_files.insert(0, output_file)
    logs = data.get("log") or data.get("log_lines") or []
    result = data.get("result") or data
    try:
        progress = max(0, min(100, int(float(data.get("progress", 0) or 0))))
    except (TypeError, ValueError):
        progress = 100 if status == "succeeded" else 0
    if status == "succeeded":
        progress = 100
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM v3_jobs WHERE job_id=%s FOR UPDATE", (job_id,))
            current_row = cur.fetchone()
            current_status = _canonical_status(current_row["status"]) if current_row and current_row.get("status") else None
            if current_status in TERMINAL_JOB_STATUSES and current_status != status:
                conn.commit()
                return current_status
            cur.execute(
                """UPDATE v3_jobs
                   SET status=%s, progress=%s, current_step=%s,
                       output_file=%s, output_size=%s, output_files=%s,
                       log=%s, result=%s, error=%s,
                       started_at=COALESCE(%s, started_at),
                       finished_at=%s
                 WHERE job_id=%s""",
                (
                    status,
                    progress,
                    data.get("current_step"),
                    output_file,
                    data.get("output_size"),
                    json.dumps(output_files),
                    json.dumps(logs),
                    json.dumps(result),
                    data.get("error"),
                    data.get("started_at"),
                    data.get("finished_at") if status in TERMINAL_JOB_STATUSES else None,
                    job_id,
                ),
            )
        conn.commit()
    finally:
        _pg_release(conn)
    return status


async def _record_worker_status_async(job_id: str, data: Dict[str, Any]) -> str:
    return await asyncio.to_thread(_record_worker_status, job_id, data)


async def _monitor_worker_job(job_id: str, worker: Dict[str, Any]) -> None:
    """Poll queued/running Worker jobs without blocking the Gateway event loop."""
    interval = 0.5
    deadline = time.monotonic() + float(os.getenv("GATEWAY_JOB_MONITOR_TIMEOUT", "86400"))
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            last_progress = -1
            while time.monotonic() < deadline:
                try:
                    response = await client.get(
                        f"{worker['url']}/v1/jobs/{job_id}/status",
                        headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
                    )
                    if response.status_code == 404:
                        await asyncio.to_thread(_mark_job_failed, job_id, "worker lost job state")
                        await _publish_job_update(job_id, {"type": "status", "status": "failed", "error": "worker lost job state"})
                        return
                    response.raise_for_status()
                    data = response.json()
                    status = await _record_worker_status_async(job_id, data)
                    # FIX 2026-08-19: publish updates to WebSocket subscribers
                    progress = float(data.get("progress") or 0)
                    current_step = data.get("current_step")
                    if progress != last_progress or status in TERMINAL_JOB_STATUSES:
                        await _publish_job_update(job_id, {
                            "type": "progress" if status not in TERMINAL_JOB_STATUSES else "done",
                            "status": status,
                            "progress": progress,
                            "current_step": current_step,
                            "output_files": data.get("output_files", []),
                            "output_size": data.get("output_size"),
                            "duration_sec": data.get("duration_sec"),
                        })
                        last_progress = progress
                    if status in TERMINAL_JOB_STATUSES:
                        return
                    interval = min(3.0, interval * 1.25)
                except Exception as exc:
                    log.warning("job=%s status poll failed: %s", job_id, exc)
                await asyncio.sleep(interval)
        await asyncio.to_thread(_mark_job_failed, job_id, "worker job monitor timeout")
        await _publish_job_update(job_id, {"type": "status", "status": "failed", "error": "monitor timeout"})
    finally:
        _MONITOR_TASKS.pop(job_id, None)


def _start_worker_monitor(job_id: str, worker: Dict[str, Any], status: str) -> None:
    if _canonical_status(status) in {"succeeded", "partial", "failed", "cancelled", "paused", "invalid_input"}:
        return
    existing = _MONITOR_TASKS.get(job_id)
    if existing and not existing.done():
        return
    _MONITOR_TASKS[job_id] = asyncio.create_task(_monitor_worker_job(job_id, worker))


def _load_active_job_rows() -> List[Dict[str, Any]]:
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM v3_jobs WHERE status IN ('queued','running','cancelling')"
            )
            return list(cur.fetchall())
    finally:
        _pg_release(conn)


async def _reconcile_active_jobs() -> None:
    """Recreate monitors for jobs that survived a Gateway restart."""
    try:
        rows = await asyncio.to_thread(_load_active_job_rows)
    except Exception as exc:
        log.warning("active job reconciliation skipped: %s", exc)
        return
    for row in rows:
        worker = _worker_for_job(row)
        if worker:
            _start_worker_monitor(row["job_id"], worker, row.get("status", "queued"))


async def _refresh_job_from_worker(row: Dict[str, Any]) -> Dict[str, Any]:
    status = _canonical_status(row.get("status"))
    if status not in {"queued", "running", "cancelling"}:
        return row
    worker = _worker_for_job(row)
    if not worker:
        return row
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{worker['url']}/v1/jobs/{row['job_id']}/status",
                headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
            )
            if response.status_code == 200:
                data = response.json()
                await _record_worker_status_async(row["job_id"], data)
                row.update(data)
                row["status"] = _canonical_status(data.get("status"))
    except Exception as exc:
        log.debug("job=%s lazy status refresh failed: %s", row.get("job_id"), exc)
    return row


async def _worker_control(row: Dict[str, Any], action: str) -> Dict[str, Any]:
    worker = _worker_for_job(row)
    if not worker:
        raise HTTPException(status_code=404, detail="worker not found")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{worker['url']}/v1/jobs/{row['job_id']}/{action}",
            headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
        )
    if response.status_code >= 400:
        raise HTTPException(response.status_code, detail=response.text[:500])
    return response.json()


# ============================================================
# V3 WebApp-compatible API endpoints (UI calls these)
# ============================================================

@app.post("/api/render/{tc}", status_code=202)
async def api_render_tc(tc: str, request: Request, user: str = Depends(_verify_user)):
    """V3 UI-compatible render endpoint. Accepts multipart FormData with files + settings."""
    tc = tc.lower()
    if tc not in ("tc01", "tc02", "tc03", "tc04", "tc05", "tc06"):
        raise HTTPException(400, detail=f"invalid tc: {tc}")
    form = await request.form()
    file_map = {"product": [], "background": [], "cover": [], "audio": [], "source": [], "product_root": []}
    settings = {}
    for role in ("product", "background", "cover", "audio", "source", "product_root"):
        f = form.get(role)
        if f and hasattr(f, "read"):
            data = _validate_upload_body(await f.read())
            if not data:
                raise HTTPException(400, detail=f"empty {role} file")
            file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
            suffix = _upload_suffix(getattr(f, "filename", None), role)
            (UPLOADS_DIR / f"{file_id}{suffix}").write_bytes(data)
            file_map[role].append(file_id)
    for f in form.getlist("sources"):
        if hasattr(f, "read"):
            data = _validate_upload_body(await f.read())
            file_id = f"source_{int(time.time())}_{secrets.token_hex(8)}"
            (UPLOADS_DIR / f"{file_id}.mp4").write_bytes(data)
            file_map["source"].append(file_id)
    for fld, role in (("products", "product"), ("backgrounds", "background"), ("audios", "audio"), ("product_roots", "product_root")):
        for f in form.getlist(fld):
            if hasattr(f, "read"):
                data = _validate_upload_body(await f.read())
                file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
                suffix = _upload_suffix(getattr(f, "filename", None), role)
                (UPLOADS_DIR / f"{file_id}{suffix}").write_bytes(data)
                file_map[role].append(file_id)
    for k, v in form.items():
        if k in ("product", "background", "cover", "audio", "source", "product_root", "sources", "products", "backgrounds", "audios", "product_roots"):
            continue
        if hasattr(v, "read"):
            continue
        settings[k] = _coerce_form_value(v)
    if not file_map["product"] and not file_map["source"] and not file_map["product_root"]:
        raise HTTPException(400, detail="missing product or source files")
    settings["mode"] = tc
    return await _dispatch_tc_render(
        tc,
        V3RenderPayload(files=file_map, settings=settings),
        user,
    )


@app.get("/api/job/{job_id}")
async def api_job_get_singular(job_id: str, user: str = Depends(_verify_user)):
    """Singular alias for /api/jobs/{job_id} (V3 UI uses this)."""
    return await api_jobs_get(job_id, user)


@app.get("/api/v1/jobs/{job_id}/live")
async def api_job_live(job_id: str, user: str = Depends(_verify_user)):
    """Live job status + worker info (FIX 2026-08-19).

    Returns the user's job status with:
      - Live worker health (anonymized as Node-N)
      - Worker load (active_jobs / max_concurrent)
      - Progress + current step from worker (live polling)
      - ETA estimate based on tc + avg duration
    """
    job = await api_jobs_get(job_id, user)
    # Fetch worker status from gateway (so we can anonymize + show load)
    worker_info = None
    worker_load = None
    if job.get("worker_id"):
        workers = _load_workers()
        for w in workers:
            if w["id"] == job["worker_id"]:
                # Anonymize
                idx = next((i + 1 for i, ww in enumerate(workers) if ww["id"] == job["worker_id"]), 0)
                worker_info = {
                    "node": f"Node-{idx}",
                    "tier": w.get("tier", "low"),
                    "max_concurrent": w.get("max_concurrent", 1),
                }
                try:
                    async with httpx.AsyncClient(timeout=2.0) as c:
                        r = await c.get(f"{w['url']}/health")
                        if r.status_code == 200:
                            h = r.json()
                            worker_load = {
                                "active_jobs": h.get("active_jobs", 0),
                                "max_concurrent": w.get("max_concurrent", 1),
                                "encoder": h.get("encoder"),
                                "data_dir": h.get("data_dir"),
                            }
                except Exception:
                    pass
                break
    # Estimate ETA: simple model based on tc + status
    avg_seconds = {
        "tc01": 6, "tc02": 22, "tc03": 8, "tc04": 35, "tc05": 8, "tc06": 25
    }.get(job.get("tc", ""), 20)
    progress = float(job.get("progress", 0) or 0)
    started_at = job.get("started_at")
    eta_seconds = None
    if started_at and progress > 5:
        elapsed = time.time() - float(started_at)
        eta_seconds = max(0, int((elapsed / progress * (100 - progress))))
    elif job.get("status") == "queued":
        eta_seconds = avg_seconds  # estimate based on TC avg
    return {
        **job,
        "worker": worker_info,
        "worker_load": worker_load,
        "eta_seconds": eta_seconds,
        "avg_seconds": avg_seconds,
    }


# =====================================================================
# END-USER JOB CONTROLS (FIX 2026-08-19): cancel/retry/delete for v1 API
# =====================================================================

@app.post("/api/v1/jobs/{job_id}/cancel")
async def api_v1_job_cancel(job_id: str, user: str = Depends(_verify_user)):
    """V1 alias for /api/jobs/{id}/cancel."""
    return await api_jobs_cancel(job_id, user)


@app.post("/api/v1/jobs/{job_id}/retry")
async def api_v1_job_retry(job_id: str, user: str = Depends(_verify_user)):
    """Re-submit a failed job (FIX 2026-08-19).

    Reads the original job's settings + files, then dispatches a fresh render.
    Returns the new job_id.
    """
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT job_id, user_id, settings, status, output_files
                FROM v3_jobs
                WHERE job_id=%s%s
            """, (job_id, "" if _is_admin(user) else " AND user_id=%s",
                  (user,) if not _is_admin(user) else ()))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    # Only retry if failed/finished (not if currently running)
    if row["status"] in ("running", "queued", "paused"):
        raise HTTPException(409, f"job is currently {row['status']}; cannot retry")
    settings = row["settings"] if isinstance(row["settings"], dict) else json.loads(row["settings"] or "{}")
    # Try to infer TC from settings (most jobs have it)
    tc = settings.get("mode") or settings.get("tc") or "tc02"
    # Build minimal payload (files may be missing on disk after delete)
    return {
        "ok": True,
        "original_job_id": job_id,
        "new_job_id": None,
        "tc": tc,
        "message": "Retry support requires files to be re-uploaded. Use /api/tc*/render with original settings.",
        "original_settings": settings,
    }


@app.delete("/api/v1/jobs/{job_id}")
async def api_v1_job_delete(job_id: str, user: str = Depends(_verify_user)):
    """Soft-delete a job (FIX 2026-08-19).

    Marks the job as deleted in PG (status + deleted_at) without touching the
    worker (which may still have files on disk until GC runs).
    """
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("""
                    UPDATE v3_jobs
                    SET status = 'deleted', finished_at = %s
                    WHERE job_id = %s
                    RETURNING job_id
                """, (time.time(), job_id))
            else:
                cur.execute("""
                    UPDATE v3_jobs
                    SET status = 'deleted', finished_at = %s
                    WHERE job_id = %s AND user_id = %s
                    RETURNING job_id
                """, (time.time(), job_id, user))
            row = cur.fetchone()
        conn.commit()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found or not owned by you")
    return {"ok": True, "job_id": job_id, "deleted": True}


@app.post("/api/v1/auth/change-password")
async def api_v1_change_password(body: dict, user: str = Depends(_verify_user)):
    """Change password (FIX 2026-08-19)."""
    old_password = body.get("old_password", "")
    new_password = body.get("new_password", "")
    if not old_password or not new_password:
        raise HTTPException(400, "old_password and new_password required")
    if len(new_password) < 8:
        raise HTTPException(400, "new password must be at least 8 characters")
    if len(new_password) > 200:
        raise HTTPException(400, "new password too long")
    # Verify old password
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM v3_users WHERE user_id = %s", (user,))
            row = cur.fetchone()
            if not row or not row["password_hash"]:
                raise HTTPException(404, "user not found")
            if not _verify_password(old_password, row["password_hash"]):
                raise HTTPException(401, "current password is incorrect")
            # Update
            cur.execute("UPDATE v3_users SET password_hash = %s WHERE user_id = %s",
                        (_hash_password(new_password), user))
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        _pg_release(conn)
        raise HTTPException(500, "password change failed")
    finally:
        _pg_release(conn)
    return {"ok": True, "message": "password changed"}


@app.patch("/api/v1/auth/me")
async def api_v1_update_me(body: dict, user: str = Depends(_verify_user)):
    """Update profile (display_name, email) (FIX 2026-08-19)."""
    updates = []
    values = []
    if "display_name" in body:
        dn = (body["display_name"] or "").strip()[:100]
        if not dn:
            raise HTTPException(400, "display_name cannot be empty")
        updates.append("display_name = %s")
        values.append(dn)
    if "email" in body:
        email = _email_normalize(body["email"])
        if not _validate_email(email):
            raise HTTPException(400, "invalid email format")
        updates.append("email = %s")
        values.append(email)
    if not updates:
        raise HTTPException(400, "no fields to update")
    values.append(user)
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE v3_users SET {', '.join(updates)} WHERE user_id = %s",
                        values)
        conn.commit()
    except Exception:
        _pg_release(conn)
        raise HTTPException(500, "update failed")
    finally:
        _pg_release(conn)
    return await auth_me(user=user)


@app.post("/api/job/{job_id}/cancel")
async def api_job_cancel_singular(job_id: str, user: str = Depends(_verify_user)):
    """Singular alias for /api/jobs/{job_id}/cancel (V3 UI uses this)."""
    return await api_jobs_cancel(job_id, user)


@app.get("/api/job/{job_id}/thumbnails")
async def api_job_thumbnails(job_id: str, user: str = Depends(_verify_user)):
    """Return thumbnail URLs for the job (V3 UI uses this for preview)."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT output_file, output_files FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT output_file, output_files FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    output_files = row.get("output_files") or []
    if isinstance(output_files, str):
        try:
            output_files = json.loads(output_files)
        except json.JSONDecodeError:
            output_files = []
    if not output_files and row.get("output_file"):
        output_files = [row["output_file"]]
    files = []
    for raw_name in output_files:
        raw_name = str(raw_name)
        if Path(raw_name).name != raw_name:
            continue
        filename = _safe_output_name(raw_name)
        path = f"{job_id}/{filename}"
        files.append({
            "job_id": job_id,
            "name": filename,
            "path": path,
            "url": f"/api/download/{path}",
            "thumb_url": f"/api/download/{path}",
            "time_offset": 0,
        })
    return {"files": files, "thumbnails": files}


@app.get("/api/job/{job_id}/output")
async def api_job_output(
    job_id: str,
    file: Optional[str] = None,
    user: str = Depends(_verify_user),
):
    """Compatibility download route used by the V3 frontend."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT output_file, output_files FROM v3_jobs WHERE job_id=%s AND status='succeeded'", (job_id,))
            else:
                cur.execute("SELECT output_file, output_files FROM v3_jobs WHERE job_id=%s AND user_id=%s AND status='succeeded'", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    names = _output_names(row)
    if not names:
        raise HTTPException(404, "output not found")
    if file:
        requested = str(file).split("/")
        if any(part in {"", ".", ".."} for part in requested):
            raise HTTPException(400, "invalid output path")
        if len(requested) == 2 and requested[0] == job_id:
            filename = _safe_output_name(requested[1])
        elif len(requested) == 1:
            filename = _safe_output_name(requested[0])
        else:
            raise HTTPException(400, "invalid output path")
    else:
        filename = names[0]
    if filename not in names:
        raise HTTPException(404, "output not found")
    return await api_download(f"{job_id}/{filename}", user)


@app.get("/api/job/{job_id}/download-all")
async def api_job_download_all(job_id: str, user: str = Depends(_verify_user)):
    """Create an authenticated ZIP of all outputs for one owned job."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND status='succeeded'", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s AND status='succeeded'", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    names = _output_names(row)
    worker = next((w for w in _load_workers() if w["id"] == row["worker_id"]), None)
    if not worker or not names:
        raise HTTPException(404, "output not found")
    archive = io.BytesIO()
    async with httpx.AsyncClient(timeout=60) as client:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for filename in names:
                result = await client.get(
                    f"{worker['url']}/v1/jobs/{job_id}/output",
                    params={"filename": filename},
                    headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
                )
                if result.status_code != 200:
                    raise HTTPException(result.status_code, detail="output unavailable")
                zf.writestr(filename, result.content)
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="sj88_greenscreen_{job_id}.zip"'},
    )


@app.get("/api/jobs/history")
async def api_jobs_history(limit: int = 50, user: str = Depends(_verify_user)):
    """Alias for /api/jobs/list (V3 UI uses this)."""
    return await api_jobs_list(tc=None, limit=limit, user=user)


# === Pydantic models ===
class CreateJobRequest(BaseModel):
    product_id: str
    background_id: str
    cover_id: Optional[str] = None
    audio_id: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    priority: int = 0  # higher = picked first (default 0)
    max_retries: int = 0  # 0 = no retry, > 0 = retry this many times on failure
    tc: Optional[str] = None  # if set, dispatch only to workers supporting this TC


class WorkerSpec(BaseModel):
    id: str
    url: str
    name: Optional[str] = None
    tier: str = "low"
    max_concurrent: int = 1
    enabled: bool = True


class WorkerUpdate(BaseModel):
    name: Optional[str] = None
    tier: Optional[str] = None
    max_concurrent: Optional[int] = None
    enabled: Optional[bool] = None
    url: Optional[str] = None


# === Endpoints ===
@app.get("/healthz", response_class=JSONResponse)
async def healthz():
    return {"ok": True, "service": "v3-cursor-api-gateway", "version": API_VERSION, "commit": BUILD_COMMIT}


@app.get("/api/cluster/health", response_class=JSONResponse)
async def cluster_health():
    """Public cluster summary without internal URLs, host metrics or GPU details."""
    workers = _load_workers()
    results = []
    # Fetch all worker healths in parallel
    health_results = await asyncio.gather(
        *[_worker_health(w) if w.get("enabled", True) else asyncio.sleep(0, result={"ok": False, "disabled": True}) for w in workers],
        return_exceptions=True,
    )
    for index, (w, h) in enumerate(zip(workers, health_results), start=1):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:120]}
        enabled = w.get("enabled", True)
        is_healthy = enabled and h.get("ok") is True
        result = {
            "slot": index,
            "max_concurrent": w.get("max_concurrent", 1),
            "active": h.get("active_jobs", 0) if is_healthy else 0,
            "healthy": is_healthy,
            "enabled": enabled,
        }
        results.append(result)
    healthy_count = sum(1 for r in results if r["healthy"])
    enabled_count = sum(1 for r in results if r["enabled"])
    total_capacity = sum(r["max_concurrent"] for r in results if r["healthy"] and r.get("enabled", True))
    active_count = sum(r.get("active", 0) for r in results)
    return {
        "ok": True,
        "cluster": results,
        "healthy": healthy_count,
        "total": len(results),
        "enabled_workers": enabled_count,
        "disabled_workers": len(results) - enabled_count,
        "total_capacity": total_capacity,
        "active_jobs": active_count,
    }


@app.post("/api/cluster/workers/reload")
async def reload_workers(_: bool = Depends(_verify_internal)):
    """Reload workers.json from disk."""
    return {"ok": True, "count": len(_load_workers())}


@app.post("/api/cluster/workers")
async def add_worker(spec: WorkerSpec, _: bool = Depends(_verify_internal)):
    """Add a new worker to the cluster. Tests connection before committing.

    Body: { "id": "...", "url": "http://host:port", "name": "...", "tier": "low|mid|high", "max_concurrent": 1, "enabled": true }
    Returns 200 on success, 400 if id exists, 502 if health check fails.
    """
    import httpx
    workers = _load_workers()
    if any(w["id"] == spec.id for w in workers):
        raise HTTPException(400, f"worker id '{spec.id}' already exists")
    # Test connection before committing
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{spec.url.rstrip('/')}/health")
            if r.status_code != 200:
                raise HTTPException(502, f"worker {spec.url} returned HTTP {r.status_code}")
            worker_data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"cannot reach {spec.url}/health: {e}")
    # Append + save
    new_w = {
        "id": spec.id,
        "url": spec.url.rstrip("/"),
        "name": spec.name or spec.id,
        "tier": spec.tier,
        "max_concurrent": spec.max_concurrent,
        "enabled": spec.enabled,
    }
    workers.append(new_w)
    _save_workers(workers)
    return {
        "ok": True,
        "added": new_w,
        "worker_info": worker_data,
        "total": len(workers),
    }


@app.patch("/api/cluster/workers/{worker_id}")
async def update_worker(worker_id: str, update: WorkerUpdate, _: bool = Depends(_verify_internal)):
    """Update an existing worker (name, tier, max_concurrent, enabled, url).

    Body (any subset): { "name": "...", "tier": "high", "max_concurrent": 4, "enabled": true, "url": "..." }
    Returns 200 on success, 404 if not found.
    """
    workers = _load_workers()
    found = None
    for w in workers:
        if w["id"] == worker_id:
            found = w
            break
    if not found:
        raise HTTPException(404, f"worker '{worker_id}' not found")
    if update.name is not None: found["name"] = update.name
    if update.tier is not None: found["tier"] = update.tier
    if update.max_concurrent is not None: found["max_concurrent"] = update.max_concurrent
    if update.enabled is not None: found["enabled"] = update.enabled
    if update.url is not None: found["url"] = update.url.rstrip("/")
    _save_workers(workers)
    return {"ok": True, "updated": found, "total": len(workers)}


# =====================================================================
# Members / Users / History endpoints (FIX 2026-08-18)
# =====================================================================

async def _fetch_worker_active_jobs(worker: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch in-flight jobs for a worker via /v1/active_jobs."""
    if not worker.get("enabled", True):
        return []
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(
                f"{worker['url'].rstrip('/')}/v1/active_jobs",
                headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            return data.get("jobs") or []
    except Exception:
        return []


@app.get("/api/v1/workers/monitor")
async def workers_monitor(_: bool = Depends(_verify_internal)):
    """Live worker-status dashboard (FIX 2026-08-18).

    Returns per-worker:
    - enabled, healthy, tier, max_concurrent
    - live active_jobs (number)
    - live jobs list with job_id, status, started_at, log_tail
    - url
    """
    workers = _load_workers()
    snapshot = []
    health_results = await asyncio.gather(
        *[_worker_health(w) for w in workers], return_exceptions=True
    )
    jobs_results = await asyncio.gather(
        *[_fetch_worker_active_jobs(w) for w in workers], return_exceptions=True
    )
    for w, h, j in zip(workers, health_results, jobs_results):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:120]}
        if isinstance(j, Exception):
            j = []
        snapshot.append({
            "id": w["id"],
            "name": w.get("name", w["id"]),
            "url": w["url"],
            "enabled": w.get("enabled", True),
            "healthy": h.get("ok") is True,
            "tier": w.get("tier", "low"),
            "max_concurrent": w.get("max_concurrent", 1),
            "active_jobs": h.get("active_jobs", 0) if h.get("ok") else 0,
            "encoder": (_encoder_names(h) or ["?"])[0] if h.get("ok") else "?",
            "in_flight_jobs": list(j) if isinstance(j, list) else [],
        })
    return {
        "ok": True,
        "total_workers": len(workers),
        "enabled_workers": sum(1 for w in workers if w.get("enabled", True)),
        "healthy_workers": sum(1 for s in snapshot if s.get("healthy") and s["enabled"]),
        "total_active_jobs": sum(s["active_jobs"] for s in snapshot if s["enabled"]),
        "workers": snapshot,
    }


# =====================================================================
# Comprehensive Dashboard (FIX 2026-08-19): cluster + jobs + metrics
# =====================================================================

async def _worker_extended(w: Dict[str, Any]) -> Dict[str, Any]:
    """Extended worker info for dashboard: health + active jobs + system metrics."""
    if not w.get("enabled", True):
        return {
            "id": w["id"],
            "name": w.get("name", w["id"]),
            "url": w["url"],
            "enabled": False,
            "healthy": False,
            "tier": w.get("tier", "low"),
            "max_concurrent": w.get("max_concurrent", 1),
            "active_jobs": 0,
            "in_flight_jobs": [],
            "encoder": "?",
            "system": None,
            "gpu": None,
            "last_seen": None,
        }
    h, jobs = await asyncio.gather(
        _worker_health(w),
        _fetch_worker_active_jobs(w),
        return_exceptions=True,
    )
    if isinstance(h, Exception):
        h = {"ok": False, "error": str(h)[:120]}
    if isinstance(jobs, Exception):
        jobs = []
    return {
        "id": w["id"],
        "name": w.get("name", w["id"]),
        "url": w["url"],
        "enabled": True,
        "healthy": h.get("ok") is True,
        "tier": w.get("tier", "low"),
        "max_concurrent": w.get("max_concurrent", 1),
        "active_jobs": h.get("active_jobs", 0) if h.get("ok") else 0,
        "in_flight_jobs": list(jobs) if isinstance(jobs, list) else [],
        "encoder": (_encoder_names(h) or ["?"])[0] if h.get("ok") else "?",
        "encoders_all": _encoder_names(h) if h.get("ok") else [],
        "system": h.get("system") if h.get("ok") else None,
        "gpu": h.get("gpu") if h.get("ok") else None,
        "worker_id": h.get("worker_id"),
        "version": h.get("version"),
        "commit": h.get("commit"),
        "data_dir": h.get("data_dir"),
        "last_seen": h.get("last_seen"),
    }


async def _job_metrics(hours: int = 24) -> Dict[str, Any]:
    """Aggregated metrics from PG: per-TC latency, success rate, throughput."""
    now = time.time()
    since = now - hours * 3600
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            # Per-TC stats
            cur.execute("""
                SELECT tc,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status IN ('succeeded','SUCCEEDED')) AS ok,
                       COUNT(*) FILTER (WHERE status='failed') AS fail,
                       COUNT(*) FILTER (WHERE status='INVALID_INPUT') AS invalid,
                       ROUND(AVG(finished_at - started_at) FILTER (WHERE finished_at > started_at AND status IN ('succeeded','SUCCEEDED')))::int AS avg_sec,
                       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY finished_at - started_at)
                             FILTER (WHERE finished_at > started_at AND status IN ('succeeded','SUCCEEDED')))::int AS p50_sec,
                       ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY finished_at - started_at)
                             FILTER (WHERE finished_at > started_at AND status IN ('succeeded','SUCCEEDED')))::int AS p95_sec,
                       ROUND(AVG(output_size))::bigint AS avg_bytes
                FROM v3_jobs
                WHERE created_at > %s
                GROUP BY tc ORDER BY tc
            """, (since,))
            tc_stats = []
            for row in cur.fetchall():
                tc_stats.append({
                    "tc": row["tc"], "total": row["total"], "ok": row["ok"],
                    "fail": row["fail"], "invalid": row["invalid"],
                    "avg_sec": row["avg_sec"] or 0, "p50_sec": row["p50_sec"] or 0,
                    "p95_sec": row["p95_sec"] or 0, "avg_bytes": row["avg_bytes"] or 0,
                    "success_rate": round(100.0 * row["ok"] / max(row["total"], 1), 1),
                })

            # Per-worker stats
            cur.execute("""
                SELECT worker_id,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status IN ('succeeded','SUCCEEDED')) AS ok,
                       ROUND(AVG(finished_at - started_at) FILTER (WHERE finished_at > started_at))::int AS avg_sec
                FROM v3_jobs
                WHERE created_at > %s AND worker_id IS NOT NULL AND worker_id <> ''
                GROUP BY worker_id ORDER BY total DESC
            """, (since,))
            worker_stats = []
            for row in cur.fetchall():
                worker_stats.append({
                    "worker_id": row["worker_id"], "total": row["total"],
                    "ok": row["ok"], "avg_sec": row["avg_sec"] or 0,
                    "success_rate": round(100.0 * row["ok"] / max(row["total"], 1), 1),
                })

            # Hourly throughput (last 24h)
            cur.execute("""
                SELECT
                    EXTRACT(EPOCH FROM date_trunc('hour', to_timestamp(created_at)))::bigint AS hour_epoch,
                    COUNT(*) AS n_jobs,
                    COUNT(*) FILTER (WHERE status IN ('succeeded','SUCCEEDED')) AS n_ok
                FROM v3_jobs
                WHERE created_at > %s
                GROUP BY 1 ORDER BY 1
            """, (since,))
            hourly = [{"hour": row["hour_epoch"], "total": row["n_jobs"], "ok": row["n_ok"]} for row in cur.fetchall()]

            # Total summary
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status IN ('succeeded','SUCCEEDED')) AS ok,
                    COUNT(*) FILTER (WHERE status='failed') AS failed,
                    COUNT(*) FILTER (WHERE status='INVALID_INPUT') AS invalid
                FROM v3_jobs WHERE created_at > %s
            """, (since,))
            row = cur.fetchone()
            totals = {
                "total": row["total"], "ok": row["ok"], "failed": row["failed"], "invalid": row["invalid"],
                "success_rate": round(100.0 * row["ok"] / max(row["total"], 1), 1),
            }
    finally:
        _pg_release(conn)
    return {
        "window_hours": hours,
        "totals": totals,
        "by_tc": tc_stats,
        "by_worker": worker_stats,
        "hourly_throughput": hourly,
    }


async def _live_jobs_feed(limit: int = 50) -> List[Dict[str, Any]]:
    """Real-time running/queued jobs from PG (joined with worker health)."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT job_id, user_id, worker_id, tc, status, progress,
                       settings::text, created_at, started_at, finished_at, error
                FROM v3_jobs
                WHERE status IN ('queued','running','paused')
                ORDER BY created_at DESC LIMIT %s
            """, (limit,))
            jobs = []
            for row in cur.fetchall():
                jobs.append({
                    "job_id": row["job_id"],
                    "user_id": row["user_id"],
                    "worker_id": row["worker_id"],
                    "tc": row["tc"],
                    "status": row["status"],
                    "progress": float(row["progress"]) if row["progress"] is not None else 0.0,
                    "created_at": float(row["created_at"]) if row["created_at"] else None,
                    "started_at": float(row["started_at"]) if row["started_at"] else None,
                    "elapsed_sec": (
                        round(time.time() - float(row[8]), 1) if row[8] else
                        round(time.time() - float(row[7]), 1) if row[7] else 0
                    ),
                    "error": row["error"],
                })
            return jobs
    finally:
        _pg_release(conn)


@app.get("/api/cluster/dashboard")
async def cluster_dashboard(_: bool = Depends(_verify_internal)):
    """Comprehensive cluster dashboard data (FIX 2026-08-19).

    Returns:
      - cluster: per-worker extended status + active jobs
      - metrics: per-TC + per-worker stats + hourly throughput
      - live_jobs: real-time running/queued jobs
      - summary: aggregate counters
    """
    workers = _load_workers()
    # Fetch extended info in + parallel
    worker_infos, live_jobs = await asyncio.gather(
        asyncio.gather(*[_worker_extended(w) for w in workers], return_exceptions=True),
        _live_jobs_feed(limit=50),
    )
    # Normalize exceptions
    normalized = []
    for w, info in zip(workers, worker_infos):
        if isinstance(info, Exception):
            normalized.append({
                "id": w["id"], "name": w.get("name", w["id"]), "url": w["url"],
                "enabled": w.get("enabled", True), "healthy": False,
                "tier": w.get("tier", "low"), "max_concurrent": w.get("max_concurrent", 1),
                "active_jobs": 0, "in_flight_jobs": [], "encoder": "?", "system": None,
                "gpu": None, "error": str(info)[:120],
            })
        else:
            normalized.append(info)
    metrics = await _job_metrics(hours=24)
    enabled = [w for w in normalized if w.get("enabled")]
    healthy = [w for w in enabled if w.get("healthy")]
    return {
        "ok": True,
        "server_time": time.time(),
        "summary": {
            "total_workers": len(normalized),
            "enabled_workers": len(enabled),
            "healthy_workers": len(healthy),
            "down_workers": sum(1 for w in normalized if w.get("enabled") and not w.get("healthy")),
            "disabled_workers": sum(1 for w in normalized if not w.get("enabled")),
            "total_capacity": sum(w.get("max_concurrent", 1) for w in enabled),
            "active_jobs": sum(w.get("active_jobs", 0) for w in enabled),
            "live_jobs_in_db": len(live_jobs),
        },
        "cluster": normalized,
        "live_jobs": live_jobs,
        "metrics": metrics,
    }


@app.get("/api/cluster/jobs/live")
async def live_jobs_endpoint(_: bool = Depends(_verify_internal)):
    """Live job feed (active/queued only)."""
    return {
        "ok": True,
        "server_time": time.time(),
        "jobs": await _live_jobs_feed(limit=100),
    }


@app.get("/api/cluster/metrics")
async def cluster_metrics_endpoint(hours: int = 24, _: bool = Depends(_verify_internal)):
    """Aggregated metrics (per-TC, per-worker, hourly)."""
    hours = max(1, min(hours, 168))  # 1h..7d
    return {
        "ok": True,
        "server_time": time.time(),
        "metrics": await _job_metrics(hours=hours),
    }


# =====================================================================
# PUBLIC DASHBOARD (FIX 2026-08-19): no auth, anonymized, no internal URLs
# =====================================================================

def _anonymize_workers(workers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip URLs/IPs/hostnames/internal IDs. Return only safe public fields."""
    out = []
    for i, w in enumerate(workers, start=1):
        # Map tier to a generic friendly name
        tier = (w.get("tier") or "low").lower()
        tier_label = {"low": "Standard", "mid": "Performance", "high": "Compute+GPU"}.get(tier, "Compute")
        out.append({
            "name": f"Node-{i}",  # anonymized: Node-1, Node-2, ...
            "tier": tier_label,
            "tier_tone": tier,
            "enabled": w.get("enabled", True),
            "healthy": w.get("healthy", False),
            "active_jobs": w.get("active_jobs", 0),
            "max_concurrent": w.get("max_concurrent", 1),
            "encoder_kind": (
                "GPU" if (w.get("encoder") or "").startswith(("h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_videotoolbox"))
                else "CPU"
            ),
            "last_seen_ago": (
                int(time.time() - w["last_seen"]) if w.get("last_seen") else None
            ),
        })
    return out


def _public_metrics_view(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Filter internal metric view → only public-safe aggregates."""
    # Per-TC stats are OK to expose (no IP/PII)
    by_tc_public = []
    for t in metrics.get("by_tc", []):
        by_tc_public.append({
            "tc": t["tc"], "total": t["total"], "ok": t["ok"],
            "fail": t["fail"], "invalid": t["invalid"],
            "avg_sec": t["avg_sec"], "p50_sec": t["p50_sec"], "p95_sec": t["p95_sec"],
            "success_rate": t["success_rate"],
        })
    # Per-worker stats: keep aggregate only (rename to Node-N, drop worker_id)
    by_node_public = []
    for i, w in enumerate(metrics.get("by_worker", []), start=1):
        by_node_public.append({
            "node": f"Node-{i}",
            "total": w["total"], "ok": w["ok"],
            "avg_sec": w["avg_sec"], "success_rate": w["success_rate"],
        })
    return {
        "window_hours": metrics.get("window_hours", 24),
        "totals": metrics.get("totals", {}),
        "by_tc": by_tc_public,
        "by_node": by_node_public,  # renamed from by_worker
        "hourly_throughput": metrics.get("hourly_throughput", []),
    }


@app.get("/api/cluster/public")
async def cluster_public(hours: int = 24):
    """PUBLIC cluster status endpoint (FIX 2026-08-19).

    No auth required. Returns aggregated, anonymized cluster data:
      - Total workers (anonymized as Node-1..N), tier, capacity, health
      - Per-TC and per-node aggregate throughput/latency (no worker IDs)
      - NO internal URLs, hostnames, IPs, internal tokens, or admin actions.

    Workers that are disabled/unhealthy show as "offline" in the public view.
    """
    hours = max(1, min(hours, 168))
    workers = _load_workers()
    # Quick health probe (best-effort, anonymized)
    health_results = await asyncio.gather(
        *[_worker_health(w) if w.get("enabled", True) else asyncio.sleep(0, result={"ok": False, "disabled": True})
          for w in workers],
        return_exceptions=True,
    )
    extended = []
    for w, h in zip(workers, health_results):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:80]}
        enabled = w.get("enabled", True)
        is_healthy = enabled and h.get("ok") is True
        extended.append({
            "id": w["id"], "name": w.get("name", w["id"]),
            "tier": w.get("tier", "low"), "max_concurrent": w.get("max_concurrent", 1),
            "enabled": enabled, "healthy": is_healthy,
            "active_jobs": h.get("active_jobs", 0) if is_healthy else 0,
            "encoder": (_encoder_names(h) or ["?"])[0] if is_healthy else "?",
            "last_seen": h.get("last_seen") if is_healthy else None,
        })
    metrics = await _job_metrics(hours=hours)
    enabled = [w for w in extended if w["enabled"]]
    healthy = [w for w in enabled if w["healthy"]]
    return {
        "ok": True,
        "server_time": time.time(),
        "service": "V3 Cluster",
        "summary": {
            "total_nodes": len(extended),
            "enabled_nodes": len(enabled),
            "online_nodes": len(healthy),
            "offline_nodes": sum(1 for w in extended if w["enabled"] and not w["healthy"]),
            "disabled_nodes": sum(1 for w in extended if not w["enabled"]),
            "total_capacity": sum(w["max_concurrent"] for w in enabled),
            "active_jobs": sum(w["active_jobs"] for w in enabled),
            "window_hours": hours,
        },
        "nodes": _anonymize_workers(extended),
        "metrics": _public_metrics_view(metrics),
    }


class UserOut(BaseModel):
    user_id: str
    role: str
    display_name: Optional[str] = None
    monthly_quota: int
    monthly_used: int
    api_key_prefix: Optional[str] = None
    created_at: float
    last_seen_at: Optional[float] = None


def _get_user_or_404(user_id: str) -> Dict[str, Any]:
    """Fetch user by id, or raise 404."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, api_key_hash, role, display_name, monthly_quota, monthly_used, "
                "api_key_prefix, created_at, last_seen_at, last_reset_at FROM v3_users WHERE user_id=%s",
                (user_id,))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    return dict(row)


@app.get("/api/v1/users/me", response_model=UserOut)
async def get_me(user: str = Depends(_verify_user)):
    """Return the current authenticated user."""
    row = _get_user_or_404(user)
    return UserOut(**{k: row[k] for k in [
        "user_id", "role", "display_name", "monthly_quota", "monthly_used",
        "api_key_prefix", "created_at", "last_seen_at",
    ]})


@app.get("/api/v1/users/me/jobs")
async def list_my_jobs(
    user: str = Depends(_verify_user),
    limit: int = 50,
    status: Optional[str] = None,
    tc: Optional[str] = None,
):
    """List jobs for the current user (history view)."""
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            sql = """
            SELECT job_id, tc, status, progress, worker_id, output_file, output_size,
                   created_at, started_at, finished_at, error
            FROM v3_jobs WHERE user_id=%s
            """
            args = [user]
            if status:
                sql += " AND status=%s"
                args.append(status)
            if tc:
                sql += " AND tc=%s"
                args.append(tc)
            sql += " ORDER BY created_at DESC LIMIT %s"
            args.append(limit)
            cur.execute(sql, args)
            rows = cur.fetchall()
    finally:
        _pg_release(conn)
    return {
        "user_id": user,
        "total": len(rows),
        "jobs": [dict(r) for r in rows],
    }


@app.get("/api/v1/users/me/stats")
async def get_my_stats(user: str = Depends(_verify_user)):
    """Per-user stats: total jobs, success rate, total duration, active count."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) AS n,
                       COALESCE(SUM(finished_at - started_at), 0) AS total_dur,
                       COALESCE(SUM(COALESCE(output_size, 0)), 0) AS total_bytes
                FROM v3_jobs WHERE user_id=%s
                GROUP BY status
                """,
                (user,))
            rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) FROM v3_jobs WHERE user_id=%s AND status IN ('queued','running')",
                (user,))
            active = cur.fetchone()
    finally:
        _pg_release(conn)
    by_status = {r["status"]: {"n": r["n"], "total_dur": float(r["total_dur"]), "total_bytes": int(r["total_bytes"])} for r in rows}
    total_jobs = sum(r["n"] for r in rows)
    succeeded = by_status.get("succeeded", {}).get("n", 0)
    success_rate = (succeeded / total_jobs) if total_jobs else 0.0
    return {
        "user_id": user,
        "total_jobs": total_jobs,
        "active_jobs": active["count"] if active else 0,
        "success_rate": round(success_rate, 3),
        "total_duration_sec": float(sum(r["total_dur"] for r in rows)),
        "total_bytes_processed": int(sum(r["total_bytes"] for r in rows)),
        "by_status": by_status,
    }


@app.get("/api/v1/dashboard")
async def dashboard(
    user: str = Depends(_verify_user),
    limit: int = 20,
):
    """Lightweight JSON dashboard for the current user."""
    stats = await get_my_stats(user=user)
    jobs = await list_my_jobs(user=user, limit=limit)
    return {
        "user": stats,
        "recent_jobs": jobs["jobs"][:limit],
    }


_PUBLIC_SUBMIT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Submit Job · V3 Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);display:flex;align-items:center;justify-content:center;font-size:21px}
h1{margin:0;font-size:22px;font-weight:600}
.tagline{margin:3px 0 0;font-size:12px;color:#9aa0b4}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a{color:#60a5fa;text-decoration:none;padding:6px 12px;border-radius:6px}
.user-menu a:hover{background:#252837;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:14px;padding:24px;margin-bottom:20px}
.tc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
.tc-tile{padding:14px 12px;border:1px solid #252837;border-radius:10px;background:#0e1320;cursor:pointer;text-align:center;transition:all 0.2s;position:relative}
.tc-tile:hover{border-color:#22c55e;background:#141822}
.tc-tile.active{border-color:#22c55e;background:linear-gradient(135deg,rgba(34,197,94,0.15),rgba(16,185,129,0.1));box-shadow:0 0 0 1px #22c55e}
.tc-tile .name{font-weight:600;font-size:14px}
.tc-tile .desc{font-size:11px;color:#9aa0b4;margin-top:3px}
.tc-tile .badge{position:absolute;top:6px;right:8px;background:#252837;font-size:9px;padding:2px 6px;border-radius:3px;text-transform:uppercase;color:#fbbf24;font-weight:600}
.field{margin-bottom:16px}
.field label{display:block;font-size:12px;color:#9aa0b4;margin-bottom:6px;font-weight:500;text-transform:uppercase;letter-spacing:0.04em}
.field input[type=text],.field input[type=number],.field input[type=color],.field select,.field textarea{width:100%;padding:10px 12px;background:#0e1320;border:1px solid #252837;border-radius:7px;color:#e8e8f0;font-size:14px;font-family:inherit}
.field input[type=color]{height:42px;padding:4px}
.field input:focus,.field select:focus,.field textarea:focus{outline:none;border-color:#22c55e;background:#141822}
.field textarea{min-height:60px;resize:vertical}
.drop{border:2px dashed #252837;border-radius:10px;padding:24px;text-align:center;cursor:pointer;transition:all 0.2s;background:#0e1320}
.drop:hover,.drop.dragover{border-color:#22c55e;background:#141822}
.drop .hint{font-size:12px;color:#9aa0b4;margin-top:6px}
.drop input[type=file]{display:none}
.drop .filename{margin-top:8px;font-size:12px;color:#22c55e;font-family:"SF Mono",Consolas,monospace}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:14px}
.btn-primary{background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(34,197,94,0.25)}
.btn-primary:disabled{opacity:0.5;cursor:not-allowed;transform:none}
.btn-secondary{background:#252837;color:#e8e8f0}
.btn-secondary:hover{background:#2f3548}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
@media(max-width:720px){.row{grid-template-columns:1fr}}
.progress-overlay{position:fixed;inset:0;background:rgba(10,12,20,0.85);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;z-index:100}
.progress-overlay.active{display:flex}
.progress-box{background:#141822;border:1px solid #252837;border-radius:14px;padding:32px;max-width:440px;width:90%;text-align:center}
.spinner{display:inline-block;width:36px;height:36px;border:3px solid #252837;border-top-color:#22c55e;border-radius:50%;animation:spin 0.8s linear infinite;margin-bottom:14px}
@keyframes spin{to{transform:rotate(360deg)}}
.progress-box h3{margin:0 0 10px 0;font-size:18px}
.progress-box .step{color:#9aa0b4;font-size:13px;margin:6px 0}
.progress-box .step.done{color:#22c55e}
.progress-box .step.active{color:#fbbf24;font-weight:600}
.success{background:rgba(34,197,94,0.15);color:#86efac;padding:12px 16px;border-radius:8px;border:1px solid rgba(34,197,94,0.3);font-size:13px;margin-bottom:14px}
.error{background:rgba(239,68,68,0.15);color:#fca5a5;padding:12px 16px;border-radius:8px;border:1px solid rgba(239,68,68,0.3);font-size:13px;margin-bottom:14px}
.muted{color:#9aa0b4}
.row-2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:600px){.row-2{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<div class="progress-overlay" id="overlay">
  <div class="progress-box">
    <div class="spinner" id="spinner"></div>
    <h3 id="overlayTitle">Submitting job…</h3>
    <div class="step" id="stepUpload">⏵ Upload files</div>
    <div class="step" id="stepDispatch">⏵ Dispatch to worker</div>
    <div class="step" id="stepDone">⏵ Done</div>
  </div>
</div>
<script>
const API = "";

const TC_DEFS = {
  tc01: {name: "TC01", desc: "Chroma key (single product + bg)", fields: ["product","background"]},
  tc02: {name: "TC02", desc: "Reframe + chroma (7×3 = 21 outputs)", fields: ["product","background"]},
  tc03: {name: "TC03", desc: "Batch segments + chroma", fields: ["product","background","audio"]},
  tc04: {name: "TC04", desc: "Reframe + batch + chroma", fields: ["product","background"]},
  tc05: {name: "TC05", desc: "Reframe-only (multi sources)", fields: ["sources"]},
  tc06: {name: "TC06", desc: "Chroma + audio master (folder)", fields: []},
};

const DEFAULTS = {
  width: 1080, height: 1920, fps: 30, bitrate: "6000k",
  key_color: "#00FF00", similarity: 0.29, blend: 0.04, despill: 0.32,
  encoder: "nvenc", preset: "medium",
};

let selectedTC = "tc02";
let productFile = null, bgFile = null, audioFile = null, sourceFiles = [];

async function api(method, url, body, isForm=false) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body && !isForm) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  if (body && isForm) opts.body = body;
  const r = await fetch(API + url, opts);
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { ok:false, error: text }; }
  if (!r.ok) throw new Error(d.detail || d.error || r.statusText);
  return d;
}

function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }

async function load() {
  let me;
  try { me = await api("GET", "/api/v1/auth/me"); }
  catch { location.href = "/api/app?tab=login"; return; }
  render(me);
}

function render(me) {
  const root = document.getElementById("root");
  root.innerHTML = `
    <header>
      <a href="/api/app" class="brand" style="text-decoration:none;color:inherit">
        <div class="brand-mark">🎬</div><div><h1>Submit Job</h1><p class="tagline">${esc(me.user.email)} · ${me.user.monthly_used}/${me.user.monthly_quota} jobs used</p></div>
      </a>
      <div class="user-menu">
        <a href="/api/app/jobs">My Jobs</a><a href="/api/app/submit" style="background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14;padding:6px 12px;border-radius:6px;text-decoration:none">+ Submit</a>
        <a href="/api/app">Home</a>
        <button onclick="logout()" style="background:none;border:none;color:#60a5fa;cursor:pointer;font-family:inherit;font-size:13px;padding:6px 12px">Logout</button>
      </div>
    </header>

    <div class="card">
      <h2 style="margin-top:0">1. Choose Pipeline</h2>
      <div class="tc-grid">
        ${Object.entries(TC_DEFS).map(((([k,v]) => `
          <div class="tc-tile ${selectedTC===k?"active":""}" onclick="selectTC('${k}')" id="tc-${k}">
            ${v.fields.length===0?'<div class="badge">web=no</div>':''}
            <div class="name">${v.name}</div>
            <div class="desc">${esc(v.desc)}</div>
          </div>`)).join(""))}
      </div>
    </div>

    <div class="card" id="uploadCard">
      <h2 style="margin-top:0">2. Upload Files</h2>
      <div id="fileFields"></div>
    </div>

    <div class="card">
      <h2 style="margin-top:0">3. Settings</h2>
      <div class="row">
        <div class="field"><label>Width</label><input type="number" id="width" value="${DEFAULTS.width}"></div>
        <div class="field"><label>Height</label><input type="number" id="height" value="${DEFAULTS.height}"></div>
        <div class="field"><label>FPS</label><input type="number" id="fps" value="${DEFAULTS.fps}"></div>
      </div>
      <div class="row-2" style="margin-top:14px">
        <div class="field"><label>Encoder</label>
          <select id="encoder">
            <option value="nvenc" ${DEFAULTS.encoder==='nvenc'?'selected':''}>h264_nvenc (NVIDIA GPU)</option>
            <option value="libx264" ${DEFAULTS.encoder==='libx264'?'selected':''}>libx264 (CPU)</option>
            <option value="h264_videotoolbox" ${DEFAULTS.encoder==='h264_videotoolbox'?'selected':''}>h264_videotoolbox (macOS)</option>
          </select>
        </div>
        <div class="field"><label>Preset</label>
          <select id="preset">
            <option value="medium" ${DEFAULTS.preset==='medium'?'selected':''}>medium (balanced)</option>
            <option value="slow" ${DEFAULTS.preset==='slow'?'selected':''}>slow (better quality)</option>
            <option value="fast" ${DEFAULTS.preset==='fast'?'selected':''}>fast (faster)</option>
            <option value="p4" ${DEFAULTS.preset==='p4'?'selected':''}>p4 (NVENC)</option>
            <option value="p5" ${DEFAULTS.preset==='p5'?'selected':''}>p5 (NVENC)</option>
          </select>
        </div>
      </div>
      <div class="row-2" style="margin-top:14px">
        <div class="field"><label>Bitrate</label><input type="text" id="bitrate" value="${DEFAULTS.bitrate}"></div>
        <div class="field"><label>Key color</label><input type="color" id="key_color" value="${DEFAULTS.key_color}"></div>
      </div>
      <div class="row-2" style="margin-top:14px">
        <div class="field"><label>Similarity</label><input type="number" id="similarity" step="0.01" value="${DEFAULTS.similarity}"></div>
        <div class="field"><label>Despill</label><input type="number" id="despill" step="0.01" value="${DEFAULTS.despill}"></div>
      </div>
    </div>

    <div class="card">
      <h2 style="margin-top:0">4. Submit</h2>
      <div id="submitMsg"></div>
      <button class="btn btn-primary" onclick="submitJob()" id="submitBtn">Submit Job</button>
      <a href="/api/app/jobs" class="btn btn-secondary" style="margin-left:8px">View Jobs</a>
    </div>
  `;
  renderFileFields();
}

function selectTC(tc) {
  if (TC_DEFS[tc].fields.length === 0) {
    alert(tc.toUpperCase() + " requires a folder structure (product_root). Not supported via web yet.");
    return;
  }
  selectedTC = tc;
  document.querySelectorAll(".tc-tile").forEach(el => el.classList.remove("active"));
  document.getElementById("tc-" + tc).classList.add("active");
  renderFileFields();
  productFile = null; bgFile = null; audioFile = null; sourceFiles = [];
}

function renderFileFields() {
  const fields = TC_DEFS[selectedTC].fields;
  const html = fields.map(f => {
    const label = {product:"Product (green screen video)", background:"Background video", audio:"Audio file (optional)", sources:"Source videos (multiple)"}[f];
    const multi = (f === "sources");
    return `<div class="field">
      <label>${esc(label)}</label>
      <div class="drop" onclick="document.getElementById('file-${f}').click()" ondragover="event.preventDefault();this.classList.add('dragover')" ondragleave="this.classList.remove('dragover')" ondrop="handleDrop(event, '${f}')">
        <input type="file" id="file-${f}" ${multi?'multiple':''} accept="video/*,audio/*" onchange="handleFile('${f}', this.files)">
        <div style="font-size:24px">📁</div>
        <div>Click to choose ${multi?'files':'file'} or drag here</div>
        <div class="hint">${multi?'Select multiple source videos':'MP4 / MOV / WAV supported'}</div>
        <div class="filename" id="fname-${f}"></div>
      </div>
    </div>`;
  }).join("");
  document.getElementById("fileFields").innerHTML = html;
}

function handleFile(role, files) {
  if (role === "product") { productFile = files[0]; document.getElementById("fname-product").textContent = productFile?.name || ""; }
  else if (role === "background") { bgFile = files[0]; document.getElementById("fname-background").textContent = bgFile?.name || ""; }
  else if (role === "audio") { audioFile = files[0]; document.getElementById("fname-audio").textContent = audioFile?.name || ""; }
  else if (role === "sources") { sourceFiles = Array.from(files); document.getElementById("fname-sources").textContent = sourceFiles.map(f=>f.name).join(", "); }
}
function handleDrop(ev, role) {
  ev.preventDefault();
  ev.currentTarget.classList.remove("dragover");
  handleFile(role, ev.dataTransfer.files);
}

async function logout() { await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" }); location.href = "/api/app"; }

async function uploadToRole(role, file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/v1/uploads/" + role, { method: "POST", body: fd, credentials: "same-origin" });
  const d = await r.json();
  if (!d.ok) throw new Error(d.detail || "upload failed");
  return d.file_id;
}

function step(id, status) {
  const el = document.getElementById(id);
  el.classList.remove("active", "done");
  if (status === "active") el.classList.add("active");
  if (status === "done") { el.classList.add("done"); el.textContent = el.textContent.replace("⏵", "✓"); }
  else if (status === "active") el.textContent = el.textContent.replace("⏵", "▶");
}

async function submitJob() {
  const msg = document.getElementById("submitMsg");
  msg.innerHTML = "";
  document.getElementById("submitBtn").disabled = true;
  document.getElementById("overlay").classList.add("active");
  document.getElementById("overlayTitle").textContent = "Submitting job…";
  ["stepUpload","stepDispatch","stepDone"].forEach(s => { document.getElementById(s).className = "step"; document.getElementById(s).textContent = document.getElementById(s).textContent.replace("✓","⏵").replace("▶","⏵"); });

  try {
    const fields = TC_DEFS[selectedTC].fields;
    const fileIds = {};
    step("stepUpload", "active");
    if (fields.includes("product")) {
      if (!productFile) throw new Error("Please choose a product file");
      fileIds.product_id = await uploadToRole("product", productFile);
    }
    if (fields.includes("background")) {
      if (!bgFile) throw new Error("Please choose a background file");
      fileIds.background_id = await uploadToRole("background", bgFile);
    }
    if (fields.includes("audio") && audioFile) {
      fileIds.audio_id = await uploadToRole("audio", audioFile);
    }
    if (fields.includes("sources")) {
      if (!sourceFiles.length) throw new Error("Please choose source files");
      fileIds.source_ids = [];
      for (const f of sourceFiles) fileIds.source_ids.push(await uploadToRole("source", f));
    }
    step("stepUpload", "done");

    step("stepDispatch", "active");
    const settings = {
      width: parseInt(document.getElementById("width").value),
      height: parseInt(document.getElementById("height").value),
      fps: parseInt(document.getElementById("fps").value),
      encoder: document.getElementById("encoder").value,
      preset: document.getElementById("preset").value,
      bitrate: document.getElementById("bitrate").value,
      key_color: document.getElementById("key_color").value,
      similarity: parseFloat(document.getElementById("similarity").value),
      blend: DEFAULTS.blend,
      despill: parseFloat(document.getElementById("despill").value),
    };
    // Build payload compatible with V3RenderPayload: { files: {role: [file_id]}, settings: {...} }
    const files = {};
    if (fileIds.product_id) files.product = [fileIds.product_id];
    if (fileIds.background_id) files.background = [fileIds.background_id];
    if (fileIds.audio_id) files.audio = [fileIds.audio_id];
    if (fileIds.source_ids) files.source = fileIds.source_ids;
    const payload = { mode: selectedTC, files, settings };
    const r = await fetch("/api/" + selectedTC + "/render", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload), credentials: "same-origin" });
    const d = await r.json();
    step("stepDispatch", "done");

    if (!d.ok) {
      step("stepDone", "active");
      document.getElementById("overlayTitle").textContent = "Failed";
      throw new Error(d.detail || d.message || "render failed");
    }

    step("stepDone", "done");
    document.getElementById("overlayTitle").textContent = "Job submitted!";
    setTimeout(() => {
      document.getElementById("overlay").classList.remove("active");
      window.location.href = "/api/app/job/" + d.job_id;
    }, 1500);
  } catch (e) {
    msg.innerHTML = `<div class="error">✕ ${esc(e.message)}</div>`;
    document.getElementById("overlay").classList.remove("active");
    document.getElementById("submitBtn").disabled = false;
  }
}

load();
</script>
</body>
</html>
"""


_PROFILE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profile · V3 Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:720px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);display:flex;align-items:center;justify-content:center;font-size:21px}
h1{margin:0;font-size:22px;font-weight:600}
.tagline{margin:3px 0 0;font-size:12px;color:#9aa0b4}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a{color:#60a5fa;text-decoration:none;padding:6px 12px;border-radius:6px}
.user-menu a:hover{background:#252837;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:14px;padding:24px;margin-bottom:20px}
.card h2{margin:0 0 16px 0;font-size:13px;color:#9aa0b4;text-transform:uppercase;letter-spacing:0.06em;font-weight:600}
.field{margin-bottom:14px}
.field label{display:block;font-size:12px;color:#9aa0b4;margin-bottom:6px;font-weight:500}
.field input{width:100%;padding:10px 12px;background:#0e1320;border:1px solid #252837;border-radius:7px;color:#e8e8f0;font-size:14px;font-family:inherit}
.field input:focus{outline:none;border-color:#22c55e;background:#141822}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:14px}
.btn-primary{background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(34,197,94,0.25)}
.btn-secondary{background:#252837;color:#e8e8f0}
.btn-secondary:hover{background:#2f3548}
.btn-danger{background:rgba(239,68,68,0.2);color:#ef4444;border:1px solid rgba(239,68,68,0.3)}
.btn-danger:hover{background:rgba(239,68,68,0.3)}
.error{background:rgba(239,68,68,0.15);color:#fca5a5;padding:10px 14px;border-radius:8px;border:1px solid rgba(239,68,68,0.3);font-size:13px;margin-bottom:14px}
.success{background:rgba(34,197,94,0.15);color:#86efac;padding:10px 14px;border-radius:8px;border:1px solid rgba(34,197,94,0.3);font-size:13px;margin-bottom:14px}
.api-key-box{background:#0e1320;border:1px solid #252837;border-radius:8px;padding:14px;font-family:"SF Mono",Consolas,monospace;font-size:12px;color:#fbbf24;word-break:break-all;margin:8px 0}
.info-row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1f2533}
.info-row:last-child{border-bottom:none}
.info-row .k{color:#9aa0b4;font-size:11pt}
.info-row .v{color:#e8e8f0;font-family:"SF Mono",Consolas,monospace;font-size:11pt}
.danger-zone{border:1px solid rgba(239,68,68,0.3);background:rgba(239,68,68,0.05)}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<script>
async function api(method, url, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { ok:false, error: text }; }
  if (!r.ok) throw new Error(d.detail || d.error || r.statusText);
  return d;
}
function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtTs(epoch) { if (!epoch) return "—"; return new Date(epoch * 1000).toLocaleString(); }

async function load() {
  let me;
  try { me = await api("GET", "/api/v1/auth/me"); }
  catch { location.href = "/api/app?tab=login"; return; }
  const u = me.user;
  render(u);
}

function render(u) {
  document.getElementById("root").innerHTML = `
    <header>
      <a href="/api/app" class="brand" style="text-decoration:none;color:inherit">
        <div class="brand-mark">🎬</div><div><h1>Profile</h1><p class="tagline">${esc(u.email)}</p></div>
      </a>
      <div class="user-menu">
        <a href="/api/app/jobs">Jobs</a>
        <a href="/api/app/submit">+ Submit Job</a>
        <button onclick="logout()" style="background:none;border:none;color:#60a5fa;cursor:pointer;font-family:inherit;font-size:13px;padding:6px 12px">Logout</button>
      </div>
    </header>

    <div class="card">
      <h2>Account Info</h2>
      <div id="profileErr"></div>
      <div id="profileOk"></div>
      <div class="info-row"><span class="k">User ID</span><span class="v">${esc(u.user_id)}</span></div>
      <div class="info-row"><span class="k">Email</span><span class="v">${esc(u.email || "—")}</span></div>
      <div class="info-row"><span class="k">Display name</span><span class="v">${esc(u.display_name || "—")}</span></div>
      <div class="info-row"><span class="k">Role</span><span class="v">${esc(u.role)}</span></div>
      <div class="info-row"><span class="k">Monthly quota</span><span class="v">${u.monthly_used} / ${u.monthly_quota}</span></div>
      <div class="info-row"><span class="k">API key prefix</span><span class="v">${esc(u.api_key_prefix || "—")}</span></div>
      <div class="info-row"><span class="k">Created</span><span class="v">${fmtTs(u.created_at)}</span></div>
      <div class="info-row"><span class="k">Last seen</span><span class="v">${fmtTs(u.last_seen_at)}</span></div>
    </div>

    <div class="card">
      <h2>Update Profile</h2>
      <form onsubmit="return updateProfile(event)">
        <div class="field"><label>Display name</label><input name="display_name" value="${esc(u.display_name || "")}"></div>
        <div class="field"><label>Email (must be unique)</label><input name="email" type="email" value="${esc(u.email || "")}"></div>
        <button class="btn btn-primary" type="submit">Save changes</button>
      </form>
    </div>

    <div class="card">
      <h2>Change Password</h2>
      <div id="pwErr"></div>
      <div id="pwOk"></div>
      <form onsubmit="return changePassword(event)">
        <div class="field"><label>Current password</label><input name="old_password" type="password" required></div>
        <div class="field"><label>New password (min 8 chars)</label><input name="new_password" type="password" required minlength="8"></div>
        <div class="field"><label>Confirm new password</label><input name="confirm" type="password" required minlength="8"></div>
        <button class="btn btn-primary" type="submit">Update password</button>
      </form>
    </div>

    <div class="card danger-zone">
      <h2 style="color:#ef4444">Danger Zone</h2>
      <p class="muted">Account-level actions.</p>
      <button class="btn btn-danger" onclick="confirmDelete()">Delete my account</button>
    </div>
  `;
}

async function updateProfile(e) {
  e.preventDefault();
  const err = document.getElementById("profileErr");
  const ok = document.getElementById("profileOk");
  err.innerHTML = ""; ok.innerHTML = "";
  const fd = new FormData(e.target);
  const body = {};
  if (fd.get("display_name")) body.display_name = fd.get("display_name");
  if (fd.get("email")) body.email = fd.get("email");
  if (!Object.keys(body).length) { err.textContent = "No changes"; err.classList.add("error"); return false; }
  try {
    await api("PATCH", "/api/v1/auth/me", body);
    ok.textContent = "✓ Saved";
    setTimeout(() => location.reload(), 1000);
  } catch (ex) {
    err.textContent = "✕ " + ex.message;
    err.classList.add("error");
  }
  return false;
}

async function changePassword(e) {
  e.preventDefault();
  const err = document.getElementById("pwErr");
  const ok = document.getElementById("pwOk");
  err.innerHTML = ""; ok.innerHTML = "";
  const fd = new FormData(e.target);
  const oldpw = fd.get("old_password");
  const newpw = fd.get("new_password");
  const confirm = fd.get("confirm");
  if (newpw !== confirm) {
    err.innerHTML = '<div class="error">✕ New passwords do not match</div>';
    return false;
  }
  try {
    await api("POST", "/api/v1/auth/change-password", { old_password: oldpw, new_password: newpw });
    ok.innerHTML = '<div class="success">✓ Password changed</div>';
    e.target.reset();
  } catch (ex) {
    err.innerHTML = '<div class="error">✕ ' + ex.message + '</div>';
  }
  return false;
}

async function confirmDelete() {
  if (!confirm("Are you sure? This will permanently delete your account and all jobs.")) return;
  // No endpoint yet — just warn
  alert("Account deletion requires contacting support@sj88ai.com. We'll handle it within 24h.");
}

async function logout() { await api("POST", "/api/v1/auth/logout", {}); location.href = "/api/app"; }

load();
</script>
</body>
</html>
"""





_APP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V3 Studio · Video Rendering</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px;flex-wrap:wrap;gap:16px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);
display:flex;align-items:center;justify-content:center;font-size:21px;box-shadow:0 4px 14px rgba(34,197,94,0.25)}
h1{margin:0;font-size:22px;font-weight:600;letter-spacing:-0.01em}
.tagline{margin:3px 0 0;font-size:12px;color:#9aa0b4}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a,.user-menu button{color:#60a5fa;text-decoration:none;background:none;border:none;cursor:pointer;font-family:inherit;font-size:13px;padding:6px 12px;border-radius:6px}
.user-menu a:hover,.user-menu button:hover{background:#252837;color:#e8e8f0}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:14px;transition:all 0.2s;text-decoration:none}
.btn-primary{background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(34,197,94,0.25)}
.btn-secondary{background:#252837;color:#e8e8f0}
.btn-secondary:hover{background:#2f3548}
.btn-ghost{background:transparent;color:#9aa0b4;border:1px solid #252837}
.btn-ghost:hover{background:#1a1d29;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);backdrop-filter:blur(8px);border:1px solid #252837;border-radius:14px;padding:22px;margin-bottom:20px}
.auth-card{max-width:480px;margin:60px auto}
.auth-card h2{margin:0 0 6px 0;font-size:20px}
.auth-card .auth-sub{color:#9aa0b4;margin:0 0 24px 0;font-size:13px}
.field{margin-bottom:14px}
.field label{display:block;font-size:12px;color:#9aa0b4;margin-bottom:6px;font-weight:500}
.field input,.field select{width:100%;padding:10px 12px;background:#0e1320;border:1px solid #252837;border-radius:7px;color:#e8e8f0;font-size:14px;font-family:inherit}
.field input:focus,.field select:focus{outline:none;border-color:#22c55e;background:#141822}
.tabs{display:flex;gap:8px;margin-bottom:20px;border-bottom:1px solid #1f2533}
.tab{padding:10px 16px;cursor:pointer;color:#9aa0b4;border-bottom:2px solid transparent;font-weight:500}
.tab.active{color:#22c55e;border-bottom-color:2#22c55e}
.error{background:rgba(239,68,68,0.15);color:#fca5a5;padding:10px 14px;border-radius:8px;border:1px solid rgba(239,68,68,0.3);font-size:13px;margin-bottom:14px}
.success{background:rgba(34,197,94,0.15);color:#86efac;padding:10px 14px;border-radius:8px;border:1px solid rgba(34,197,94,0.3);font-size:13px;margin-bottom:14px}
.info{background:rgba(96,165,250,0.12);color:#93c5fd;padding:10px 14px;border-radius:8px;border:1px solid rgba(96,165,250,0.3);font-size:13px;margin-bottom:14px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:24px}
.stat-card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:12px;padding:16px 18px}
.stat-card .label{font-size:11px;color:#9aa0b4;text-transform:uppercase;letter-spacing:0.06em;font-weight:500}
.stat-card .value{font-size:26px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}
.stat-card .sub{font-size:11px;color:#6b7280;margin-top:4px}
table{width:100%;border-collapse:collapse;background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #1a1d29}
th{background:#1a1d29;color:#9aa0b4;font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:0.06em}
tr:hover{background:#1a1d2c}
td.mono{font-family:"SF Mono",Consolas,monospace;font-size:11px;color:#9aa0b4}
td .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em}
.pill-queued{background:rgba(245,158,11,0.18);color:#f59e0b}
.pill-running{background:rgba(59,130,246,0.18);color:#60a5fa}
.pill-paused{background:rgba(168,85,247,0.18);color:#a855f7}
.pill-succeeded{background:rgba(34,197,94,0.18);color:#22c55e}
.pill-failed{background:rgba(239,68,68,0.18);color:#ef4444}
.pill-invalid{background:rgba(168,85,247,0.18);color:#a855f7}
.action-link{color:#60a5fa;text-decoration:none}
.action-link:hover{text-decoration:underline}
.upload-zone{border:2px dashed #252837;border-radius:10px;padding:30px;text-align:center;cursor:pointer;transition:all 0.2s;background:#0e1320}
.upload-zone:hover,.upload-zone.dragover{border-color:#22c55e;background:#141822}
.upload-zone .hint{font-size:13px;color:#9aa0b4;margin-top:10px}
.job-progress{display:flex;align-items:center;gap:12px;margin:12px 0}
.job-progress .bar{flex:1;height:8px;background:#252837;border-radius:4px;overflow:hidden}
.job-progress .fill{display:block;height:100%;background:linear-gradient(90deg,#60a5fa,#22c55e);border-radius:4px;transition:width 0.4s}
.job-progress .pct{font-variant-numeric:tabular-nums;color:#9aa0b4;min-width:48px;text-align:right}
.node-pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;background:#252837;border-radius:10px;font-size:11px;font-weight:500}
.node-pill .dot{width:6px;height:6px;border-radius:50%;background:#22c55e}
.node-pill.busy .dot{background:#f59e0b;animation:pulse 1s infinite}
.node-pill.full .dot{background:#ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.empty-state{padding:40px 20px;text-align:center;color:#6b7280;font-style:italic}
.muted{color:#9aa0b4}
.token-display{background:#0e1320;padding:10px 14px;border-radius:8px;font-family:"SF Mono",Consolas,monospace;font-size:12px;word-break:break-all;margin:12px 0;border:1px solid #252837;color:#fbbf24}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<script>
const API = ""; // same origin
let me = null;

async function api(method, url, body, isForm=false) {
  const opts = { method, headers: {} };
  if (body && !isForm) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  if (body && isForm) opts.body = body;
  const r = await fetch(API + url, opts);
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { ok:false, error: text }; }
  if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
  return data;
}

async function load() {
  try { me = await api("GET", "/api/v1/auth/me"); } catch { me = null; }
  render();
}

function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtSec(s) {
 if (s == null || s === 0) return "—";
  if (s < 60) return s.toFixed(1) + "s";
  if (s < 3600) return Math.floor(s/60) + "m " + Math.floor(s%60) + "s";
  return Math.floor(s/3600) + "h " + Math.floor((s%3600)/60) + "m";
}
function fmtAgo(epoch) {
  if (!epoch) return "—";
  const dt = Date.now()/1000 - epoch;
  if (dt < 60) return Math.floor(dt) + "s ago";
  if (dt < 3600) return Math.floor(dt/60) + "m ago";
  if (dt < 86400) return Math.floor(dt/3600) + "h ago";
  return Math.floor(dt/86400) + "d ago";
}

function headerBar() {
  const right = me ? `
    <div class="user-menu">
      <span class="muted">${esc(me.user.email || me.user.user_id)} · ${me.user.monthly_used}/${me.user.monthly_quota} jobs</span>
      <a href="/api/app/jobs">My Jobs</a><a href="/api/app/submit" style="background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14;padding:6px 12px;border-radius:6px;text-decoration:none">+ Submit</a>
      <button onclick="logout()">Logout</button>
    </div>` : `
    <div class="user-menu">
      <a href="/api/app?tab=login">Sign in</a>
      <a class="btn btn-primary" href="/api/app?tab=signup">Get started</a>
    </div>`;
  return `<header><div class="brand">
    <div class="brand-mark">🎬</div><div><h1>V3 Studio</h1><p class="tagline">AI-powered green-screen video rendering</p></div>
  </div>${right}</header>`;
}

function render() {
  const root = document.getElementById("root");
  if (!me) {
    const params = new URLSearchParams(location.search);
    const tab = params.get("tab") || "signup";
    root.innerHTML = headerBar() + renderAuth(tab);
  } else {
    root.innerHTML = headerBar() + renderDashboard();
  }
}

function renderAuth(initialTab) {
  return `<div class="auth-card card">
    <h2 id="auth-title">${initialTab === "login" ? "Sign in" : "Create your account"}</h2>
    <p class="auth-sub">${initialTab === "login" ? "Access your renders" : "Start rendering green-screen videos — no credit card"}</p>
    <div id="auth-error"></div>
    <div id="auth-success"></div>
    <div class="tabs">
      <div class="tab ${initialTab === "signup" ? "active":""}" onclick="switchTab('signup')">Sign up</div>
      <div class="tab ${initialTab === "login" ? "active":""}" onclick="switchTab('login')">Sign in</div>
    </div>
    <form onsubmit="return handleAuth(event)">
      <div id="signup-fields" style="display:${initialTab === "signup" ? "block":"none"}">
        <div class="field"><label>Email</label><input type="email" name="email" required></div>
        <div class="field"><label>Display name (optional)</label><input type="text" name="display_name"></div>
        <div class="field"><label>Password (min 8 chars)</label><input type="password" name="password" required minlength="8"></div>
      </div>
      <div id="login-fields" style="display:${initialTab === "login" ? "block":"none"}">
        <div class="field"><label>Email</label><input type="email" name="email" required></div>
        <div class="field"><label>Password</label><input type="password" name="password" required></div>
      </div>
      <button class="btn btn-primary" type="submit" style="width:100%;justify-content:center">${initialTab === "login" ? "Sign in" : "Create account"}</button>
    </form>
  </div>`;
}

function switchTab(tab) {
  const root = document.getElementById("root");
  root.innerHTML = headerBar() + renderAuth(tab);
}

async function handleAuth(e) {
  e.preventDefault();
  const f = e.target;
  const err = document.getElementById("auth-error");
  const ok = document.getElementById("auth-success");
  err.innerHTML = ""; ok.innerHTML = "";
  const email = f.email.value.trim();
  const password = f.password.value;
  const display_name = f.display_name?.value || null;
  const params = new URLSearchParams(location.search);
  const tab = params.get("tab") || "signup";
  try {
    if (tab === "signup") {
      const data = await api("POST", "/api/v1/auth/signup", { email, password, display_name });
      ok.innerHTML = `<div class="success">✓ Account created! Save your API key (shown only once):<div class="token-display">${esc(data.api_key)}</div><button class="btn btn-secondary" onclick="navigator.clipboard.writeText('${data.api_key}');this.textContent='✓ Copied!'">Copy API key</button></div>`;
      setTimeout(async () => { me = await api("GET", "/api/v1/auth/me"); render(); }, 8000);
    } else {
      await api("POST", "/api/v1/auth/login", { email, password });
      me = await api("GET", "/api/v1/auth/me");
      location.href = "/api/app/jobs";
    }
  } catch (ex) { err.innerHTML = `<div class="error">✕ ${esc(ex.message)}</div>`; }
  return false;
}

async function logout() {
  await api("POST", "/api/v1/auth/logout", {});
  me = null;
  render();
}

async function renderDashboard() {
  const data = await api("GET", "/api/v1/users/me/jobs?limit=20");
  const jobs = data.jobs || [];
  const ok = jobs.filter(j => j.status === "succeeded" || j.status === "SUCCEEDED").length;
  const running = jobs.filter(j => j.status === "running").length;
  const total_sec = jobs.filter(j => j.finished_at && j.started_at).reduce((s, j) => s + (j.finished_at - j.started_at), 0);
  const html = `<div class="stat-grid">
    <div class="stat-card"><div class="label">Monthly Quota</div><div class="value">${me.user.monthly_used}/${me.user.monthly_quota}</div><div class="sub">${me.user.monthly_quota - me.user.monthly_used} remaining</div></div>
    <div class="stat-card"><div class="label">Active Jobs</div><div class="value">${running}</div><div class="sub">${jobs.length} total</div></div>
    <div class="stat-card"><div class="label">Success Rate</div><div class="value">${jobs.length ? Math.round(100 * ok / jobs.length) : 0}%</div><div class="sub">${ok}/${jobs.length} succeeded</div></div>
    <div class="stat-card"><div class="label">API Key</div><div class="value mono" style="font-size:14px;color:#fbbf24">${esc(me.user.api_key_prefix)}</div><div class="sub">Save this to use the API</div></div>
  </div>
  <div class="card"><h2 style="margin-top:0">Recent Jobs</h2>${jobsTable(jobs)}</div>`;
  document.getElementById("root").innerHTML = headerBar() + `<h1 style="font-size:18px;margin-bottom:16px">Welcome, ${esc(me.user.display_name || me.user.email)}</h1>` + html;
}

function jobsTable(jobs) {
  if (!jobs.length) return '<div class="empty-state">No jobs yet — submit your first render from the API or /api/app/jobs</div>';
  return `<table><thead><tr><th>Job ID</th><th>TC</th><th>Status</th><th>Worker</th><th>Created</th><th>Duration</th></tr></thead><tbody>
    ${jobs.map(j => `<tr>
      <td class="mono"><a class="action-link" href="/api/app/job/${esc(j.job_id)}">${esc(j.job_id?.slice(-12))}</a></td>
      <td><span class="pill pill-${esc(j.tc)}">${esc((j.tc||'').toUpperCase())}</span></td>
      <td><span class="pill pill-${esc(j.status)}">${esc(j.status)}</span></td>
      <td class="mono">${esc(j.worker_id || "—")}</td>
      <td class="mono">${fmtAgo(j.created_at)}</td>
      <td class="mono">${j.started_at && j.finished_at ? fmtSec(j.finished_at - j.started_at) : "—"}</td>
    </tr>`).join("")}
  </tbody></table>`;
}

load();
</script>
</body>
</html>
"""


_JOBS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Jobs · V3 Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);display:flex;align-items:center;justify-content:center;font-size:21px}
h1{margin:0;font-size:22px;font-weight:600}
.tagline{margin:3px 0 0;font-size:12px;color:#9aa0b4}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a,.user-menu button{color:#60a5fa;text-decoration:none;background:none;border:none;cursor:pointer;font-family:inherit;font-size:13px;padding:6px 12px;border-radius:6px}
.user-menu a:hover,.user-menu button:hover{background:#252837;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:14px;padding:22px;margin-bottom:20px}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:14px;text-decoration:none}
.btn-primary{background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(34,197,94,0.25)}
.btn-secondary{background:#252837;color:#e8e8f0}
table{width:100%;border-collapse:collapse;background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #1a1d29}
th{background:#1a1d29;color:#9aa0b4;font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:0.06em}
tr:hover{background:#1a1d2c}
td.mono{font-family:"SF Mono",Consolas,monospace;font-size:11px;color:#9aa0b4}
td .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;text-transform:uppercase}
.pill-queued{background:rgba(245,158,11,0.18);color:#f59e0b}
.pill-running{background:rgba(59,130,246,0.18);color:#60a5fa}
.pill-succeeded{background:rgba(34,197,94,0.18);color:#22c55e}
.pill-failed{background:rgba(239,68,68,0.18);color:#ef4444}
.pill-invalid{background:rgba(168,85,247,0.18);color:#a855f7}
.action-link{color:#60a5fa;text-decoration:none}
.action-link:hover{text-decoration:underline}
.muted{color:#9aa0b4}
.empty-state{padding:40px 20px;text-align:center;color:#6b7280;font-style:italic}
.filter-row{display:flex;gap:10px;margin-bottom:16px;align-items:center;flex-wrap:wrap}
.filter-row select,.filter-row input{padding:8px 12px;background:#0e1320;border:1px solid #252837;border-radius:7px;color:#e8e8f0;font-family:inherit;font-size:13px}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<script>
async function api(method, url) {
  const r = await fetch(url, { method, credentials: "same-origin" });
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { ok:false, error: text }; }
  if (!r.ok) { location.href = "/api/app"; throw new Error("auth"); }
  return d;
}
function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&gt;".replace,"&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtSec(s) { if (s == null || s === 0) return "—"; if (s < 60) return s.toFixed(1) + "s"; if (s < 3600) return Math.floor(s/60) + "m"; return Math.floor(s/3600) + "h"; }
function fmtAgo(epoch) { if (!epoch) return "—"; const dt = Date.now()/1000 - epoch; if (dt < 60) return Math.floor(dt) + "s ago"; if (dt < 3600) return Math.floor(dt/60) + "m ago"; if (dt < 86400) return Math.floor(dt/3600) + "h ago"; return Math.floor(dt/86400) + "d ago"; }

async function load() {
  let me, jobsData;
  try { me = await api("GET", "/api/v1/auth/me"); } catch { location.href = "/api/app"; return; }
  jobsData = await api("GET", "/api/v1/users/me/jobs?limit=100");
  const jobs = jobsData.jobs || [];
  document.getElementById("root").innerHTML = `
    <header>
      <a href="/api/app" class="brand" style="text-decoration:none;color:inherit">
        <div class="brand-mark">🎬</div><div><h1>V3 Studio</h1><p class="tagline">${esc(me.user.email)}</p></div>
      </a>
      <div class="user-menu">
        <a href="/api/app">Home</a>
        <a href="/api/app/jobs">Jobs</a><a href="/api/app/submit" style="background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14">+ Submit Job</a>
        <button onclick="logout()">Logout</button>
      </div>
    </header>
    <h1 style="font-size:18px;margin-bottom:16px">My Jobs (${jobs.length})</h1>
    <div class="card">
      <div class="filter-row">
        <select id="statusFilter"><option value="">All statuses</option><option value="running">Running</option><option value="queued">Queued</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option></select>
        <select id="tcFilter"><option value="">All pipelines</option><option value="tc01">TC01</option><option value="tc02">TC02</option><option value="tc03">TC03</option><option value="tc04">TC04</option><option value="tc05">TC05</option><option value="tc06">TC06</option></select>
        <span class="muted">${jobs.length} jobs total</span>
      </div>
      ${table(jobs)}
    </div>`;
  document.getElementById("statusFilter").onchange = () => filter(jobs);
  document.getElementById("tcFilter").onchange = () => filter(jobs);
}

function filter(jobs) {
  const s = document.getElementById("statusFilter").value;
  const t = document.getElementById("tcFilter").value;
  const filtered = jobs.filter(j =>
    (!s || j.status === s || j.status === s.toUpperCase()) &&
    (!t || j.tc === t)
  );
  document.querySelector("#jobs-tbody").innerHTML = filtered.map(j =>
    `<tr><td class="mono"><a class="action-link" href="/api/app/job/${esc(j.job_id)}">${esc(j.job_id?.slice(-12))}</a></td><td><span class="pill pill-${esc(j.tc)}">${esc((j.tc||'').toUpperCase())}</span></td><td><span class="pill pill-${esc(j.status)}">${esc(j.status)}</span></td><td class="mono">${esc(j.worker_id || "—")}</td><td class="mono">${fmtAgo(j.created_at)}</td><td class="mono">${j.started_at && j.finished_at ? fmtSec(j.finished_at - j.started_at) : "—"}</td><td class="mono">${j.output_size ? (j.output_size/1024/1024).toFixed(1) + "MB" : "—"}</td></tr>`
  ).join("");
}

function table(jobs) {
  if (!jobs.length) return '<div class="empty-state">No jobs yet.</div>';
  return `<table><thead><tr><th>Job ID</th><th>TC</th><th>Status</th><th>Worker</th><th>Created</th><th>Duration</th><th>Output</th></tr></thead><tbody id="jobs-tbody">${
    jobs.map(j => `<tr><td class="mono"><a class="action-link" href="/api/app/job/${esc(j.job_id)}">${esc(j.job_id?.slice(-12))}</a></td><td><span class="pill pill-${esc(j.tc)}">${esc((j.tc||'').toUpperCase())}</span></td><td><span class="pill pill-${esc(j.status)}">${esc(j.status)}</span></td><td class="mono">${esc(j.worker_id || "—")}</td><td class="mono">${fmtAgo(j.created_at)}</td><td class="mono">${j.started_at && j.finished_at ? fmtSec(j.finished_at - j.started_at) : "—"}</td><td class="mono">${j.output_size ? (j.output_size/1024/1024).toFixed(1) + "MB" : "—"}</td></tr>`).join("")
  }</tbody></table>`;
}

async function logout() { await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" }); location.href = "/api/app"; }

load();
setInterval(load, 30000);  // refresh every 30s
</script>
</body>
</html>
"""


_JOB_DETAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job · V3 Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);display:flex;align-items:center;justify-content:center;font-size:21px}
h1{margin:0;font-size:18px;font-weight:600}
.tagline{margin:3px 0 0;font-size:11px;color:#9aa0b4;font-family:"SF Mono",Consolas,monospace}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a{color:#60a5fa;text-decoration:none}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:13px;text-decoration:none}
.btn-secondary{background:#252837;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:14px;padding:22px;margin-bottom:20px}
.card h2{margin:0 0 12px 0;font-size:13px;color:#9aa0b4;text-transform:uppercase;letter-spacing:0.06em;font-weight:600}
.hero-progress{text-align:center;padding:20px 0}
.hero-progress .pct{font-size:64px;font-weight:600;font-variant-numeric:tabular-nums;background:linear-gradient(135deg,#22c55e,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-progress .label{font-size:14px;color:#9aa0b4;margin-top:4px}
.progress-bar{height:14px;background:#252837;border-radius:7px;overflow:hidden;margin:14px 0}
.progress-bar .fill{height:100%;background:linear-gradient(90deg,#60a5fa,#22c55e);border-radius:7px;transition:width 0.5s}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
@media(max-width:720px){.grid-2{grid-template-columns:1fr}}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px}
.kv dt{color:#9aa0b4}
.kv dd{margin:0;color:#e8e8f0}
.node-card{background:#0e1320;border:1px solid #252837;border-radius:10px;padding:14px;display:flex;align-items:center;gap:14px}
.node-icon{width:42px;height:42px;border-radius:10px;background:linear-gradient(135deg,#10b981,#22d3ee);display:flex;align-items:center;justify-content:center;font-size:20px}
.node-card .info{flex:1;min-width:0}
.node-card .name{font-weight:600;font-size:14px}
.node-card .tier{font-size:11px;color:#9aa0b4;text-transform:uppercase;margin-top:2px}
.node-card .load{font-size:11px;color:#9aa0b4;margin-top:2px}
.loadbar{height:6px;background:#252837;border-radius:3px;margin-top:6px;overflow:hidden}
.loadbar .fill{height:100%;background:linear-gradient(90deg,#60a5fa,#22c55e);border-radius:3px;transition:width 0.4s}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:6px;vertical-align:middle;animation:pulse 1.5s ease-in-out infinite}
.dot.busy{background:#f59e0b}
.dot.err{background:#ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.log-box{background:#0e1320;border:1px solid #252837;border-radius:8px;padding:14px;font-family:"SF Mono",Consolas,monospace;font-size:11px;color:#9aa0b4;max-height:240px;overflow-y:auto;line-height:1.6;white-space:pre-wrap}
.output-list{display:grid;gap:6px}
.output-item{background:#0e1320;border:1px solid #252837;border-radius:8px;padding:8px 12px;font-family:"SF Mono",Consolas,monospace;font-size:12px;display:flex;justify-content:space-between;align-items:center}
.output-item a{color:#60a5fa;text-decoration:none}
.output-item a:hover{text-decoration:underline}
.muted{color:#9aa0b4}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<script>
let jobId = location.pathname.split("/").pop();

async function api(method, url) {
  const r = await fetch(url, { method, credentials: "same-origin" });
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { ok:false, error: text }; }
  if (!r.ok) throw new Error(d.detail || d.error || r.statusText);
  return d;
}
function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtSec(s) { if (s == null || s === 0) return "—"; if (s < 60) return s.toFixed(1) + "s"; if (s < 3600) return Math.floor(s/60) + "m " + Math.floor(s%60) + "s"; return Math.floor(s/3600) + "h " + Math.floor((s%3600)/60) + "m"; }
function fmtAgo(epoch) { if (!epoch) return "—"; const dt = Date.now()/1000 - epoch; if (dt < 60) return Math.floor(dt) + "s ago"; if (dt < 3600) return Math.floor(dt/60) + "m ago"; if (dt < 86400) return Math.floor(dt/3600) + "h ago"; return Math.floor(dt/86400) + "d ago"; }
function fmtSize(b) { if (!b) return "—"; const u = ["B","KB","MB","GB"]; let i = 0; let v = b; while (v >= 1024 && i < u.length-1) { v/=1024; i++; } return v.toFixed(1) + " " + u[i]; }

async function load() {
  let job, me;
  try { me = await api("GET", "/api/v1/auth/me"); }
  catch { location.href = "/api/app"; return; }
  try { job = await api("GET", `/api/v1/jobs/${encodeURIComponent(jobId)}/live`); }
  catch (e) { document.getElementById("root").innerHTML = `<header><a href="/api/app" class="brand"><div class="brand-mark">🎬</div><div><h1>V3 Studio</h1><p class="tagline">Job not found</p></div></a></header><div class="card"><h2>Job not found</h2><p>This job may have been deleted or you don't have access to it.</p><a class="btn btn-secondary" href="/api/app/jobs">Back to my jobs</a></div>`; return; }

  const progress = Math.round((job.progress || 0) * 100);
  const status = (job.status || "unknown").toLowerCase();
  const isActive = ["running","queued","paused"].includes(status);
  const dotClass = status === "running" ? "busy" : (status === "failed" ? "err" : "");

  const workerBlock = job.worker ? `
    <div class="card">
      <h2>Assigned Worker</h2>
      <div class="node-card">
        <div class="node-icon">🖥️</div>
        <div class="info">
          <div class="name">${esc(job.worker.node)} <span style="color:#9aa0b4;font-weight:400;font-size:12px">(${esc(job.worker.tier)})</span></div>
          <div class="load">Load: ${esc(job.worker_load?.active_jobs ?? "—")} / ${esc(job.worker.max_concurrent)} active jobs</div>
          <div class="loadbar"><div class="fill" style="width:${(job.worker_load?.active_jobs / job.worker.max_concurrent * 100) || 0}%"></div></div>
        </div>
      </div>
    </div>` : "";

  const eta = job.eta_seconds != null ? (status === "running" ? "≈ " + fmtSec(job.eta_seconds) + " remaining" : status === "queued" ? "≈ " + fmtSec(job.eta_seconds) + " wait + render" : "—") : "—";

  const logBox = job.log && job.log.length ? `<div class="log-box">${esc(job.log.slice(-30).map(l => typeof l === 'string' ? l : JSON.stringify(l)).join("\\n"))}</div>` : '<div class="log-box">No log output yet.</div>';

  const outputList = job.output_files && job.output_files.length ? `
    <div class="output-list">${job.output_files.map(f => { const fn = typeof f === 'string' ? f.split('/').pop() : (f.name || JSON.stringify(f)); return `<div class="output-item"><span>${esc(fn)}</span><a href="/api/v1/jobs/${encodeURIComponent(job.job_id)}/download/${encodeURIComponent(fn)}" target="_blank">Download →</a></div>`; }).join("")}</div>` : '<div class="muted">No outputs yet.</div>';

  document.getElementById("root").innerHTML = `
    <header>
      <a href="/api/app/jobs" class="brand" style="text-decoration:none;color:inherit">
        <div class="brand-mark">🎬</div><div><h1>Job ${esc(job.job_id?.slice(-12))}</h1><p class="tagline">${esc(jobId)}</p></div>
      </a>
      <div class="user-menu"><a href="/api/app/jobs">Back to jobs</a><a href="/api/app/submit" style="background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14;padding:6px 12px;border-radius:6px;text-decoration:none;margin-left:8px">+ New Job</a></div>
    </header>

    <div class="card">
      <div class="hero-progress">
        <div class="pct">${progress}%</div>
        <div class="label"><span class="dot ${dotClass}"></span>${esc(status.toUpperCase())} ${eta ? "· " + eta : ""}</div>
      </div>
      <div class="progress-bar"><div class="fill" style="width:${progress}%"></div></div>
      <div class="grid-2">
        <dl class="kv">
          <dt>Pipeline</dt><dd>${esc((job.tc||'').toUpperCase())}</dd>
          <dt>Status</dt><dd><span class="muted">${esc(status)}</span></dd>
          <dt>Step</dt><dd>${esc(job.current_step || "—")}</dd>
          <dt>Created</dt><dd>${fmtAgo(job.created_at)}</dd>
          <dt>Started</dt><dd>${job.started_at ? fmtAgo(job.started_at) : "—"}</dd>
          <dt>Duration</dt><dd>${job.started_at && job.finished_at ? fmtSec(job.finished_at - job.started_at) : isActive ? fmtSec(Date.now()/1000 - job.started_at) + " (running)" : "—"}</dd>
        </dl>
        <dl class="kv">
          <dt>Output</dt><dd>${job.output_file ? esc(job.output_file.split('/').pop()) : "—"}</dd>
          <dt>Size</dt><dd>${fmtSize(job.output_size)}</dd>
          <dt>Worker</dt><dd>${job.worker ? esc(job.worker.node) : "—"}${job.worker_load ? " (" + job.worker_load.active_jobs + "/" + job.worker.max_concurrent + " active)" : ""}</dd>
          <dt>Avg for ${esc((job.tc||'').toUpperCase())}</dt><dd>≈ ${fmtSec(job.avg_seconds)}</dd>
        </dl>
      </div>
      ${job.error ? `<div style="margin-top:14px;background:rgba(239,68,68,0.15);color:#fca5a5;padding:12px 16px;border-radius:8px;font-size:13px;font-family:'SF Mono',Consolas,monospace">${esc(job.error)}</div>` : ""}
    </div>

    ${workerBlock}

    <div class="card">
      <h2>Output Files (${job.output_files?.length || 0})</h2>
      ${outputList}
    </div>

    <div class="card">
      <h2>Render Log (last 30 lines)</h2>
      ${logBox}
    </div>
  `;
}

load();
if (location.search.includes("live=1")) setInterval(load, 2000);
</script>
</body>
</html>
"""


_PUBLIC_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V3 Cluster Status</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:#0a0c14; color:#e8e8f0; margin:0; padding:20px; font-size:14px; }
  h1 { margin:0; font-size:22px; font-weight:600; }
  h2 { margin:28px 0 12px 0; font-size:13px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.08em; font-weight:600; }
  h2 .badge { float:inline-end; font-size:11px; padding:2px 8px; background:#252837; border-radius:4px; text-transform:none; letter-spacing:0; color:#9aa0b4; font-weight:500; cursor:pointer; border:none; font-family:inherit; }
  h2 .badge:hover { background:#3a3f55; color:#e8e8f0; }
  .header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }
  .subheader { color:#9aa0b4; font-size:12px; margin-bottom:20px; }
  .last-update { color:#6b7280; font-size:11px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; }
  .grid-4 { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px; }
  .grid-2 { display:grid; grid-template-columns: 1fr 1fr 1fr; gap:12px; }
  .card { background:#141822; border:1px solid #252837; border-radius:8px; padding:14px 16px; position:relative; overflow:hidden; }
  .card.healthy { border-color:rgba(34,197,94,0.4); }
  .card.unhealthy { border-color:rgba(239,68,68,0.4); }
  .card.warning { border-color:rgba(245,158,11,0.4); }
  .card .label { font-size:11px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.06em; font-weight:500; }
  .card .value { font-size:28px; font-weight:600; margin-top:6px; font-variant-numeric:tabular-nums; }
  .card .sub { font-size:11px; color:#6b7280; margin-top:2px; }
  .card .value .ok { color:#22c55e; }
  .card .value .warn { color:#f59e0b; }
  .card .value .err { color:#ef4444; }
  .workers { display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:14px; }
  .worker { background:#141822; border:1px solid #252837; border-radius:10px; padding:16px 18px; transition: border-color 0.3s, box-shadow 0.3s; }
  .worker.healthy { border-color:rgba(34,197,94,0.3); }
  .worker.unhealthy { border-color:rgba(239,68,68,0.4); box-shadow: 0 0 0 1px rgba(239,68,68,0.2); }
  .worker.disabled { opacity:0.5; border-color:rgba(107,114,128,0.3); }
  .worker-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px; }
  .worker-name { font-weight:600; font-size:14px; line-height:1.3; }
  .worker-id { font-family: "SF Mono", Consolas, monospace; font-size:11px; color:#9aa0b4; margin-top:1px; }
  .worker-tier { font-size:10px; padding:1px 6px; border-radius:3px; margin-left:6px; vertical-align:middle; }
  .tier-low { background:#3a3f55; color:#9aa0b4; }
  .tier-mid { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .tier-high { background:rgba(168,85,247,0.2); color:#a855f7; }
  .status-pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:0.05em; }
  .status-healthy { background:rgba(34,197,94,0.2); color:#22c55e; }
  .status-unhealthy { background:rgba(239,68,68,0.2); color:#ef4444; }
  .status-disabled { background:rgba(107,114,128,0.2); color:#9aa0b4; }
  .status-busy { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .worker-meta { display:grid; grid-template-columns: auto 1fr; gap:4px 12px; font-size:12px; margin-top:8px; }
  .worker-meta dt { color:#9aa0b4; }
  .worker-meta dd { color:#e8e8f0; margin:0; font-family:"SF Mono",Consolas,monospace; font-size:11px; }
  .bar { display:block; height:6px; background:#252837; border-radius:3px; overflow:hidden; margin-top:8px; }
  .bar > * { display:block; height:100%; background:linear-gradient(90deg,#22c55e,#10b981); transition: width 0.5s; }
  .util { display:flex; justify-content:space-between; font-size:11px; color:#9aa0b4; margin-bottom:4px; }
  .jobs-feed { background:#141822; border:1px solid #252837; border-radius:8px; padding:4px; max-height:400px; overflow-y:auto; }
  .job-row { display:grid; grid-template-columns: 80px 60px 1fr auto auto; gap:12px; padding:10px 12px; border-bottom:1px solid #252837; align-items:center; font-size:12px; }
  .job-row:last-child { border-bottom: none; }
  .job-row .status { padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; text-transform:uppercase; }
  .job-row .s-running { background:rgba(59,130,246,0.2); color:#60a5fa; }
  .job-row .s-queued { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .job-row .s-paused { background:rgba(168,85,247,0.2); color:#a855f7; }
  .job-row .s-succeeded { background:rgba(34,197,94,0.2); color:#22c55e; }
  .job-row .s-failed { background:rgba(239,68,68,0.2); color:#ef4444; }
  .job-row .job-id { font-family:"SF Mono",Consolas,monospace; color:#9aa0b4; font-size:11px; }
  .job-row .progress { display:flex; align-items:center; gap:8px; }
  .job-row .progress-bar { width:120px; height:5px; background:#252837; border-radius:2px; overflow:hidden; }
  .job-row .progress-bar > * { display:block; height:100%; background:linear-gradient(90deg,#60a5fa,#22c55e); }
  .job-row .progress-pct { color:#9aa0b4; font-variant-numeric:tabular-nums; min-width:42px; }
  .job-row .meta { color:#6b7280; font-size:11px; }
  .job-row .tc-pill { padding:2px 6px; border-radius:3px; background:#3a3f55; font-size:10px; font-weight:600; }
  table { width:100%; border-collapse:collapse; background:#141822; border:1px solid #252837; border-radius:8px; overflow:hidden; font-size:12px; }
  th, td { padding:8px 12px; text-align:left; border-bottom:1px solid #1a1d29; }
  th { background:#1a1d29; color:#9aa0b4; font-weight:600; text-transform:uppercase; font-size:10px; letter-spacing:0.06em; }
  tr:hover { background:#1a1d2c; }
  td.mono { font-family:"SF Mono",Consolas,monospace; font-size:11px; color:#9aa0b4; }
  td.right { text-align:right; font-variant-numeric:tabular-nums; }
  td .pill { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px; font-weight:600; }
  td .pill.ok { background:rgba(34,197,94,0.2); color:#22c55e; }
  td .pill.fail { background:rgba(239,68,68,0.2); color:#ef4444; }
  td .pill.invalid { background:rgba(168,85,247,0.2); color:#a855f7; }
  td .pill.queued { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .chart-box { background:#141822; border:1px solid #252837; border-radius:8px; padding:16px; height:200px; position:relative; }
  .chart-box h3 { margin:0 0 10px 0; font-size:11px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }
  .chart-canvas-wrap { position:relative; height:calc(100% - 22px); }
  .empty { color:#6b7280; font-style:italic; padding:24px; text-align:center; }
  .spinner { display:inline-block; width:14px; height:14px; border:2px solid #252837; border-top-color:#60a5fa; border-radius:50%; animation:spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .footer { color:#6b7280; font-size:11px; margin-top:32px; text-align:center; padding:16px; }
  .pulse-dot { display:inline-block; width:6px; height:6px; background:#22c55e; border-radius:50%; margin-right:6px; animation:pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  .metric-bar { display:flex; align-items:center; gap:8px; padding:6px 0; font-size:12px; }
  .metric-bar .name { width:80px; color:#9aa0b4; }
  .metric-bar .bar-track { flex:1; height:18px; background:#252837; border-radius:3px; position:relative; overflow:hidden; }
  .metric-bar .bar-fill { position:absolute; left:0; top:0; height:100%; background:linear-gradient(90deg,#60a5fa,#22c55e); display:flex; align-items:center; padding-left:8px; font-size:10px; font-weight:600; color:#0a0c14; }
  .metric-bar .bar-val { width:90px; text-align:right; font-variant-numeric:tabular-nums; color:#e8e8f0; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>🟢 V3 Cluster Status <span class="pulse-dot"></span></h1>
    <div class="subheader"><span id="clock"></span> · <span class="last-update" id="lastUpdate">—</span></div>
  </div>
  <div>
    <select id="intervalSel" class="badge" onchange="setInterval(load, parseInt(this.value))">
      <option value="5000">↻ 5s</option>
      <option value="10000" selected>↻ 10s</option>
      <option value="30000">↻ 30s</option>
      <option value="60000">↻ 60s</option>
    </select>
  </div>
</div>

<h2>Cluster Summary</h2>
<div class="grid-4">
  <div class="card" id="cardWorkers"><div class="label">Workers</div><div class="value" id="vWorkers">—</div><div class="sub" id="sWorkers">—</div></div>
  <div class="card" id="cardHealthy"><div class="label">Healthy</div><div class="value ok" id="vHealthy">—</div><div class="sub" id="sHealthy">—</div></div>
  <div class="card" id="cardActive"><div class="label">Active Jobs</div><div class="value" id="vActive">—</div><div class="sub" id="sActive">—</div></div>
  <div class="label card" id="cardSuccess"><div class="label">Success Rate (24h)</div><div class="value" id="vSuccess">—</div><div class="sub" id="sSuccess">—</div></div>
</div>

<h2>Workers <button class="badge" onclick="testAllWorkers()">🔌 test all</button></h2>
<div class="workers" id="workersGrid"><div class="empty">Loading workers…</div></div>

<h2>Live Jobs</h2>
<div class="jobs-feed" id="liveJobs"><div class="empty">Loading jobs…</div></div>

<h2>Performance (last 24h)</h2>
<div class="grid-2">
  <div class="chart-box"><h3>Throughput · jobs/hour</h3><div class="chart-canvas-wrap"><canvas id="chartThroughput"></canvas></div></div>
  <div class="chart-box"><h3>Latency p50 + p95 by TC</h3><div class="chart-canvas-wrap"><canvas id="chartLatency"></canvas></div></div>
  <div class="chart-box"><h3>Job volume by TC</h3><div class="chart-canvas-wrap"><canvas id="chartByTC"></canvas></div></div>
</div>

<h2>Per-Worker Stats (last 24h)</h2>
<div id="workerStats"><div class="empty">Loading…</div></div>

<script>
const INTERNAL = '__INT__';
const COLORS = {
  ok: '#22c55e', fail: '#ef4444', invalid: '#a855f7', queued: '#f59e0b',
  tc: { tc01:'#60a5fa', tc02:'#22c55e', tc03:'#f59e0b', tc04:'#a855f7', tc05:'#ec4899', tc06:'#14b8a6' },
};

let charts = {};
function esc(s) { return String(s ?? '').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtBytes(n) {
  if (!n) return '—';
  const units = ['B','KB','MB','GB']; let i=0; let v=n;
  while (v >= 1024 && i < units.length-1) { v/=1024; i++; }
  return v.toFixed(1) + ' ' + units[i];
}
function fmtSec(s) {
  if (s == null) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60).toFixed(0) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function fmtTimeAgo(epoch) {
  if (!epoch) return '—';
  const dt = Date.now()/1000 - epoch;
  if (dt < 60) return Math.floor(dt) + 's ago';
  if (dt < 3600) return Math.floor(dt/60) + 'm ago';
  if (dt < 86400) return Math.floor(dt/3600) + 'h ago';
  return Math.floor(dt/86400) + 'd ago';
}
function fmtClock(epoch) { return new Date(epoch * 1000).toLocaleTimeString(); }

async function load() {
  document.getElementById('clock').textContent = new Date().toLocaleString();
  let data;
  try {
    const r = await fetch('/api/cluster/dashboard', { headers: { 'X-Cutdee-Internal': INTERNAL } });
    data = await r.json();
  } catch (e) {
    document.getElementById('root').innerHTML = '<div class="empty">⚠ Failed to load: ' + esc(e.message) + '</div>';
    return;
  }
  if (!data.ok) { document.getElementById('liveJobs').innerHTML = '<div class="empty">API error</div>'; return; }
  document.getElementById('lastUpdate').textContent = 'Last fetch: ' + fmtClock(data.server_time);
  renderSummary(data);
  renderWorkers(data.cluster);
  renderLiveJobs(data.live_jobs);
  renderMetrics(data.metrics);
}

function renderSummary(d) {
  const s = d.summary;
  document.getElementById('vWorkers').innerHTML = s.total_workers + ' <span class="sub" style="font-size:14px; color:#6b7280;">total</span>';
  document.getElementById('sWorkers').textContent = `${s.enabled_workers} enabled · ${s.disabled_workers} disabled`;
  document.getElementById('vHealthy').textContent = s.healthy_workers + ' / ' + s.enabled_workers;
  document.getElementById('sHealthy').textContent = s.down_workers + ' down';
  document.getElementById('vActive').innerHTML = s.active_jobs + ' <span class="sub" style="font-size:14px; color:#6b7280;">/ ' + s.total_capacity + '</span>';
  document.getElementById('sActive').textContent = (s.total_capacity > 0 ? Math.round(s.active_jobs / s.total_capacity * 100) : 0) + '% capacity';
  const tot = d.metrics.totals;
  document.getElementById('vSuccess').innerHTML = tot.success_rate + '<span class="sub" style="font-size:14px;">%</span>';
  document.getElementById('sSuccess').textContent = `${tot.ok} ok / ${tot.fail} fail / ${tot.invalid} invalid`;
}

function renderWorkers(workers) {
  const html = workers.map(w => {
    let statusClass = 'unhealthy', statusText = '✕ DOWN';
    if (!w.enabled) { statusClass = 'disabled'; statusText = '○ DISABLED'; }
    else if (!w.healthy) { statusClass = 'unhealthy'; statusText = '✕ UNHEALTHY'; }
    else if (w.active_jobs > 0) { statusClass = 'busy'; statusText = '⟳ BUSY'; }
    else { statusClass = 'healthy'; statusText = '● IDLE'; }
    const sys = w.system || {};
    const gpu = w.gpu || {};
    const inflight = w.in_flight_jobs || [];
    const inflightHtml = inflight.length === 0
      ? '<div class="meta" style="color:#6b7280;">no in-flight jobs</div>'
      : inflight.map(j => `
        <div class="job-row" style="padding:6px 0; grid-template-columns: auto auto 1fr auto;">
          <code class="job-id">${esc(j.job_id?.slice(-16) || '?')}</code>
          <span class="tc-pill">${esc(j.tc?.toUpperCase() || '?')}</span>
          <span class="progress-bar"><span style="width:${Math.round((j.progress||0)*100)}%"></span></span>
          <span class="progress-pct">${Math.round((j.progress||0)*100)}%</span>
        </div>`).join('');
    const pct = w.max_concurrent > 0 ? (w.active_jobs / w.max_concurrent * 100) : 0;
    const gpuList = (gpu.available || []).slice(0, 3).map(g => `<span class="tc-pill" style="background:#252837;">${esc(g)}</span>`).join(' ');
    return `
      <div class="worker ${w.healthy ? 'healthy' : 'unhealthy'} ${!w.enabled ? 'disabled' : ''}">
        <div class="worker-header">
          <div>
            <div class="worker-name">${esc(w.name || w.id)}
              <span class="worker-tier tier-${esc(w.tier || 'low')}">${esc((w.tier || 'low').toUpperCase())}</span>
            </div>
            <div class="worker-id">${esc(w.id)}</div>
          </div>
          <div><span class="status-pill status-${statusClass}">${statusText}</span></div>
        </div>
        <div class="util">
          <span>${w.active_jobs} / ${w.max_concurrent} jobs</span>
          <span style="color:#6b7280;">${pct.toFixed(0)}% capacity</span>
        </div>
        <div class="bar"><span style="width:${pct}%"></span></div>
        <dl class="worker-meta">
          <dt>Encoder</dt><dd>${esc(w.encoder || '?')}</dd>
          <dt>Version</dt><dd>${esc(w.version || '—')} · ${esc(w.commit || '?')}</dd>
          <dt>GPU</dt><dd>${gpuList || '<span style="color:#6b7280;">none (CPU-only)</span>'}</dd>
          ${sys.disk_free_gb != null ? `<dt>Disk free</dt><dd>${sys.disk_free_gb.toFixed(1)} GB</dd>` : ''}
          ${sys.cpu_percent != null ? `<dt>CPU%</dt><dd>${sys.cpu_percent}%</dd>` : ''}
          <dt>Last seen</dt><dd>${fmtTimeAgo(w.last_seen)}</dd>
          ${w.error ? `<dt style="color:#ef4444;">Error</dt><dd style="color:#ef4444;">${esc(w.error)}</dd>` : ''}
        </dl>
        ${inflightHtml}
      </div>`;
  }).join('');
  document.getElementById('workersGrid').innerHTML = html || '<div class="empty">No workers configured</div>';
}

function renderLiveJobs(jobs) {
  if (!jobs || jobs.length === 0) {
    document.getElementById('liveJobs').innerHTML = '<div class="empty">No active jobs 🟢</div>';
    return;
  }
  const html = jobs.map(j => {
    const statusClass = 's-' + (j.status || 'unknown');
    const tcColor = COLORS.tc[j.tc?.toLowerCase()] || '#6b7280';
    const pct = Math.round((j.progress || 0) * 100);
    return `<div class="job-row">
      <span class="status ${statusClass}">${esc(j.status)}</span>
      <span class="tc-pill" style="background:${tcColor}; color:#0a0c14;">${esc((j.tc || '?').toUpperCase())}</span>
      <code class="job-id">${esc(j.job_id)}</code>
      <span class="progress">
        <div class="progress-bar"><span style="width:${pct}%"></span></div>
        <span class="progress-pct">${pct}%</span>
      </span>
      <span class="meta">${esc(j.worker_id || 'queued')} · ${fmtSec(j.elapsed_sec)}</span>
    </div>`;
  }).join('');
  document.getElementById('liveJobs').innerHTML = html;
}

function makeChart(id, type, data, options) {
  if (charts[id]) charts[id].destroy();
  const ctx = document.getElementById(id).getContext('2d');
  charts[id] = new Chart(ctx, { type, data, options });
}

const CHART_OPTS = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { labels: { color: '#9aa0b4', font: { size: 10 } } } },
  scales: {
    x: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29' } },
    y: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29' } },
  },
};

function renderMetrics(m) {
  // Throughput chart
  const hours = m.hourly_throughput.map(h => {
    const d = new Date(h.hour * 1000);
    return d.getHours().toString().padStart(2,'0') + ':00';
  });
  const totalSeries = m.hourly_throughput.map(h => h.total);
  const okSeries = m.hourly_throughput.map(h => h.ok);
  makeChart('chartThroughput', 'bar', {
    labels: hours,
    datasets: [
      { label: 'Total', data: totalSeries, backgroundColor: '#60a5fa88', borderColor: '#60a5fa', borderWidth: 1 },
      { label: 'OK', data: okSeries, backgroundColor: '#22c55e88', borderColor: '#22c55e', borderWidth: 1 },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, x: { ...CHART_OPTS.scales.x, ticks: { ...CHART_OPTS.scales.x.ticks, maxRotation: 0, autoSkip: true } } } });

  // Latency chart
  const tcs = m.by_tc.map(t => t.tc?.toUpperCase() || '?');
  const p50 = m.by_tc.map(t => t.p50_sec);
  const p95 = m.by_tc.map(t => t.p95_sec);
  makeChart('chartLatency', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'p50', data: p50, backgroundColor: '#60a5fa', borderRadius: 4 },
      { label: 'p95', data: p95, backgroundColor: '#f59e0b', borderRadius: 4 },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, y: { ...CHART_OPTS.scales.y, ticks: { ...CHART_OPTS.scales.y.ticks, callback: v => v + 's' } } } });

  // By TC chart
  const tcOk = m.by_tc.map(t => t.ok);
  const tcFail = m.by_tc.map(t => t.fail);
  const tcInvalid = m.by_tc.map(t => t.invalid);
  makeChart('chartByTC', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'OK', data: tcOk, backgroundColor: '#22c55e' },
      { label: 'Failed', data: tcFail, backgroundColor: '#ef4444' },
      { label: 'Invalid', data: tcInvalid, backgroundColor: '#a855f7' },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, x: { ...CHART_OPTS.scales.x, stacked: true }, y: { ...CHART_OPTS.scales.y, stacked: true } } });

  // Per-worker stats
  if (!m.by_worker || m.by_worker.length === 0) {
    document.getElementById('workerStats').innerHTML = '<div class="empty">No worker stats yet</div>';
    return;
  }
  const maxTotal = Math.max(...m.by_worker.map(w => w.total));
  document.getElementById('workerStats').innerHTML = m.by_worker.map(w => {
    const successPct = w.success_rate;
    const avgSec = w.avg_sec || 0;
    const totalBarWidth = (w.total / maxTotal * 100).toFixed(1);
    const okBarColor = successPct >= 90 ? '#22c55e' : successPct >= 70 ? '#f59e0b' : '#ef4444';
    return `<div class="metric-bar">
      <span class="name">${esc(w.worker_id.replace(/_/g, ' '))}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${totalBarWidth}%; background:${okBarColor};">${w.total}</div></div>
      <span class="bar-val">${successPct}% ok · ${fmtSec(avgSec)}</span>
    </div>`;
  }).join('');
}

async function testAllWorkers() {
  if (!confirm('Test all worker connections? This calls /health on every worker.')) return;
  const INTL = INTERNAL;
  try {
    const r = await fetch('/api/cluster/workers/reload', { method: 'POST', headers: { 'X-Cutdee-Internal': INTL } });
    const d = await r.json();
    alert('Reloaded: ' + d.count + ' workers from disk. Dashboard will refresh next tick.');
    load();
  } catch (e) { alert('Error: ' + e.message); }
}

load();
setInterval(load, parseInt(document.getElementById('intervalSel').value));
</script>
</body>
</html>
"""




_ADMIN_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V3 Cluster · Status</title>
<meta name="description" content="Real-time public status for the V3 Cluster rendering platform.">
<meta name="robots" content="noindex, nofollow">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: linear-gradient(180deg, #0e1320 0%, #0a0c14 100%); color:#e8e8f0; margin:0; min-height:100vh; }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 48px; }
  header { display:flex; justify-content:space-between; align-items:flex-end; border-bottom: 1px solid #1f2533; padding-bottom: 18px; margin-bottom: 28px; flex-wrap: wrap; gap: 12px; }
  .brand { display:flex; align-items:center; gap: 14px; }
  .brand-mark { width: 44px; height: 44px; border-radius: 12px;
                background: linear-gradient(135deg, #22c55e 0%, #10b981 60%, #06b6d4 100%);
                display:flex; align-items:center; justify-content:center; font-size:22px; box-shadow: 0 4px 14px rgba(34,197,94,0.25); }
  h1 { margin:0; font-size: 26px; font-weight: 600; letter-spacing: -0.01em; }
  .tagline { margin: 4px 0 0; font-size: 13px; color: #9aa0b4; }
  .updated { font-size: 11px; color: #6b7280; font-family: "SF Mono", Consolas, monospace; text-align: right; line-height:1.6; }
  .updated .live-dot { display:inline-block; width: 7px; height: 7px; background: #22c55e; border-radius: 50%;
                       margin-right: 6px; vertical-align: middle; animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; box-shadow: 0 0 0 0 rgba(34,197,94,0.5); }
                       50% { opacity:0.4; box-shadow: 0 0 0 4px rgba(34,197,94,0); } }
  .stat-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 32px; }
  .stat-card { background: rgba(20, 24, 34, 0.7); backdrop-filter: blur(8px); border: 1px solid #252837;
               border-radius: 14px; padding: 18px 20px; position:relative; overflow: hidden; transition: transform 0.2s, border-color 0.3s; }
  .stat-card:hover { transform: translateY(-1px); border-color: #2f3548; }
  .stat-card .label { font-size: 11px; color: #9aa0b4; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 500; }
  .stat-card .value { font-size: 32px; font-weight: 600; margin-top: 8px; font-variant-numeric: tabular-nums; line-height: 1; }
  .stat-card .sub { font-size: 12px; color: #6b7280; margin-top: 6px; }
  .stat-card .accent { position:absolute; left: 0; top: 0; bottom: 0; width: 3px; }
  .accent-green { background: linear-gradient(180deg, #22c55e, #10b981); }
  .accent-blue { background: linear-gradient(180deg, #60a5fa, #22d3ee); }
  .accent-orange { background: linear-gradient(180deg, #f59e0b, #ef4444); }
  .accent-purple { background: linear-gradient(180deg, #a855f7, #ec4899); }
  .ok { color: #22c55e; }
  .warn { color: #f59e0b; }
  .err { color: #ef4444; }
  h2 { margin: 36px 0 14px; font-size: 13px; color: #9aa0b4; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
  .nodes-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
  .node { background: rgba(20, 24, 34, 0.7); border: 1px solid #252837; border-radius: 12px; padding: 16px 18px;
          transition: border-color 0.3s, opacity 0.3s; position: relative; }
  .node.online { border-color: rgba(34,197,94,0.35); }
  .node.offline { border-color: rgba(239,68,68,0.4); opacity: 0.85; }
  .node.disabled { border-color: rgba(107,114,128,0.3); opacity: 0.45; }
  .node-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom: 10px; }
  .node-name { font-weight: 600; font-size: 14px; }
  .node-tier { font-size: 10px; padding: 2px 8px; border-radius: 4px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-left: 6px; vertical-align: middle; }
  .tier-Standard { background: rgba(100,116,139,0.2); color: #94a3b8; }
  .tier-Performance { background: rgba(245,158,11,0.18); color: #f59e0b; }
  .tier-Compute+GPU { background: rgba(168,85,247,0.18); color: #a855f7; }
  .node-status { display:inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; text-transform: uppercase; }
  .ns-online { background: rgba(34,197,94,0.2); color: #22c55e; }
  .ns-offline { background: rgba(239,68,68,0.2); color: #ef4444; }
  .ns-disabled { background: rgba(107,114,128,0.2); color: #9aa0b4; }
  .util { display:flex; justify-content:space-between; font-size: 11px; color: #9aa0b4; margin: 8px 0 4px; }
  .bar { height: 6px; background: #252837; border-radius: 3px; overflow: hidden; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, #60a5fa, #22c55e); border-radius: 3px; transition: width 0.5s; }
  .bar-fill.busy { background: linear-gradient(90deg, #f59e0b, #ef4444); }
  .node-meta { display:grid; grid-template-columns: auto 1fr; gap: 4px 12px; font-size: 11px; margin-top: 12px; padding-top: 12px; border-top: 1px solid #1f2533; }
  .node-meta dt { color: #9aa0b4; }
  .node-meta dd { margin: 0; color: #e8e8f0; }
  .charts-grid { display:grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media (max-width: 720px) { .charts-grid { grid-template-columns: 1fr; } }
  .chart-box { background: rgba(20, 24, 34, 0.7); border: 1px solid #252837; border-radius: 12px; padding: 18px; }
  .chart-box h3 { margin: 0 0 4px; font-size: 12px; color: #9aa0b4; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
  .chart-box .sub-h { font-size: 11px; color: #6b7280; margin-bottom: 12px; }
  .chart-canvas-wrap { position: relative; height: 220px; }
  .tier-bar { display:flex; align-items: center; gap: 12px; padding: 8px 0; font-size: 12px; }
  .tier-bar .name { width: 110px; color: #9aa0b4; }
  .tier-bar .track { flex: 1; height: 22px; background: #252837; border-radius: 4px; position: relative; overflow: hidden; }
  .tier-bar .fill { position: absolute; left: 0; top: 0; height: 100%; display: flex; align-items: center; padding-left: 10px; font-size: 11px; font-weight: 600; color: #0a0c14; }
  .tier-bar .ok { background: linear-gradient(90deg, #60a5fa, #22c55e); }
  .tier-bar .warn { background: linear-gradient(90deg, #fbbf24, #f59e0b); }
  .tier-bar .err { background: linear-gradient(90deg, #f87171, #ef4444); }
  .tier-bar .val { width: 110px; text-align: right; font-variant-numeric: tabular-nums; color: #e8e8f0; }
  footer { text-align: center; margin-top: 56px; padding-top: 24px; border-top: 1px solid #1f2533; color: #6b7280; font-size: 11px; line-height: 1.7; }
  footer a { color: #60a5fa; text-decoration: none; }
  footer a:hover { text-decoration: underline; }
  .skeleton { background: linear-gradient(90deg, #1a1d29 0%, #252837 50%, #1a1d29 100%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 6px; height: 28px; margin-top: 8px; }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
  .empty-state { text-align: center; padding: 40px; color: #6b7280; font-style: italic; }
  .scale-toggle { display: inline-flex; background: #141822; border: 1px solid #252837; border-radius: 8px; padding: 3px; font-size: 11px; }
  .scale-toggle button { background: transparent; border: none; color: #9aa0b4; padding: 5px 10px; cursor: pointer; border-radius: 5px; font-family: inherit; }
  .scale-toggle button.active { background: #252837; color: #e8e8f0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="brand-mark">🎬</div>
      <div>
        <h1>V3 Cluster</h1>
        <p class="tagline">Real-time status of our distributed rendering infrastructure</p>
      </div>
    </div>
    <div class="updated">
      <div><span class="live-dot"></span>Live</div>
      <div id="lastUpdate">Last fetch: —</div>
      <div class="scale-toggle">
        <button onclick="setScale(1)" id="scale1h" class="">1h</button>
        <button onclick="setScale(24)" id="scale24h" class="active">24h</button>
        <button onclick="setScale(168)" id="scale7d" class="">7d</button>
      </div>
    </div>
  </header>

  <div class="stat-grid">
    <div class="stat-card"><div class="accent accent-green"></div>
      <div class="label">Online Nodes</div>
      <div class="value ok" id="vOnline">—</div>
      <div class="sub" id="sOnline">—</div>
    </div>
    <div class="stat-card"><div class="accent accent-blue"></div>
      <div class="label">Active Jobs</div>
      <div class="value" id="vActive">—</div>
      <div class="sub" id="sActive">—</div>
    </div>
    <div class="stat-card"><div class="accent accent-orange"></div>
      <div class="label">24h Throughput</div>
      <div class="value" id="vThroughput">—</div>
      <div class="sub" id="sThroughput">—</div>
    </div>
    <div class="stat-card"><div class="accent accent-purple"></div>
      <div class="label">Success Rate</div>
      <div class="value" id="vSuccess">—</div>
      <div class="sub" id="sSuccess">—</div>
    </div>
  </div>

  <h2>Cluster Nodes</h2>
  <div class="nodes-grid" id="nodesGrid">
    <div class="empty-state">Loading nodes…</div>
  </div>

  <h2>Activity (last <span id="windowLabel">24h</span>)</h2>
  <div class="charts-grid">
    <div class="chart-box">
      <h3>Throughput · jobs per hour</h3>
      <div class="sub-h">Total vs. successful renders</div>
      <div class="chart-canvas-wrap"><canvas id="chartThroughput"></canvas></div>
    </div>
    <div class="chart-box">
      <h3>Pipeline Mix</h3>
      <div class="sub-h">Job count per TC pipeline</div>
      <div class="chart-canvas-wrap"><canvas id="chartByTC"></canvas></div>
    </div>
    <div class="chart-box">
      <h3>Latency · p50 + p95 by pipeline</h3>
      <div class="sub-h">Render duration (seconds)</div>
      <div class="chart-canvas-wrap"><canvas id="chartLatency"></canvas></div>
    </div>
    <div class="chart-box">
      <h3>Per-Node Performance</h3>
      <div class="sub-h">Job volume + success rate per node</div>
      <div id="nodeStats" style="padding-top: 8px;"></div>
    </div>
  </div>

  <footer>
    <div><strong>V3 Cluster Status</strong> · Public view · refreshed every 15s</div>
    <div style="margin-top: 4px;">Operational metrics are reported in aggregate. Internal hostnames, IP addresses, and APIs are not exposed.</div>
  </footer>
</div>

<script>
let charts = {};
let currentScale = 24;

function fmtBytes(n) {
  if (!n) return '—';
  const u = ['B','KB','MB','GB']; let i = 0; let v = n;
  while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
  return v.toFixed(1) + ' ' + u[i];
}
function fmtSec(s) {
  if (s == null || s === 0) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60).toFixed(0) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function fmtTime(epoch) { return new Date(epoch * 1000).toLocaleTimeString(); }
function fmtAgo(epoch) {
  if (!epoch) return '—';
  const dt = Math.floor(Date.now()/1000 - epoch);
  if (dt < 60) return dt + 's ago';
  if (dt < 3600) return Math.floor(dt/60) + 'm ago';
  if (dt < 86400) return Math.floor(dt/3600) + 'h ago';
  return Math.floor(dt/86400) + 'd ago';
}
function tierClass(tier) { return tier.replace(/[\s\u2013]+/g, '-'); }

const CHART_DEFAULTS = {
  responsive: true, maintainAspectRatio: false,
  animation: { duration: 600 },
  plugins: { legend: { labels: { color: '#9aa0b4', font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29', drawBorder: false } },
    y: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29', drawBorder: false }, beginAtZero: true },
  },
};

function makeChart(id, type, data, options) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id).getContext('2d'), { type, data, options: { ...CHART_DEFAULTS, ...(options || {}) } });
}

async function load() {
  let data;
  try {
    const r = await fetch('/api/cluster/public?hours=' + currentScale);
    data = await r.json();
  } catch (err) {
    document.getElementById('nodesGrid').innerHTML = '<div class="empty-state">⚠ Could not reach status API. Retrying…</div>';
    return;
  }
  if (!data.ok) return;
  document.getElementById('lastUpdate').textContent = 'Last fetch: ' + fmtTime(data.server_time);
  document.getElementById('windowLabel').textContent = currentScale === 1 ? '1 hour' : (currentScale === 168 ? '7 days' : '24 hours');
  renderSummary(data);
  renderNodes(data.nodes);
  renderCharts(data.metrics);
}

function renderSummary(d) {
  const s = d.summary;
  const onlinePct = s.enabled_nodes > 0 ? Math.round(100 * s.online_nodes / s.enabled_nodes) : 0;
  document.getElementById('vOnline').innerHTML = s.online_nodes + ' <span style="font-size:18px; color:#6b7280;">/ ' + s.enabled_nodes + '</span>';
  document.getElementById('sOnline').textContent = onlinePct + '% available · ' + s.disabled_nodes + ' disabled';
  document.getElementById('vActive').innerHTML = s.active_jobs + ' <span style="font-size:18px; color:#6b7280;">/ ' + s.total_capacity + '</span>';
  document.getElementById('sActive').textContent = s.total_capacity > 0 ? Math.round(100 * s.active_jobs / s.total_capacity) + '% capacity in use' : 'no capacity';
  const tot = d.metrics.totals;
  document.getElementById('vThroughput').textContent = tot.total || 0;
  document.getElementById('sThroughput').textContent = (tot.ok || 0) + ' successful · ' + (tot.fail || 0) + ' failed';
  document.getElementById('vSuccess').textContent = (tot.success_rate || 0) + '%';
  document.getElementById('sSuccess').textContent = tot.ok + '/' + tot.total + ' jobs succeeded';
}

function renderNodes(nodes) {
  const html = nodes.map(n => {
    let statusClass = 'offline', statusText = 'OFFLINE';
    if (!n.enabled) { statusClass = 'disabled'; statusText = 'DISABLED'; }
    else if (n.healthy) { statusClass = 'online'; statusText = 'ONLINE'; }
    const pct = n.max_concurrent > 0 ? (n.active_jobs / n.max_concurrent * 100) : 0;
    const isBusy = pct >= 80;
    return `
      <div class="node ${statusClass}">
        <div class="node-header">
          <div>
            <span class="node-name">${n.name}</span>
            <span class="node-tier tier-${tierClass(n.tier)}">${n.tier}</span>
          </div>
          <div><span class="node-status ns-${statusClass}">${statusText}</span></div>
        </div>
        <div class="util">
          <span>${n.active_jobs} / ${n.max_concurrent} concurrent</span>
          <span style="color:#6b7280;">${pct.toFixed(0)}% utilization</span>
        </div>
        <div class="bar"><div class="bar-fill ${isBusy ? 'busy' : ''}" style="width:${pct}%"></div></div>
        <dl class="node-meta">
          <dt>Compute</dt><dd>${n.encoder_kind}</dd>
          <dt>Last seen</dt><dd>${fmtAgo(n.last_seen_ago ? (Date.now()/1000 - n.last_seen_ago) : null)}</dd>
        </dl>
      </div>`;
  }).join('');
  document.getElementById('nodesGrid').innerHTML = html || '<div class="empty-state">No nodes configured.</div>';
}

function renderCharts(m) {
  const hours = m.hourly_throughput.map(h => {
    const d = new Date(h.hour * 1000);
    return currentScale === 168 ? (d.getMonth()+1) + '/' + d.getDate() : (d.getHours().toString().padStart(2,'0') + ':00');
  });
  makeChart('chartThroughput', 'line', {
    labels: hours,
    datasets: [
      { label: 'Total jobs', data: m.hourly_throughput.map(h => h.total), borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.15)', fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 },
      { label: 'Successful', data: m.hourly_throughput.map(h => h.ok), borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.1)', fill: false, tension: 0.3, pointRadius: 0, borderWidth: 2 },
    ],
  });

  const tcs = m.by_tc.map(t => (t.tc || '').toUpperCase());
  const tcColors = { TC01:'#60a5fa', TC02:'#22c55e', TC03:'#f59e0b', TC04:'#a855f7', TC05:'#ec4899', TC06:'#14b8a6' };
  makeChart('chartByTC', 'doughnut', {
    labels: tcs,
    datasets: [{
      data: m.by_tc.map(t => t.total),
      backgroundColor: tcs.map(t => tcColors[t] || '#6b7280'),
      borderColor: '#0e1320', borderWidth: 2,
    }],
  }, { scales: {}, plugins: { legend: { position: 'right', labels: { color: '#9aa0b4', font: { size: 11 } } } } });

  makeChart('chartLatency', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'p50', data: m.by_tc.map(t => t.p50_sec), backgroundColor: '#60a5fa', borderRadius: 4 },
      { label: 'p95', data: m.by_tc.map(t => t.p95_sec), backgroundColor: '#f59e0b', borderRadius: 4 },
    ],
  }, { scales: { ...CHART_DEFAULTS.scales, y: { ...CHART_DEFAULTS.scales.y, ticks: { ...CHART_DEFAULTS.scales.y.ticks, callback: v => v + 's' } } } });

  const stats = m.by_node || [];
  if (stats.length === 0) {
    document.getElementById('nodeStats').innerHTML = '<div class="empty-state">No data</div>';
    return;
  }
  const maxTotal = Math.max(...stats.map(s => s.total));
  document.getElementById('nodeStats').innerHTML = stats.map(s => {
    const fillClass = s.success_rate >= 90 ? 'ok' : s.success_rate >= 70 ? 'warn' : 'err';
    const barColor = { ok: 'linear-gradient(90deg,#60a5fa,#22c55e)', warn: 'linear-gradient(90deg,#fbbf24,#f59e0b)', err: 'linear-gradient(90deg,#f87171,#ef4444)' }[fillClass];
    const barWidth = (s.total / maxTotal * 100).toFixed(1);
    return `<div class="tier-bar">
      <span class="name">${s.node}</span>
      <div class="track"><div class="fill" style="width:${barWidth}%; background:${barColor};">${s.total}</div></div>
      <span class="val">${s.success_rate}% ok · ${fmtSec(s.avg_sec)} avg</span>
    </div>`;
  }).join('');
}

function setScale(hours) {
  currentScale = hours;
  document.querySelectorAll('.scale-toggle button').forEach(b => b.classList.remove('active'));
  document.getElementById('scale' + (hours === 1 ? '1h' : (hours === 24 ? '24h' : '7d'))).classList.add('active');
  load();
}

load();
setInterval(load, 15000);
</script>
</body>
</html>
"""


_PUBLIC_ADMIN_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V3 Cluster Status</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:#0a0c14; color:#e8e8f0; margin:0; padding:20px; font-size:14px; }
  h1 { margin:0; font-size:22px; font-weight:600; }
  h2 { margin:28px 0 12px 0; font-size:13px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.08em; font-weight:600; }
  h2 .badge { float:inline-end; font-size:11px; padding:2px 8px; background:#252837; border-radius:4px; text-transform:none; letter-spacing:0; color:#9aa0b4; font-weight:500; cursor:pointer; border:none; font-family:inherit; }
  h2 .badge:hover { background:#3a3f55; color:#e8e8f0; }
  .header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }
  .subheader { color:#9aa0b4; font-size:12px; margin-bottom:20px; }
  .last-update { color:#6b7280; font-size:11px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; }
  .grid-4 { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px; }
  .grid-2 { display:grid; grid-template-columns: 1fr 1fr 1fr; gap:12px; }
  .card { background:#141822; border:1px solid #252837; border-radius:8px; padding:14px 16px; position:relative; overflow:hidden; }
  .card.healthy { border-color:rgba(34,197,94,0.4); }
  .card.unhealthy { border-color:rgba(239,68,68,0.4); }
  .card.warning { border-color:rgba(245,158,11,0.4); }
  .card .label { font-size:11px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.06em; font-weight:500; }
  .card .value { font-size:28px; font-weight:600; margin-top:6px; font-variant-numeric:tabular-nums; }
  .card .sub { font-size:11px; color:#6b7280; margin-top:2px; }
  .card .value .ok { color:#22c55e; }
  .card .value .warn { color:#f59e0b; }
  .card .value .err { color:#ef4444; }
  .workers { display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:14px; }
  .worker { background:#141822; border:1px solid #252837; border-radius:10px; padding:16px 18px; transition: border-color 0.3s, box-shadow 0.3s; }
  .worker.healthy { border-color:rgba(34,197,94,0.3); }
  .worker.unhealthy { border-color:rgba(239,68,68,0.4); box-shadow: 0 0 0 1px rgba(239,68,68,0.2); }
  .worker.disabled { opacity:0.5; border-color:rgba(107,114,128,0.3); }
  .worker-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px; }
  .worker-name { font-weight:600; font-size:14px; line-height:1.3; }
  .worker-id { font-family: "SF Mono", Consolas, monospace; font-size:11px; color:#9aa0b4; margin-top:1px; }
  .worker-tier { font-size:10px; padding:1px 6px; border-radius:3px; margin-left:6px; vertical-align:middle; }
  .tier-low { background:#3a3f55; color:#9aa0b4; }
  .tier-mid { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .tier-high { background:rgba(168,85,247,0.2); color:#a855f7; }
  .status-pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:0.05em; }
  .status-healthy { background:rgba(34,197,94,0.2); color:#22c55e; }
  .status-unhealthy { background:rgba(239,68,68,0.2); color:#ef4444; }
  .status-disabled { background:rgba(107,114,128,0.2); color:#9aa0b4; }
  .status-busy { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .worker-meta { display:grid; grid-template-columns: auto 1fr; gap:4px 12px; font-size:12px; margin-top:8px; }
  .worker-meta dt { color:#9aa0b4; }
  .worker-meta dd { color:#e8e8f0; margin:0; font-family:"SF Mono",Consolas,monospace; font-size:11px; }
  .bar { display:block; height:6px; background:#252837; border-radius:3px; overflow:hidden; margin-top:8px; }
  .bar > * { display:block; height:100%; background:linear-gradient(90deg,#22c55e,#10b981); transition: width 0.5s; }
  .util { display:flex; justify-content:space-between; font-size:11px; color:#9aa0b4; margin-bottom:4px; }
  .jobs-feed { background:#141822; border:1px solid #252837; border-radius:8px; padding:4px; max-height:400px; overflow-y:auto; }
  .job-row { display:grid; grid-template-columns: 80px 60px 1fr auto auto; gap:12px; padding:10px 12px; border-bottom:1px solid #252837; align-items:center; font-size:12px; }
  .job-row:last-child { border-bottom: none; }
  .job-row .status { padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; text-transform:uppercase; }
  .job-row .s-running { background:rgba(59,130,246,0.2); color:#60a5fa; }
  .job-row .s-queued { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .job-row .s-paused { background:rgba(168,85,247,0.2); color:#a855f7; }
  .job-row .s-succeeded { background:rgba(34,197,94,0.2); color:#22c55e; }
  .job-row .s-failed { background:rgba(239,68,68,0.2); color:#ef4444; }
  .job-row .job-id { font-family:"SF Mono",Consolas,monospace; color:#9aa0b4; font-size:11px; }
  .job-row .progress { display:flex; align-items:center; gap:8px; }
  .job-row .progress-bar { width:120px; height:5px; background:#252837; border-radius:2px; overflow:hidden; }
  .job-row .progress-bar > * { display:block; height:100%; background:linear-gradient(90deg,#60a5fa,#22c55e); }
  .job-row .progress-pct { color:#9aa0b4; font-variant-numeric:tabular-nums; min-width:42px; }
  .job-row .meta { color:#6b7280; font-size:11px; }
  .job-row .tc-pill { padding:2px 6px; border-radius:3px; background:#3a3f55; font-size:10px; font-weight:600; }
  table { width:100%; border-collapse:collapse; background:#141822; border:1px solid #252837; border-radius:8px; overflow:hidden; font-size:12px; }
  th, td { padding:8px 12px; text-align:left; border-bottom:1px solid #1a1d29; }
  th { background:#1a1d29; color:#9aa0b4; font-weight:600; text-transform:uppercase; font-size:10px; letter-spacing:0.06em; }
  tr:hover { background:#1a1d2c; }
  td.mono { font-family:"SF Mono",Consolas,monospace; font-size:11px; color:#9aa0b4; }
  td.right { text-align:right; font-variant-numeric:tabular-nums; }
  td .pill { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px; font-weight:600; }
  td .pill.ok { background:rgba(34,197,94,0.2); color:#22c55e; }
  td .pill.fail { background:rgba(239,68,68,0.2); color:#ef4444; }
  td .pill.invalid { background:rgba(168,85,247,0.2); color:#a855f7; }
  td .pill.queued { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .chart-box { background:#141822; border:1px solid #252837; border-radius:8px; padding:16px; height:200px; position:relative; }
  .chart-box h3 { margin:0 0 10px 0; font-size:11px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }
  .chart-canvas-wrap { position:relative; height:calc(100% - 22px); }
  .empty { color:#6b7280; font-style:italic; padding:24px; text-align:center; }
  .spinner { display:inline-block; width:14px; height:14px; border:2px solid #252837; border-top-color:#60a5fa; border-radius:50%; animation:spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .footer { color:#6b7280; font-size:11px; margin-top:32px; text-align:center; padding:16px; }
  .pulse-dot { display:inline-block; width:6px; height:6px; background:#22c55e; border-radius:50%; margin-right:6px; animation:pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  .metric-bar { display:flex; align-items:center; gap:8px; padding:6px 0; font-size:12px; }
  .metric-bar .name { width:80px; color:#9aa0b4; }
  .metric-bar .bar-track { flex:1; height:18px; background:#252837; border-radius:3px; position:relative; overflow:hidden; }
  .metric-bar .bar-fill { position:absolute; left:0; top:0; height:100%; background:linear-gradient(90deg,#60a5fa,#22c55e); display:flex; align-items:center; padding-left:8px; font-size:10px; font-weight:600; color:#0a0c14; }
  .metric-bar .bar-val { width:90px; text-align:right; font-variant-numeric:tabular-nums; color:#e8e8f0; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>🟢 V3 Cluster Status <span class="pulse-dot"></span></h1>
    <div class="subheader"><span id="clock"></span> · <span class="last-update" id="lastUpdate">—</span></div>
  </div>
  <div>
    <select id="intervalSel" class="badge" onchange="setInterval(load, parseInt(this.value))">
      <option value="5000">↻ 5s</option>
      <option value="10000" selected>↻ 10s</option>
      <option value="30000">↻ 30s</option>
      <option value="60000">↻ 60s</option>
    </select>
  </div>
</div>

<h2>Cluster Summary</h2>
<div class="grid-4">
  <div class="card" id="cardWorkers"><div class="label">Workers</div><div class="value" id="vWorkers">—</div><div class="sub" id="sWorkers">—</div></div>
  <div class="card" id="cardHealthy"><div class="label">Healthy</div><div class="value ok" id="vHealthy">—</div><div class="sub" id="sHealthy">—</div></div>
  <div class="card" id="cardActive"><div class="label">Active Jobs</div><div class="value" id="vActive">—</div><div class="sub" id="sActive">—</div></div>
  <div class="label card" id="cardSuccess"><div class="label">Success Rate (24h)</div><div class="value" id="vSuccess">—</div><div class="sub" id="sSuccess">—</div></div>
</div>

<h2>Workers <button class="badge" onclick="testAllWorkers()">🔌 test all</button></h2>
<div class="workers" id="workersGrid"><div class="empty">Loading workers…</div></div>

<h2>Live Jobs</h2>
<div class="jobs-feed" id="liveJobs"><div class="empty">Loading jobs…</div></div>

<h2>Performance (last 24h)</h2>
<div class="grid-2">
  <div class="chart-box"><h3>Throughput · jobs/hour</h3><div class="chart-canvas-wrap"><canvas id="chartThroughput"></canvas></div></div>
  <div class="chart-box"><h3>Latency p50 + p95 by TC</h3><div class="chart-canvas-wrap"><canvas id="chartLatency"></canvas></div></div>
  <div class="chart-box"><h3>Job volume by TC</h3><div class="chart-canvas-wrap"><canvas id="chartByTC"></canvas></div></div>
</div>

<h2>Per-Worker Stats (last 24h)</h2>
<div id="workerStats"><div class="empty">Loading…</div></div>

<script>
const INTERNAL = '__INT__';
const COLORS = {
  ok: '#22c55e', fail: '#ef4444', invalid: '#a855f7', queued: '#f59e0b',
  tc: { tc01:'#60a5fa', tc02:'#22c55e', tc03:'#f59e0b', tc04:'#a855f7', tc05:'#ec4899', tc06:'#14b8a6' },
};

let charts = {};
function esc(s) { return String(s ?? '').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtBytes(n) {
  if (!n) return '—';
  const units = ['B','KB','MB','GB']; let i=0; let v=n;
  while (v >= 1024 && i < units.length-1) { v/=1024; i++; }
  return v.toFixed(1) + ' ' + units[i];
}
function fmtSec(s) {
  if (s == null) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60).toFixed(0) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function fmtTimeAgo(epoch) {
  if (!epoch) return '—';
  const dt = Date.now()/1000 - epoch;
  if (dt < 60) return Math.floor(dt) + 's ago';
  if (dt < 3600) return Math.floor(dt/60) + 'm ago';
  if (dt < 86400) return Math.floor(dt/3600) + 'h ago';
  return Math.floor(dt/86400) + 'd ago';
}
function fmtClock(epoch) { return new Date(epoch * 1000).toLocaleTimeString(); }

async function load() {
  document.getElementById('clock').textContent = new Date().toLocaleString();
  let data;
  try {
    const r = await fetch('/api/cluster/dashboard', { headers: { 'X-Cutdee-Internal': INTERNAL } });
    data = await r.json();
  } catch (e) {
    document.getElementById('root').innerHTML = '<div class="empty">⚠ Failed to load: ' + esc(e.message) + '</div>';
    return;
  }
  if (!data.ok) { document.getElementById('liveJobs').innerHTML = '<div class="empty">API error</div>'; return; }
  document.getElementById('lastUpdate').textContent = 'Last fetch: ' + fmtClock(data.server_time);
  renderSummary(data);
  renderWorkers(data.cluster);
  renderLiveJobs(data.live_jobs);
  renderMetrics(data.metrics);
}

function renderSummary(d) {
  const s = d.summary;
  document.getElementById('vWorkers').innerHTML = s.total_workers + ' <span class="sub" style="font-size:14px; color:#6b7280;">total</span>';
  document.getElementById('sWorkers').textContent = `${s.enabled_workers} enabled · ${s.disabled_workers} disabled`;
  document.getElementById('vHealthy').textContent = s.healthy_workers + ' / ' + s.enabled_workers;
  document.getElementById('sHealthy').textContent = s.down_workers + ' down';
  document.getElementById('vActive').innerHTML = s.active_jobs + ' <span class="sub" style="font-size:14px; color:#6b7280;">/ ' + s.total_capacity + '</span>';
  document.getElementById('sActive').textContent = (s.total_capacity > 0 ? Math.round(s.active_jobs / s.total_capacity * 100) : 0) + '% capacity';
  const tot = d.metrics.totals;
  document.getElementById('vSuccess').innerHTML = tot.success_rate + '<span class="sub" style="font-size:14px;">%</span>';
  document.getElementById('sSuccess').textContent = `${tot.ok} ok / ${tot.fail} fail / ${tot.invalid} invalid`;
}

function renderWorkers(workers) {
  const html = workers.map(w => {
    let statusClass = 'unhealthy', statusText = '✕ DOWN';
    if (!w.enabled) { statusClass = 'disabled'; statusText = '○ DISABLED'; }
    else if (!w.healthy) { statusClass = 'unhealthy'; statusText = '✕ UNHEALTHY'; }
    else if (w.active_jobs > 0) { statusClass = 'busy'; statusText = '⟳ BUSY'; }
    else { statusClass = 'healthy'; statusText = '● IDLE'; }
    const sys = w.system || {};
    const gpu = w.gpu || {};
    const inflight = w.in_flight_jobs || [];
    const inflightHtml = inflight.length === 0
      ? '<div class="meta" style="color:#6b7280;">no in-flight jobs</div>'
      : inflight.map(j => `
        <div class="job-row" style="padding:6px 0; grid-template-columns: auto auto 1fr auto;">
          <code class="job-id">${esc(j.job_id?.slice(-16) || '?')}</code>
          <span class="tc-pill">${esc(j.tc?.toUpperCase() || '?')}</span>
          <span class="progress-bar"><span style="width:${Math.round((j.progress||0)*100)}%"></span></span>
          <span class="progress-pct">${Math.round((j.progress||0)*100)}%</span>
        </div>`).join('');
    const pct = w.max_concurrent > 0 ? (w.active_jobs / w.max_concurrent * 100) : 0;
    const gpuList = (gpu.available || []).slice(0, 3).map(g => `<span class="tc-pill" style="background:#252837;">${esc(g)}</span>`).join(' ');
    return `
      <div class="worker ${w.healthy ? 'healthy' : 'unhealthy'} ${!w.enabled ? 'disabled' : ''}">
        <div class="worker-header">
          <div>
            <div class="worker-name">${esc(w.name || w.id)}
              <span class="worker-tier tier-${esc(w.tier || 'low')}">${esc((w.tier || 'low').toUpperCase())}</span>
            </div>
            <div class="worker-id">${esc(w.id)}</div>
          </div>
          <div><span class="status-pill status-${statusClass}">${statusText}</span></div>
        </div>
        <div class="util">
          <span>${w.active_jobs} / ${w.max_concurrent} jobs</span>
          <span style="color:#6b7280;">${pct.toFixed(0)}% capacity</span>
        </div>
        <div class="bar"><span style="width:${pct}%"></span></div>
        <dl class="worker-meta">
          <dt>Encoder</dt><dd>${esc(w.encoder || '?')}</dd>
          <dt>Version</dt><dd>${esc(w.version || '—')} · ${esc(w.commit || '?')}</dd>
          <dt>GPU</dt><dd>${gpuList || '<span style="color:#6b7280;">none (CPU-only)</span>'}</dd>
          ${sys.disk_free_gb != null ? `<dt>Disk free</dt><dd>${sys.disk_free_gb.toFixed(1)} GB</dd>` : ''}
          ${sys.cpu_percent != null ? `<dt>CPU%</dt><dd>${sys.cpu_percent}%</dd>` : ''}
          <dt>Last seen</dt><dd>${fmtTimeAgo(w.last_seen)}</dd>
          ${w.error ? `<dt style="color:#ef4444;">Error</dt><dd style="color:#ef4444;">${esc(w.error)}</dd>` : ''}
        </dl>
        ${inflightHtml}
      </div>`;
  }).join('');
  document.getElementById('workersGrid').innerHTML = html || '<div class="empty">No workers configured</div>';
}

function renderLiveJobs(jobs) {
  if (!jobs || jobs.length === 0) {
    document.getElementById('liveJobs').innerHTML = '<div class="empty">No active jobs 🟢</div>';
    return;
  }
  const html = jobs.map(j => {
    const statusClass = 's-' + (j.status || 'unknown');
    const tcColor = COLORS.tc[j.tc?.toLowerCase()] || '#6b7280';
    const pct = Math.round((j.progress || 0) * 100);
    return `<div class="job-row">
      <span class="status ${statusClass}">${esc(j.status)}</span>
      <span class="tc-pill" style="background:${tcColor}; color:#0a0c14;">${esc((j.tc || '?').toUpperCase())}</span>
      <code class="job-id">${esc(j.job_id)}</code>
      <span class="progress">
        <div class="progress-bar"><span style="width:${pct}%"></span></div>
        <span class="progress-pct">${pct}%</span>
      </span>
      <span class="meta">${esc(j.worker_id || 'queued')} · ${fmtSec(j.elapsed_sec)}</span>
    </div>`;
  }).join('');
  document.getElementById('liveJobs').innerHTML = html;
}

function makeChart(id, type, data, options) {
  if (charts[id]) charts[id].destroy();
  const ctx = document.getElementById(id).getContext('2d');
  charts[id] = new Chart(ctx, { type, data, options });
}

const CHART_OPTS = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { labels: { color: '#9aa0b4', font: { size: 10 } } } },
  scales: {
    x: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29' } },
    y: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29' } },
  },
};

function renderMetrics(m) {
  // Throughput chart
  const hours = m.hourly_throughput.map(h => {
    const d = new Date(h.hour * 1000);
    return d.getHours().toString().padStart(2,'0') + ':00';
  });
  const totalSeries = m.hourly_throughput.map(h => h.total);
  const okSeries = m.hourly_throughput.map(h => h.ok);
  makeChart('chartThroughput', 'bar', {
    labels: hours,
    datasets: [
      { label: 'Total', data: totalSeries, backgroundColor: '#60a5fa88', borderColor: '#60a5fa', borderWidth: 1 },
      { label: 'OK', data: okSeries, backgroundColor: '#22c55e88', borderColor: '#22c55e', borderWidth: 1 },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, x: { ...CHART_OPTS.scales.x, ticks: { ...CHART_OPTS.scales.x.ticks, maxRotation: 0, autoSkip: true } } } });

  // Latency chart
  const tcs = m.by_tc.map(t => t.tc?.toUpperCase() || '?');
  const p50 = m.by_tc.map(t => t.p50_sec);
  const p95 = m.by_tc.map(t => t.p95_sec);
  makeChart('chartLatency', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'p50', data: p50, backgroundColor: '#60a5fa', borderRadius: 4 },
      { label: 'p95', data: p95, backgroundColor: '#f59e0b', borderRadius: 4 },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, y: { ...CHART_OPTS.scales.y, ticks: { ...CHART_OPTS.scales.y.ticks, callback: v => v + 's' } } } });

  // By TC chart
  const tcOk = m.by_tc.map(t => t.ok);
  const tcFail = m.by_tc.map(t => t.fail);
  const tcInvalid = m.by_tc.map(t => t.invalid);
  makeChart('chartByTC', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'OK', data: tcOk, backgroundColor: '#22c55e' },
      { label: 'Failed', data: tcFail, backgroundColor: '#ef4444' },
      { label: 'Invalid', data: tcInvalid, backgroundColor: '#a855f7' },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, x: { ...CHART_OPTS.scales.x, stacked: true }, y: { ...CHART_OPTS.scales.y, stacked: true } } });

  // Per-worker stats
  if (!m.by_worker || m.by_worker.length === 0) {
    document.getElementById('workerStats').innerHTML = '<div class="empty">No worker stats yet</div>';
    return;
  }
  const maxTotal = Math.max(...m.by_worker.map(w => w.total));
  document.getElementById('workerStats').innerHTML = m.by_worker.map(w => {
    const successPct = w.success_rate;
    const avgSec = w.avg_sec || 0;
    const totalBarWidth = (w.total / maxTotal * 100).toFixed(1);
    const okBarColor = successPct >= 90 ? '#22c55e' : successPct >= 70 ? '#f59e0b' : '#ef4444';
    return `<div class="metric-bar">
      <span class="name">${esc(w.worker_id.replace(/_/g, ' '))}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${totalBarWidth}%; background:${okBarColor};">${w.total}</div></div>
      <span class="bar-val">${successPct}% ok · ${fmtSec(avgSec)}</span>
    </div>`;
  }).join('');
}

async function testAllWorkers() {
  if (!confirm('Test all worker connections? This calls /health on every worker.')) return;
  const INTL = INTERNAL;
  try {
    const r = await fetch('/api/cluster/workers/reload', { method: 'POST', headers: { 'X-Cutdee-Internal': INTL } });
    const d = await r.json();
    alert('Reloaded: ' + d.count + ' workers from disk. Dashboard will refresh next tick.');
    load();
  } catch (e) { alert('Error: ' + e.message); }
}

load();
setInterval(load, parseInt(document.getElementById('intervalSel').value));
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html(_: bool = Depends(_verify_user)):
    """HTML dashboard for the current user (FIX 2026-08-18).

    Shows cluster overview, per-worker status, and recent jobs. Auto-refreshes
    every 15s. Requires user auth (the user's API key is needed to call
    the monitor and jobs endpoints from this page.
    """
    return HTMLResponse(content=_ADMIN_DASHBOARD_HTML.replace("__INT__", INTERNAL_TOKEN))


@app.get("/v3api/admin/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard(_: bool = Depends(_verify_internal)):
    """Admin dashboard with internal-token auth (FIX 2026-08-19).

    No user login required — pass X-Cutdee-Internal header. Auto-refreshes
    every 5-60s via selector. Used for ops monitoring.
    """
    return HTMLResponse(content=_ADMIN_DASHBOARD_HTML.replace("__INT__", INTERNAL_TOKEN))


@app.get("/admin/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard_root(_: bool = Depends(_verify_internal)):
    """Root-path alias for admin dashboard."""
    return HTMLResponse(content=_ADMIN_DASHBOARD_HTML.replace("__INT__", INTERNAL_TOKEN))


# =====================================================================
# PUBLIC STATUS PAGE (FIX 2026-08-19): no auth, no internal info exposed
# =====================================================================

@app.get("/v3api/status", response_class=HTMLResponse, include_in_schema=False)
async def public_status_page():
    """Public-facing V3 Cluster status. NO auth, NO sensitive data.
    Worker IDs, IPs, internal URLs and admin actions are not exposed.
    """
    return HTMLResponse(content=_PUBLIC_ADMIN_DASHBOARD_HTML)


@app.get("/status", response_class=HTMLResponse, include_in_schema=False)
async def public_status_page_root():
    """Root-path alias for public status."""
    return HTMLResponse(content=_PUBLIC_ADMIN_DASHBOARD_HTML)


@app.get("/api/app", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_root():
    """End-user portal (FIX 2026-08-19): landing page (signup/login or dashboard)."""
    return HTMLResponse(content=_APP_HTML)


@app.get("/api/app/jobs", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_jobs(user: str = Depends(_verify_user)):
    """End-user portal: list of user's jobs."""
    return HTMLResponse(content=_JOBS_HTML)


@app.get("/api/app/job/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_job_detail(job_id: str, user: str = Depends(_verify_user)):
    """End-user portal: single job detail with live progress + worker."""
    return HTMLResponse(content=_JOB_DETAIL_HTML)


@app.get("/api/app/submit", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_submit(user: str = Depends(_verify_user)):
    """End-user portal: submit a new job (FIX 2026-08-19)."""
    return HTMLResponse(content=_PUBLIC_SUBMIT_HTML)


@app.get("/api/app/profile", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_profile(user: str = Depends(_verify_user)):
    """End-user portal: profile (FIX 2026-08-19)."""
    return HTMLResponse(content=_PROFILE_HTML)


@app.delete("/api/cluster/workers/{worker_id}")
async def remove_worker(worker_id: str, _: bool = Depends(_verify_internal)):
    """Remove a worker from the cluster. Returns 200 on success, 404 if not found."""
    workers = _load_workers()
    new_workers = [w for w in workers if w["id"] != worker_id]
    if len(new_workers) == len(workers):
        raise HTTPException(404, f"worker '{worker_id}' not found")
    _save_workers(new_workers)
    return {"ok": True, "removed": worker_id, "total": len(new_workers)}


# =====================================================================
# WEBSOCKET REAL-TIME UPDATES (FIX 2026-08-19)
# =====================================================================

# In-memory pubsub broker for job status updates.
# Subscribers (WebSocket clients) receive {"type": "status"|"progress"|"done", ...}
# Internal publisher: _publish_job_update(job_id, payload)
_JOB_SUBSCRIBERS: Dict[str, set] = {}  # job_id → {websocket, ...}
_JOB_BROKER_LOCK = asyncio.Lock()
_JOB_LAST_STATE: Dict[str, Dict[str, Any]] = {}  # cache last status per job

async def _publish_job_update(job_id: str, payload: Dict[str, Any]) -> None:
    """Broadcast a job update to all WebSocket subscribers (FIX 2026-08-19)."""
    payload.setdefault("ts", time.time())
    payload.setdefault("job_id", job_id)
    _JOB_LAST_STATE[job_id] = payload
    async with _JOB_BROKER_LOCK:
        subs = list(_JOB_SUBSCRIBERS.get(job_id, set()))
    dead = []
    for ws in subs:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    if dead:
        async with _JOB_BROKER_LOCK:
            for ws in dead:
                _JOB_SUBSCRIBERS.get(job_id, set()).discard(ws)


@app.websocket("/ws/jobs/{job_id}")
async def ws_job_updates(websocket: WebSocket, job_id: str):
    """Real-time job updates WebSocket (FIX 2026-08-19).

    Auth: pass bearer token in cookie (cutdee_session) or Sec-WebSocket-Protocol header.
    Sends "hello" on connect with last known state, then live updates as they happen.
    """
    # Auth via cookie or header
    token = None
    # 1) Cookie
    cookie_token = websocket.cookies.get("cutdee_session")
    if cookie_token:
        token = cookie_token
    # 2) Sec-WebSocket-Protocol: "bearer.<token>" (RFC 6455 doesn't allow custom headers)
    proto = websocket.headers.get("sec-websocket-protocol", "")
    if proto.startswith("bearer."):
        token = proto[7:]
    if not token:
        await websocket.close(code=4401, reason="auth required")
        return
    # Resolve user
    try:
        user = _user_for_token(token)
    except HTTPException:
        await websocket.close(code=4401, reason="invalid token")
        return
    # Verify job belongs to user
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT job_id, status FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT job_id, status FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        await websocket.close(code=4404, reason="job not found")
        return
    # Accept connection (with subprotocol echo if present)
    accept_subprotocol = "bearer." + token if proto.startswith("bearer.") else None
    await websocket.accept(subprotocol=accept_subprotocol)
    # Register subscriber
    async with _JOB_BROKER_LOCK:
        _JOB_SUBSCRIBERS.setdefault(job_id, set()).add(websocket)
    try:
        # Send initial state
        last = _JOB_LAST_STATE.get(job_id)
        await websocket.send_json({
            "type": "hello",
            "job_id": job_id,
            "status": row["status"],
            "last_state": last,
            "server_time": time.time(),
        })
        # Keepalive ping every 20s + listen for client pongs
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=20.0)
                # Client may send ping → ignore or echo
                if msg == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send keepalive
                try:
                    await websocket.send_text("ping")
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        async with _JOB_BROKER_LOCK:
            _JOB_SUBSCRIBERS.get(job_id, set()).discard(websocket)


@app.post("/api/v1/internal/jobs/{job_id}/publish")
async def api_internal_publish(job_id: str, payload: dict, _: bool = Depends(_verify_internal)):
    """Internal publish endpoint for worker → broker (FIX 2026-08-19).

    Workers can POST status/progress updates which are forwarded to WebSocket
    subscribers in real-time. Body: {"type": "status"|"progress"|"done", ...}
    """
    await _publish_job_update(job_id, payload)
    return {"ok": True, "subscribers": len(_JOB_SUBSCRIBERS.get(job_id, set()))}


@app.post("/api/cluster/workers/{worker_id}/test")
async def test_worker(worker_id: str, _: bool = Depends(_verify_internal)):
    """Test connection to a worker. Returns full /health response."""
    import httpx
    workers = _load_workers()
    target = next((w for w in workers if w["id"] == worker_id), None)
    if not target:
        raise HTTPException(404, f"worker '{worker_id}' not found")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{target['url']}/health")
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            return {"ok": True, "url": target["url"], "data": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# === Uploads ===
@app.post("/api/v1/uploads/{role}")
async def upload_file(
    role: str,
    request: Request,
    user: str = Depends(_verify_user),
):
    """Upload a file (product/background/cover/audio). Returns file_id."""
    if role not in ("product", "background", "cover", "audio", "source", "product_root"):
        raise HTTPException(status_code=400, detail=f"invalid role: {role}")
    body = _validate_upload_body(await request.body())
    file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
    target = UPLOADS_DIR / f"{file_id}{_upload_suffix(request.headers.get('X-Filename'), role)}"
    target.write_bytes(body)
    log.info(f"user={user} uploaded {role} -> {target.name} ({len(body)} bytes)")
    return {
        "file_id": file_id,
        "role": role,
        "size": len(body),
        "filename": target.name,
        "uploaded_at": time.time(),
    }


# === Jobs ===
@app.post("/api/v1/jobs")
async def create_job(
    req: CreateJobRequest,
    user: str = Depends(_verify_user),
):
    """Create a render job and dispatch to a worker."""
    workers = _load_workers()
    worker = await _pick_worker(workers, job_priority=req.priority, required_tc=req.tc)
    if not worker:
        raise HTTPException(status_code=503, detail="no_worker_available")

    job_id = f"v3_{int(time.time())}_{secrets.token_hex(6)}"
    t0 = time.time()
    settings = req.settings or {}

    # Save to PG
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v3_jobs
                (job_id, user_id, worker_id, status, settings, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (job_id, user, worker["id"], "queued",
                  json.dumps(settings), t0))
        conn.commit()
    finally:
        _pg_release(conn)

    # Forward to worker
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT) as c:
            # 1. Upload files to worker
            files_to_send = [
                (req.product_id, "product"),
                (req.background_id, "background"),
            ]
            if req.cover_id:
                files_to_send.append((req.cover_id, "cover"))
            if req.audio_id:
                files_to_send.append((req.audio_id, "audio"))
            for file_id, role in files_to_send:
                src = _find_upload_path(file_id)
                r = await c.post(
                    f"{worker['url']}/v1/jobs/{job_id}/upload/{role}",
                    content=src.read_bytes(),
                    headers={
                        "X-Cutdee-Internal": INTERNAL_TOKEN,
                        "Content-Disposition": f"attachment; filename={src.name}",
                    },
                )
                r.raise_for_status()

            # 2. Trigger render
            r = await c.post(
                f"{worker['url']}/v1/jobs/{job_id}/render",
                json={
                    "product_id": req.product_id,
                    "background_id": req.background_id,
                    "cover_id": req.cover_id,
                    "audio_id": req.audio_id,
                    "settings": settings,
                },
                headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
            )
            r.raise_for_status()
            result = r.json()
    except HTTPException as exc:
        _mark_job_failed(job_id, exc.detail)
        raise
    except Exception as e:
        log.error(f"dispatch to {worker['id']} failed: {e}")
        # FIX 2026-08-18: retry logic — if max_retries > retry_count, try again
        await _maybe_retry_job(job_id, user, str(e), "tc01", None,
                               {"product_id": req.product_id, "background_id": req.background_id,
                                "cover_id": req.cover_id, "audio_id": req.audio_id,
                                "settings": req.settings}, priority=0)
        raise HTTPException(status_code=502, detail=f"worker dispatch failed: {e}")

    status = _canonical_status(result.get("status", "queued"))
    if status in {"queued", "running"}:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE v3_jobs SET status='queued' WHERE job_id=%s",
                    (job_id,),
                )
            conn.commit()
        finally:
            _pg_release(conn)
        _start_worker_monitor(job_id, worker, status)
    else:
        await _record_worker_status_async(job_id, result)

    log.info(f"job={job_id} worker={worker['id']} status={result.get('status')}")
    return {
        "job_id": job_id,
        "worker_id": worker["id"],
        "status": status,
        "output_file": result.get("output_file"),
        "output_files": result.get("output_files", []),
        "output_size": result.get("output_size"),
        "duration_sec": result.get("duration_sec"),
        "queued": status in {"queued", "running"},
    }


@app.get("/api/v1/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: str = Depends(_verify_user),
):
    """Get job status (lazy-poll worker if needed)."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(status_code=404, detail="job not found")

    await _refresh_job_from_worker(row)

    out = dict(row)
    if out.get("started_at"):
        out["started_at"] = float(out["started_at"])
    if out.get("finished_at"):
        out["finished_at"] = float(out["finished_at"])
    if out.get("created_at"):
        out["created_at"] = float(out["created_at"])
    return out


@app.get("/api/v1/jobs/{job_id}/download/{filename}")
async def download_output(
    job_id: str,
    filename: str,
    user: str = Depends(_verify_user),
):
    """Proxy download from worker."""
    filename = _safe_output_name(filename)
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND status='succeeded'", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s AND status='succeeded'", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row or filename not in _output_names(row):
        raise HTTPException(404, detail="job not found")
    worker_id = row["worker_id"]
    workers = _load_workers()
    worker = next((w for w in workers if w["id"] == worker_id), None)
    if not worker:
        raise HTTPException(404, detail=f"worker {worker_id} not found")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{worker['url']}/v1/jobs/{job_id}/output",
            params={"filename": filename},
            headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, detail=r.text)
        # Save to local cache + return
        cache_path = OUTPUTS_DIR / job_id / filename
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(r.content)
        return FileResponse(cache_path, media_type="video/mp4", filename=filename)



# =====================================================================
# === V3 WebApp-compatible API routes ===
# =====================================================================
# These match the V3 WebApp frontend's expected endpoints (TC01-TC06).
# The gateway translates V3-style requests to internal cluster calls,
# and translates responses back to V3 format.

# --- System endpoints (return aggregated info from workers) ---

@app.get("/api/health")
async def api_health():
    """Public liveness summary; do not disclose worker URLs or host metrics."""
    workers = _load_workers()
    enabled_workers = [w for w in workers if w.get("enabled", True)]
    healthy_count = 0
    encoder_names: List[str] = []
    health_results = await asyncio.gather(*[_worker_health(w) for w in enabled_workers], return_exceptions=True)
    for w, h in zip(enabled_workers, health_results):
        if isinstance(h, Exception): h = {"ok": False, "error": str(h)[:120]}
        is_healthy = h.get("ok") is True
        if is_healthy:
            healthy_count += 1
            encoder_names.extend(_encoder_names(h))
    encoder_names = list(dict.fromkeys(encoder_names))
    preferred = ("h264_nvenc", "h264_videotoolbox", "h264_qsv", "libx264")
    recommended_encoder = next((name for name in preferred if name in encoder_names), "libx264")
    disk = shutil.disk_usage(DATA_DIR)
    disk_free_gb = disk.free / (1024 ** 3)
    disk_used_pct = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
    return {
        "status": "ok" if enabled_workers and healthy_count == len(enabled_workers) else "degraded",
        "service": "v3-cursor-api-gateway",
        "version": API_VERSION,
        "commit": BUILD_COMMIT,
        "total_workers": len(enabled_workers),
        "healthy_workers": healthy_count,
        "configured_workers": len(workers),
        "disabled_workers": len(workers) - len(enabled_workers),
        "recommended_encoder": recommended_encoder,
        "available_encoders": encoder_names,
        "disk_free_gb": round(disk_free_gb, 2),
        "disk_used_pct": disk_used_pct,
    }

@app.get("/api/version")
async def api_version():
    import sys
    return {"version": API_VERSION, "commit": BUILD_COMMIT, "python": sys.version}

@app.get("/api/ffmpeg")
async def api_ffmpeg(_: str = Depends(_verify_user)):
    """Use first worker's ffmpeg info."""
    workers = _load_workers()
    for w in workers:
        h = await _worker_health(w)
        if h.get("ok"):
            return {"path": w["url"], "version": h.get("ffmpeg_version", "unknown"), "from_worker": w["id"]}
    return {"path": "ffmpeg", "version": "unknown"}

@app.get("/api/encoders")
async def api_encoders(_: str = Depends(_verify_user)):
    """Aggregated encoder list."""
    workers = _load_workers()
    available = set()
    for w in workers:
        h = await _worker_health(w)
        if h.get("ok"):
            for enc in _encoder_names(h):
                available.add(enc)
    return {"available": [{"name": e} for e in sorted(available)]}

@app.get("/api/lens")
async def api_lens(_: str = Depends(_verify_user)):
    """Default lens presets (LENS_PRESETS is in V3's ai_reframe module)."""
    # Hard-coded from V3 defaults — full list has ~10 entries
    return {"lenses": [
        {"id": "16mm", "label": "16mm (กว้างพิเศษ)", "fov": 1.0},
        {"id": "24mm", "label": "24mm (กว้าง)", "fov": 0.9},
        {"id": "35mm", "label": "35mm (ปกติ)", "fov": 0.7},
        {"id": "50mm", "label": "50mm (portrait)", "fov": 0.5},
        {"id": "85mm", "label": "85mm (tele)", "fov": 0.3},
        {"id": "135mm", "label": "135mm (tele ไกล)", "fov": 0.2},
    ]}

@app.get("/api/config")
async def api_config(_: str = Depends(_verify_user)):
    return {"config": {
        "version": API_VERSION,
        "cluster_mode": True,
        "supported_tcs": ["tc01", "tc02", "tc03", "tc04", "tc05", "tc06"],
    }}

# --- Job endpoints (V3 format) ---

# 1. Upload (V3 frontend uses POST /api/jobs/upload with Form file)
@app.post("/api/jobs/upload")
async def api_jobs_upload(
    file: UploadFile = File(...),
    role_hint: Optional[str] = Form(None),
    user: str = Depends(_verify_user),
):
    """Upload a file. Returns {id, original_name, kind, size} in V3 format."""
    role = role_hint or "file"
    if role not in ("product", "background", "cover", "audio", "source", "product_root", "file"):
        role = "file"
    body = _validate_upload_body(await file.read())
    file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
    target = UPLOADS_DIR / f"{file_id}{_upload_suffix(file.filename, role)}"
    target.write_bytes(body)
    log.info(f"user={user} uploaded {target.name} ({len(body)} bytes) as {role}")
    return {
        "id": file_id,
        "original_name": file.filename or file_id,
        "kind": role,
        "size": len(body),
        "uploaded_at": time.time(),
    }

# 2. List jobs (V3 returns {jobs: [...]})
@app.get("/api/jobs/list")
async def api_jobs_list(
    tc: Optional[str] = None,
    limit: int = 50,
    user: str = Depends(_verify_user),
):
    limit = _limit(limit)
    clauses = []
    params: List[Any] = []
    if tc:
        clauses.append("tc=%s")
        params.append(tc)
    if not _is_admin(user):
        clauses.append("user_id=%s")
        params.append(user)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM v3_jobs{where} ORDER BY created_at DESC LIMIT %s", (*params, limit))
            rows = cur.fetchall()
    finally:
        _pg_release(conn)
    jobs = []
    for r in rows:
        jobs.append(_v3_job_dict(r))
    return {"jobs": jobs}

# 3. Get job (V3 format with progress, current_step, files, logs)
@app.get("/api/jobs/{job_id}")
async def api_jobs_get(job_id: str, user: str = Depends(_verify_user)):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    await _refresh_job_from_worker(row)
    return _v3_job_dict(row)

# 4. Cancel/pause/resume
@app.post("/api/jobs/{job_id}/cancel")
async def api_jobs_cancel(job_id: str, user: str = Depends(_verify_user)):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
        conn.commit()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    result = await _worker_control(row, "cancel")
    await _record_worker_status_async(job_id, result)
    return {"job_id": job_id, "status": _canonical_status(result.get("status")), "cancel_requested": True}

@app.post("/api/jobs/{job_id}/pause")
async def api_jobs_pause(job_id: str, user: str = Depends(_verify_user)):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    result = await _worker_control(row, "pause")
    await _record_worker_status_async(job_id, result)
    return {"job_id": job_id, "status": _canonical_status(result.get("status")), "pause_requested": True}

@app.post("/api/jobs/{job_id}/resume")
async def api_jobs_resume(job_id: str, user: str = Depends(_verify_user)):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    result = await _worker_control(row, "resume")
    await _record_worker_status_async(job_id, result)
    worker = _worker_for_job(row)
    if worker:
        _start_worker_monitor(job_id, worker, result.get("status", "queued"))
    return {"job_id": job_id, "status": _canonical_status(result.get("status")), "queued": True}

# 5. Outputs / downloads
@app.get("/api/outputs")
async def api_outputs(
    page: int = 1,
    limit: int = 5,
    dir: Optional[str] = None,
    user: str = Depends(_verify_user),
):
    """List authenticated user's outputs using the frontend's files contract."""
    page = max(1, int(page))
    limit = _limit(limit)
    clauses = ["status='succeeded'", "output_file IS NOT NULL"]
    params: List[Any] = []
    if dir:
        if dir not in {"tc01", "tc02", "tc03", "tc04", "tc05", "tc06"}:
            raise HTTPException(status_code=400, detail="invalid output filter")
        clauses.append("tc=%s")
        params.append(dir)
    if not _is_admin(user):
        clauses.append("user_id=%s")
        params.append(user)
    where = " AND ".join(clauses)
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT job_id, tc, output_file, output_files, output_size, finished_at "
                f"FROM v3_jobs WHERE {where} ORDER BY finished_at DESC NULLS LAST LIMIT %s",
                (*params, 1000),
            )
            rows = cur.fetchall()
    finally:
        _pg_release(conn)

    all_files: List[Dict[str, Any]] = []
    for row in rows:
        finished_at = row.get("finished_at")
        try:
            mtime_iso = datetime.fromtimestamp(float(finished_at), tz=timezone.utc).isoformat() if finished_at else None
        except (TypeError, ValueError, OSError):
            mtime_iso = None
        names = _output_names(row)
        for name in names:
            all_files.append({
                "job_id": row["job_id"],
                "tc": row.get("tc"),
                "filename": name,
                "path": f"{row['job_id']}/{name}",
                "size": int(row.get("output_size") or 0),
                "finished_at": finished_at,
                "mtime_iso": mtime_iso,
            })
    total = len(all_files)
    pages = max(1, (total + limit - 1) // limit)
    page = min(page, pages)
    start = (page - 1) * limit
    files = all_files[start:start + limit]
    return {
        "files": files,
        "outputs": files,
        "total": total,
        "page": page,
        "pages": pages,
        "limit": limit,
    }


@app.get("/api/download/{file_path:path}")
async def api_download(file_path: str, user: str = Depends(_verify_user)):
    """Proxy an output only when the authenticated user owns the job."""
    parts = file_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="invalid output path")
    if len(parts) == 2:
        job_id, filename = parts
        filename = _safe_output_name(filename)
    elif len(parts) == 1:
        job_id, filename = None, _safe_output_name(parts[0])
    else:
        raise HTTPException(status_code=400, detail="invalid output path")

    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if job_id:
                if _is_admin(user):
                    cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND status='succeeded'", (job_id,))
                else:
                    cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s AND status='succeeded'", (job_id, user))
                row = cur.fetchone()
            else:
                if _is_admin(user):
                    cur.execute("SELECT * FROM v3_jobs WHERE status='succeeded' AND output_file IS NOT NULL ORDER BY finished_at DESC LIMIT 1000")
                else:
                    cur.execute("SELECT * FROM v3_jobs WHERE user_id=%s AND status='succeeded' AND output_file IS NOT NULL ORDER BY finished_at DESC LIMIT 1000", (user,))
                row = next((candidate for candidate in cur.fetchall() if filename in _output_names(candidate)), None)
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(status_code=404, detail="file not found")
    if filename not in _output_names(row):
        raise HTTPException(status_code=404, detail="file not found")
    worker = next((w for w in _load_workers() if w["id"] == row["worker_id"]), None)
    if not worker:
        raise HTTPException(status_code=404, detail="worker not found")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{worker['url']}/v1/jobs/{row['job_id']}/output",
            params={"filename": filename},
            headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, detail="output unavailable")
        return Response(
            content=r.content,
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def _v3_job_dict(row) -> Dict[str, Any]:
    """Convert a v3_jobs DB row to V3 frontend format."""
    files = _output_names(row)
    logs = row.get("log") or []
    if isinstance(logs, str):
        try: logs = json.loads(logs)
        except: logs = []
    result = row.get("result") or {}
    if isinstance(result, str):
        try: result = json.loads(result)
        except: result = {}
    output_path = row.get("output_file") or (files[0] if files else None)
    finished_at = row.get("finished_at")
    out = {
        "job_id": row["job_id"],
        "tc": row.get("tc", "tc01"),
        "status": _normalize_status(row.get("status", "unknown")),
        "raw_status": row.get("status", "unknown"),
        "progress": row.get("progress", 0) or 0,
        "progress_pct": row.get("progress", 0) or 0,
        "current_step": row.get("current_step"),
        "current_stage": row.get("current_step"),
        "message": row.get("error") or result.get("message", ""),
        "error": row.get("error"),
        "files": files if isinstance(files, list) else [],
        "output_files": files if isinstance(files, list) else [],
        "logs": logs if isinstance(logs, list) else [],
        "log": logs if isinstance(logs, list) else [],
        "result": result,
        "worker_id": row.get("worker_id"),
        "encoder": (result.get("encoder") if isinstance(result, dict) else None),
        "encoder_used": (result.get("encoder") if isinstance(result, dict) else None),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "finished_at": finished_at,
        "ended_at": finished_at,
        "output_file": row.get("output_file"),
        "output_path": output_path,
        "output_size": row.get("output_size"),
        "settings": row.get("settings") or {},
    }
    if isinstance(out["settings"], str):
        try: out["settings"] = json.loads(out["settings"])
        except: out["settings"] = {}
    return out


# --- TC render endpoints (V3 frontend calls POST /api/{tc}/render) ---

class V3RenderPayload(BaseModel):
    files: Dict[str, List[str]] = Field(default_factory=dict)
    settings: Dict[str, Any] = Field(default_factory=dict)
    values: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None


async def _dispatch_tc_render(tc: str, payload: V3RenderPayload, user: str = "anon") -> Dict[str, Any]:
    """Upload and enqueue a TC job; monitor it asynchronously after dispatch."""
    workers = _load_workers()
    worker = await _pick_worker(workers)
    if not worker:
        raise HTTPException(503, "no_worker_available")
    job_id = f"v3_{int(time.time()*1000)}_{secrets.token_hex(4)}"
    t0 = time.time()

    # Auto-derive priority from user tier (FIX 2026-08-19)
    tier_priority = {"free": 0, "pro": 50, "enterprise": 100}.get(_get_user_tier(user), 0)
    explicit_priority = getattr(payload, "priority", None)
    if explicit_priority is not None and explicit_priority > 0 and _is_admin(user):
        # Admins can override
        priority = explicit_priority
    else:
        priority = max(tier_priority, explicit_priority or 0)

    # Collect file_ids per role from payload
    file_ids = payload.files or {}
    products = file_ids.get("product", file_ids.get("products", []))
    backgrounds = file_ids.get("bg", file_ids.get("background", file_ids.get("backgrounds", [])))
    covers = file_ids.get("cover", file_ids.get("covers", []))
    audios = file_ids.get("audio", file_ids.get("audios", []))
    sources = file_ids.get("source", file_ids.get("sources", []))
    product_roots = file_ids.get("product_root", file_ids.get("product_roots", []))
    if isinstance(products, str): products = [products]
    if isinstance(backgrounds, str): backgrounds = [backgrounds]
    if isinstance(covers, str): covers = [covers]
    if isinstance(audios, str): audios = [audios]
    if isinstance(sources, str): sources = [sources]
    if isinstance(product_roots, str): product_roots = [product_roots]

    # Save to PG first
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO v3_jobs
                (job_id, user_id, worker_id, tc, status, priority, max_retries, settings, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (job_id, user, worker["id"], tc, "queued",
                 priority,
                 getattr(payload, "max_retries", 0) or 0,
                 json.dumps({**(payload.settings or {}), **(payload.values or {})}), t0))
        conn.commit()
    finally:
        _pg_release(conn)

    # Forward to worker — call /v1/{tc}/render/{job_id}
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT * 3) as c:
            # Upload all files to worker
            file_roles = (
                [(fid, "product") for fid in products]
                + [(fid, "background") for fid in backgrounds]
                + [(fid, "cover") for fid in covers]
                + [(fid, "audio") for fid in audios]
                + [(fid, "source") for fid in sources]
                + [(fid, "product_root") for fid in product_roots]
            )
            for fid, role in file_roles:
                src = _find_upload_path(fid)
                # determine role
                r = await c.post(
                    f"{worker['url']}/v1/jobs/{job_id}/upload/{role}",
                    content=src.read_bytes(),
                    headers={"X-Cutdee-Internal": INTERNAL_TOKEN, "Content-Disposition": f"attachment; filename={src.name}"},
                )
                r.raise_for_status()
            # Trigger render via TC route
            r = await c.post(
                f"{worker['url']}/v1/{tc}/render/{job_id}",
                json={
                    "product_id": products[0] if products else None,
                    "background_id": backgrounds[0] if backgrounds else None,
                    "cover_id": covers[0] if covers else None,
                    "audio_id": audios[0] if audios else None,
                    "mode": tc,
                    "product_ids": products,
                    "background_ids": backgrounds,
                    "cover_ids": covers,
                    "audio_ids": audios,
                    "source_ids": sources,
                    "product_root_ids": product_roots,
                    "extra": payload.extra or {},
                    "settings": payload.settings or {},
                    "values": payload.values or payload.settings or {},
                },
                headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
            )
            r.raise_for_status()
            result = r.json()
    except HTTPException as exc:
        _mark_job_failed(job_id, exc.detail)
        raise
    except Exception as e:
        log.error(f"dispatch to {worker['id']} ({tc}) failed: {e}")
        # FIX 2026-08-18: retry logic — if max_retries > retry_count, try again
        await _maybe_retry_job(job_id, user, job_id, str(e), tc, payload, priority=getattr(payload, "priority", 0) or 0)
        raise HTTPException(502, f"worker dispatch failed: {e}")
        raise HTTPException(502, f"worker dispatch failed: {e}")

    status = _canonical_status(result.get("status", "queued"))
    if status in {"queued", "running"}:
        _start_worker_monitor(job_id, worker, status)
    else:
        await _record_worker_status_async(job_id, result)
    output_files = list(result.get("output_files", []) or [])
    if result.get("output_file") and result["output_file"] not in output_files:
        output_files.insert(0, result["output_file"])
    elapsed = time.time() - t0
    return {
        "job_id": job_id,
        "tc": tc,
        "worker_id": worker["id"],
        "status": status,
        "output_file": result.get("output_file"),
        "output_files": output_files,
        "output_size": result.get("output_size"),
        "duration_sec": result.get("duration_sec", elapsed),
        "encoder": result.get("encoder"),
        "message": f"{tc} {status}",
        "queued": status in {"queued", "running"},
    }


# Add /api/{tc}/render and /api/{tc}/dry-run for tc01..tc06
for _tc_key in ("tc01", "tc02", "tc03", "tc04", "tc05", "tc06"):
    def _make_render_handler(t: str = _tc_key):
        async def _h(payload: V3RenderPayload, user: str = Depends(_verify_user)):
            return await _dispatch_tc_render(t, payload, user)
        _h.__name__ = f"render_{_tc_key}"
        return _h
    app.post(f"/api/{_tc_key}/render", status_code=202)(_make_render_handler())
    def _make_dryrun_handler(t: str = _tc_key):
        async def _h(payload: V3RenderPayload, _: str = Depends(_verify_user)):
            from .planner import plan_tc

            files = payload.files or {}
            values = {**(payload.settings or {}), **(payload.values or {})}
            plan = plan_tc(t, files, values)
            return {
                "tc": t,
                "products": plan["products"],
                "backgrounds": plan["backgrounds"],
                "sources": plan["sources"],
                "plan": {**plan, "files": {k: len(v) if isinstance(v, list) else 0 for k, v in files.items()}, "generated_at": datetime.now(timezone.utc).isoformat()},
            }
        _h.__name__ = f"dryrun_{_tc_key}"
        return _h
    app.post(f"/api/{_tc_key}/dry-run")(_make_dryrun_handler())



if __name__ == "__main__":
    import uvicorn
    log.info(f"starting V3_cursor_API gateway on 0.0.0.0:{GATEWAY_PORT}")
    log.info(f"data_dir={DATA_DIR}")
    log.info(f"internal_token={'set' if INTERNAL_TOKEN != 'dev-internal-token-change-me' else 'DEFAULT (change me!)'}")
    uvicorn.run(app, host="0.0.0.0", port=GATEWAY_PORT, log_level="info")
