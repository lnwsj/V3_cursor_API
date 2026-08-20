"""User CRUD + auth helpers (Phase 1.2 refactor)."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Response

from .db import pg_conn


# ---------------------------------------------------------------------------
# Session key cache (in-memory; cleared on restart)
# ---------------------------------------------------------------------------
SESSION_KEYS: Dict[str, str] = {}  # api_key → user_id

SESSION_COOKIE_NAME = "cutdee_session"
TIER_PRIORITY = {"free": 0, "pro": 50, "enterprise": 100}


def session_key_register(api_key: str, user_id: str) -> None:
    """Register a freshly issued API key for in-memory cookie auth."""
    SESSION_KEYS[api_key] = user_id


def session_key_clear(api_key: str) -> None:
    SESSION_KEYS.pop(api_key, None)


def session_key_clear_all() -> None:
    SESSION_KEYS.clear()


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-SHA256 (no external deps)."""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000, dklen=32)
    return "pbkdf2$120000$" + salt.hex() + "$" + key.hex()


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against PBKDF2 hash. Constant-time via hmac.compare_digest."""
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


def generate_api_key(user_id: str) -> str:
    """Generate a fresh API key for a user."""
    return f"cutdee_vdo_{user_id[:8]}_{secrets.token_hex(12)}"


# ---------------------------------------------------------------------------
# Email validation
# ---------------------------------------------------------------------------
def email_normalize(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


# ---------------------------------------------------------------------------
# Session cookie helper (for HTTP responses)
# ---------------------------------------------------------------------------
def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=30 * 24 * 60 * 60,  # 30 days
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------
def get_user_tier(user: str) -> str:
    """Get user's subscription tier (free / pro / enterprise)."""
    try:
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT tier FROM v3_users WHERE user_id = %s", (user,))
                row = cur.fetchone()
                return (row["tier"] if row else None) or "free"
    except Exception:
        return "free"


def is_admin(user: str) -> bool:
    return user == "admin"


def user_exists(user: str) -> bool:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM v3_users WHERE user_id = %s LIMIT 1", (user,))
            return cur.fetchone() is not None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, password_hash, role, monthly_quota, monthly_used,
                       display_name, api_key_prefix
                FROM v3_users WHERE lower(email) = lower(%s)
            """, (email,))
            return cur.fetchone()


def create_user(user_id: str, email: str, password_hash: str,
                display_name: str, api_key_hash: str, api_key_prefix: str,
                role: str = "user") -> None:
    now = time.time()
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v3_users
                    (user_id, api_key_hash, role, display_name, monthly_quota, monthly_used,
                     api_key_prefix, created_at, last_seen_at, last_reset_at,
                     email, password_hash, last_login_at, tier)
                VALUES (%s, %s, %s, %s, 100, 0, %s, %s, %s, %s, %s, %s, %s, 'free')
            """, (user_id, api_key_hash, role, display_name, api_key_prefix,
                  now, now, now, email, password_hash, now))
        conn.commit()


def update_last_login(user_id: str) -> None:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE v3_users SET last_login_at = %s WHERE user_id = %s",
                        (time.time(), user_id))
        conn.commit()


def update_user_profile(user_id: str, display_name: Optional[str] = None,
                       email: Optional[str] = None) -> None:
    updates = []
    values = []
    if display_name is not None:
        updates.append("display_name = %s")
        values.append(display_name.strip()[:100])
    if email is not None:
        updates.append("email = %s")
        values.append(email_normalize(email))
    if not updates:
        return
    values.append(user_id)
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE v3_users SET {', '.join(updates)} WHERE user_id = %s", values)
        conn.commit()


def change_password(user_id: str, new_password_hash: str) -> None:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE v3_users SET password_hash = %s WHERE user_id = %s",
                        (new_password_hash, user_id))
        conn.commit()


def update_user_last_seen(user_id: str) -> None:
    """Touch last_seen_at (auto-registers on first use for legacy public keys)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE v3_users SET last_seen_at = %s
                WHERE user_id = %s
            """, (time.time(), user_id))
        conn.commit()


def auto_register_user(user_id: str, api_key: str, role: str = "user",
                        monthly_quota: int = 100) -> None:
    """Insert a new user on first API key use (legacy public key path)."""
    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    api_key_prefix = api_key[:11] + "..." if len(api_key) > 11 else api_key
    now = time.time()
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v3_users
                    (user_id, api_key_hash, role, api_key_prefix, created_at,
                     last_seen_at, last_reset_at, monthly_quota, monthly_used)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
                ON CONFLICT (user_id) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at
            """, (user_id, api_key_hash, role, api_key_prefix, now, now, now, monthly_quota))
        conn.commit()


def auto_register_admin(api_key: str) -> None:
    """Insert/update admin user record (called on admin key first use)."""
    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    now = time.time()
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v3_users
                    (user_id, api_key_hash, role, api_key_prefix, created_at,
                     last_seen_at, last_reset_at, monthly_quota, monthly_used)
                VALUES ('admin', %s, 'admin', 'admin...', %s, %s, %s, 999999, 0)
                ON CONFLICT (user_id) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at
            """, (api_key_hash, now, now, now))
        conn.commit()


def get_user_full(user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch full user row (for /me, /profile endpoints)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, email, display_name, role, tier, monthly_quota, monthly_used,
                       monthly_quota_paid, api_key_prefix, created_at, last_seen_at, last_login_at
                FROM v3_users WHERE user_id = %s
            """, (user_id,))
            return cur.fetchone()


# ---------------------------------------------------------------------------
# Bearer token resolution (called from request dependency)
# ---------------------------------------------------------------------------
def _bearer_token(header_value: Optional[str]) -> str:
    """Extract token from 'Authorization: Bearer <token>' header value."""
    if not header_value:
        raise HTTPException(status_code=401, detail="Bearer API token required")
    token = header_value
    if token.lower().startswith("bearer "):
        token = token[7:]
    token = token.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer API token required")
    return token


def resolve_token_to_user(token: Optional[str], public_api_keys: Tuple[str, ...],
                         admin_api_key: str) -> str:
    """Resolve an API key to a user_id (3-tier lookup).

    1) Session cache (issued at signup/login)
    2) Static admin key from env
    3) Static public keys from env (legacy auto-register)
    """
    if not token:
        raise HTTPException(status_code=401, detail="invalid API token")
    # 1) Session cache
    if token in SESSION_KEYS:
        return SESSION_KEYS[token]
    # 2) Admin key
    if admin_api_key and hmac.compare_digest(token, admin_api_key):
        try:
            auto_register_admin(token)
        except Exception:
            pass
        return "admin"
    # 3) Public keys (legacy)
    if any(hmac.compare_digest(token, key) for key in public_api_keys):
        user_id = f"u_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:12]}"
        try:
            auto_register_user(user_id, token)
        except Exception:
            pass
        return user_id
    raise HTTPException(status_code=401, detail="invalid API token")
