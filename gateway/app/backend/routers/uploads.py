"""Uploads router (Phase 2.5)."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path as PathParam, UploadFile

from app.backend.deps import DATA_DIR, MAX_UPLOAD_BYTES, SAFE_FILE_ID, UPLOADS_DIR, _verify_user


router = APIRouter()
ALLOWED_UPLOAD_SUFFIX = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".png", ".jpg", ".jpeg", ".zip"}


def _upload_suffix(filename: Optional[str], role: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in ALLOWED_UPLOAD_SUFFIX:
        return suffix
    if role == "cover":
        return ".png"
    if role == "product_root":
        return ".zip"
    return ".mp4"


def _find_upload_path(file_id: str) -> Path:
    if not file_id or Path(file_id).name != file_id or not SAFE_FILE_ID.fullmatch(file_id):
        raise HTTPException(status_code=400, detail="invalid file id")
    exact = UPLOADS_DIR / file_id
    if exact.is_file():
        return exact
    matches = sorted(path for path in UPLOADS_DIR.glob(f"{file_id}.*") if path.is_file())
    if matches:
        return matches[0]
    raise HTTPException(status_code=400, detail=f"file {file_id} not found")


@router.post("/api/v1/uploads/{role}")
async def api_v1_uploads_role(
    role: str = PathParam(..., regex="^(product|background|cover|audio|source|product_root)$"),
    file: UploadFile = File(...),
    user: str = Depends(_verify_user),
):
    """Save an upload to disk and return its file_id.

    Roles: product, background, cover, audio, source, product_root.
    """
    body = await file.read()
    if not body:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = _upload_suffix(file.filename, role)
    file_id = f"{role}_{int(time.time() * 1000)}_{secrets_token_hex()}{suffix}".replace("secrets", "")
    import secrets as _sec
    file_id = f"{role}_{int(time.time() * 1000)}_{_sec.token_hex(8)}{suffix}"
    out_path = UPLOADS_DIR / file_id
    out_path.write_bytes(body)
    return {"ok": True, "file_id": file_id, "role": role, "size": len(body), "suffix": suffix}
