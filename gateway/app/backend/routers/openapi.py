"""OpenAPI / Swagger UI custom routes (FIX Phase 4)."""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi

router = APIRouter()


@router.get("/docs", include_in_schema=False)
async def custom_swagger_ui():
    """Custom Swagger UI for V3 Cluster API."""
    return get_swagger_ui_html(openapi_url="/openapi.json", title="V3 Cluster API")


@router.get("/redoc", include_in_schema=False)
async def custom_redoc():
    """Custom ReDoc for V3 Cluster API."""
    return get_redoc_html(openapi_url="/openapi.json", title="V3 Cluster API")


@router.get("/openapi.json", include_in_schema=False)
async def custom_openapi():
    """Custom OpenAPI schema for V3 Cluster API."""
    app: FastAPI = router  # placeholder; the actual app reference comes from main
    return get_openapi(
        title="V3 Cursor Cluster API",
        version="1.2.0",
        description="V3 Cursor Cluster API - gateway + workers for video chroma-key rendering",
        routes=app.routes,
    )
