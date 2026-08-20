"""HTML page router (Phase 2.8)."""
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..deps import INTERNAL_TOKEN, _verify_internal, _verify_user
from ..templates.pages import (
    _APP_HTML,
    _JOB_DETAIL_HTML,
    _JOBS_HTML,
    _PROFILE_HTML,
    _PUBLIC_DASHBOARD_HTML,
    _PUBLIC_SUBMIT_HTML,
    _ADMIN_DASHBOARD_HTML,
)


router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_html(_=Depends(_verify_internal)):
    """Legacy user dashboard (admin/internal auth)."""
    return HTMLResponse(content=_ADMIN_DASHBOARD_HTML.replace("__INT__", INTERNAL_TOKEN))


@router.get("/v3api/admin/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard(_=Depends(_verify_internal)):
    """Admin dashboard (internal token)."""
    return HTMLResponse(content=_ADMIN_DASHBOARD_HTML.replace("__INT__", INTERNAL_TOKEN))


@router.get("/admin/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard_root(_=Depends(_verify_internal)):
    """Root-path alias."""
    return HTMLResponse(content=_ADMIN_DASHBOARD_HTML.replace("__INT__", INTERNAL_TOKEN))


@router.get("/v3api/status", response_class=HTMLResponse, include_in_schema=False)
async def public_status_page():
    """Public-facing cluster status (no auth)."""
    return HTMLResponse(content=_PUBLIC_DASHBOARD_HTML)


@router.get("/status", response_class=HTMLResponse, include_in_schema=False)
async def public_status_page_root():
    """Root-path alias for public status."""
    return HTMLResponse(content=_PUBLIC_DASHBOARD_HTML)


@router.get("/api/app", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_root():
    """End-user portal landing (signup/login/dashboard)."""
    return HTMLResponse(content=_APP_HTML)


@router.get("/api/app/jobs", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_jobs(user=Depends(_verify_user)):
    """My Jobs list page."""
    return HTMLResponse(content=_JOBS_HTML)


@router.get("/api/app/job/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_job_detail(job_id: str, user=Depends(_verify_user)):
    """Single job detail page (with live WS)."""
    return HTMLResponse(content=_JOB_DETAIL_HTML)


@router.get("/api/app/submit", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_submit(user=Depends(_verify_user)):
    """Submit new job page."""
    return HTMLResponse(content=_PUBLIC_SUBMIT_HTML)


@router.get("/api/app/profile", response_class=HTMLResponse, include_in_schema=False)
async def app_portal_profile(user=Depends(_verify_user)):
    """Profile page (display_name, email, password)."""
    return HTMLResponse(content=_PROFILE_HTML)
