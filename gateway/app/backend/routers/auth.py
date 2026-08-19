"""Auth router (Phase 2.2)."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from app.backend.deps import (
    ADMIN_API_KEY,
    INTERNAL_TOKEN,
    SESSION_COOKIE_NAME as _SESSION_COOKIE_NAME,
    _get_user_tier,
    _is_admin,
    _user_for_token,
    _verify_user,
)
from app.backend.services.users import (
    SESSION_KEYS,
    TIER_PRIORITY,
    auto_register_admin,
    auto_register_user,
    change_password as _change_password,
    clear_session_cookie,
    create_user,
    email_normalize,
    generate_api_key,
    get_user_by_email,
    get_user_full,
    hash_password,
    set_session_cookie,
    session_key_clear,
    session_key_register,
    update_last_login,
    update_user_profile,
    validate_email,
    verify_password,
)


router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class SignupIn(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginIn(BaseModel):
    email: str
    password: str


class UpdateProfileIn(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post("/api/auth/session")
async def create_auth_session(response: Response, authorization: Optional[str] = Header(None)):
    """Exchange a valid bearer token for a short-lived HttpOnly media-session cookie."""
    token = authorization
    if token and token.lower().startswith("bearer "):
        token = token[7:]
    token = (token or "").strip()
    if not token:
        raise HTTPException(401, "Bearer API token required")
    user = _user_for_token(token)
    response.set_cookie(
        _SESSION_COOKIE_NAME,
        token,
        max_age=8 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True, "user": user, "expires_in": 8 * 60 * 60}


@router.post("/api/v1/auth/signup")
async def signup(body: SignupIn, response: Response, user: str = Depends(_verify_user)):
    """Public signup (FIX 2026-08-19).

    Body: { "email": "...", "password": "...", "display_name": "..." (optional)
    - Validates email format + password length (min 8)
    - Creates user in v3_users (with API key + password hash)
    - Sets session cookie + returns API key (shown once)
    """
    email = email_normalize(body.email)
    if not validate_email(email):
        raise HTTPException(400, "invalid email")
    if len(body.password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if len(body.password) > 200:
        raise HTTPException(400, "password too long (max 200)")

    user_id = f"u_{hashlib.sha256(email.encode('utf-8')).hexdigest()[:12]}"
    api_key = generate_api_key(user_id)
    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    password_hash = hash_password(body.password)
    api_key_prefix = api_key[:11] + "..."

    # Check if email already exists
    existing = get_user_by_email(email)
    if existing:
        raise HTTPException(409, "email already registered")

    create_user(
        user_id=user_id,
        email=email,
        password_hash=password_hash,
        display_name=body.display_name or email.split("@")[0],
        api_key_hash=api_key_hash,
        api_key_prefix=api_key_prefix,
    )
    # Register session key for cookie auth
    session_key_register(api_key, user_id)
    set_session_cookie(response, api_key)
    return {
        "ok": True,
        "user_id": user_id,
        "email": email,
        "api_key": api_key,  # shown ONCE — user must save this
        "session_set": True,
        "quota_per_month": 100,
        "message": "Welcome! Save your API key — it won't be shown again.",
    }


@router.post("/api/v1/auth/login")
async def login(body: LoginIn, response: Response):
    """Email + password login (FIX 2026-08-19).

    - Verify password hash
    - Rotate API key (invalidates old session key)
    - Set new session cookie
    - Return new API key + user info
    """
    email = email_normalize(body.email)
    row = get_user_by_email(email)
    if not row or not row.get("password_hash"):
        raise HTTPException(401, "invalid email or password")
    if not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "invalid email or password")
    # Rotate API key (invalidates old)
    new_api_key = generate_api_key(row["user_id"])
    new_hash = hashlib.sha256(new_api_key.encode("utf-8")).hexdigest()
    with __import__("app.backend.services.db", fromlist=["pg_conn"]).pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v3_users SET api_key_hash = %s, last_login_at = %s WHERE user_id = %s",
                (new_hash, time.time(), row["user_id"]),
            )
        conn.commit()
    # Register new session key
    session_key_register(new_api_key, row["user_id"])
    set_session_cookie(response, new_api_key)
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


@router.post("/api/v1/auth/logout")
async def logout(request: Request, response: Response):
    """Logout: clear cookie + revoke session token (best-effort)."""
    response.delete_cookie(_SESSION_COOKIE_NAME, path="/")
    cookie_value = request.cookies.get(_SESSION_COOKIE_NAME)
    if cookie_value:
        session_key_clear(cookie_value)
    return {"ok": True}


@router.get("/api/v1/auth/me")
async def auth_me(user: str = Depends(_verify_user)):
    """Current user info (FIX 2026-08-19) — used by the portal."""
    row = get_user_full(user)
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


@router.post("/api/v1/auth/change-password")
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
    with __import__("app.backend.services.db", fromlist=["pg_conn"]).pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM v3_users WHERE user_id = %s", (user,))
            row = cur.fetchone()
            if not row or not row["password_hash"]:
                raise HTTPException(404, "user not found")
            if not verify_password(old_password, row["password_hash"]):
                raise HTTPException(401, "current password is incorrect")
    _change_password(user, hash_password(new_password))
    return {"ok": True, "message": "password changed"}


@router.patch("/api/v1/auth/me")
async def api_v1_update_me(body: UpdateProfileIn, user: str = Depends(_verify_user)):
    """Update profile (display_name, email) (FIX 2026-08-19)."""
    if body.email is not None and not validate_email(body.email):
        raise HTTPException(400, "invalid email format")
    if body.display_name is not None and not body.display_name.strip():
        raise HTTPException(400, "display_name cannot be empty")
    if body.email is None and body.display_name is None:
        raise HTTPException(400, "no fields to update")
    update_user_profile(user, display_name=body.display_name, email=body.email)
    # Return updated info
    return await auth_me(user=user)
