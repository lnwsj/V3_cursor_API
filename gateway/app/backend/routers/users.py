"""Users router (Phase 2.3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.backend.deps import _verify_user
from app.backend.services.jobs import list_active_for_user, list_user_jobs
from app.backend.services.users import get_user_full


router = APIRouter()


@router.get("/api/v1/users/me")
async def api_v1_users_me(user: str = Depends(_verify_user)):
    """Current user info (alias for /auth/me)."""
    return await _me_inner(user)


@router.get("/api/v1/users/me/jobs")
async def api_v1_users_me_jobs(user: str = Depends(_verify_user), limit: int = 50):
    """User-scoped job list (newest first)."""
    jobs = list_user_jobs(user, limit=min(max(limit, 1), 100))
    return {"ok": True, "jobs": jobs}


@router.get("/api/v1/users/me/stats")
async def api_v1_users_me_stats(user: str = Depends(_verify_user)):
    """Lightweight user dashboard: quota + active count + job stats."""
    active = list_active_for_user(user)
    return {
        "ok": True,
        "user": get_user_full(user),
        "active_jobs": active,
    }


async def _me_inner(user: str):
    row = get_user_full(user)
    if not row:
        return {"ok": False, "error": "user not found"}
    return {"ok": True, "user": row}
