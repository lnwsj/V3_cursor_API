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
from fastapi import Cookie, FastAPI, Request, HTTPException, UploadFile, File, Form, Header, Depends, Response, Security
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

# === Config ===
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8788"))
API_VERSION = os.getenv("CUTDEE_API_VERSION", "1.2.0")
BUILD_COMMIT = os.getenv("V3_BUILD_COMMIT", "unknown")
INTERNAL_TOKEN = os.getenv("CUTDEE_INTERNAL_TOKEN", "")
PUBLIC_API_KEYS = tuple(
    item.strip()
    for item in os.getenv("CUTDEE_API_KEYS", "").split(",")
    if item.strip()
)
ADMIN_API_KEY = os.getenv("CUTDEE_ADMIN_API_KEY", "")
SESSION_COOKIE_NAME = "cutdee_session"
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
    if not file_id or Path(file_id).name != file_id:
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


def _validate_upload_body(body: bytes) -> bytes:
    if not body:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")
    return body
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
    CREATE INDEX IF NOT EXISTS idx_v3_jobs_user ON v3_jobs(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_v3_jobs_status ON v3_jobs(status);
    """
    migrations = [
        "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS tc TEXT DEFAULT 'tc01'",
        "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS progress INT DEFAULT 0",
        "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS current_step TEXT",
        "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS output_files JSONB",
        "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS log JSONB",
        "ALTER TABLE v3_jobs ADD COLUMN IF NOT EXISTS result JSONB",
        "CREATE INDEX IF NOT EXISTS idx_v3_jobs_tc ON v3_jobs(tc, created_at DESC)",
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
    yield


app = FastAPI(title="V3_cursor_API Gateway", version=API_VERSION, lifespan=lifespan)


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
    if not PUBLIC_API_KEYS:
        raise HTTPException(status_code=503, detail="API authentication is not configured")
    if not token or not any(hmac.compare_digest(token, key) for key in PUBLIC_API_KEYS):
        raise HTTPException(status_code=401, detail="invalid API token")
    if ADMIN_API_KEY and hmac.compare_digest(token, ADMIN_API_KEY):
        return "admin"
    return f"u_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:12]}"


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


async def _pick_worker(workers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the least-loaded healthy worker using live active_jobs when available."""
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
        candidates.append((active, w))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], secrets.token_hex(2)))
    return candidates[0][1]


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
                "UPDATE v3_jobs SET status='failed', error=%s, finished_at=%s WHERE job_id=%s",
                (str(message), time.time(), job_id),
            )
        conn.commit()
    finally:
        _pg_release(conn)


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
                    data.get("finished_at") if status in {"succeeded", "partial", "failed", "cancelled", "paused", "invalid_input"} else None,
                    job_id,
                ),
            )
        conn.commit()
    finally:
        _pg_release(conn)
    return status


async def _monitor_worker_job(job_id: str, worker: Dict[str, Any]) -> None:
    """Poll queued/running Worker jobs without blocking the Gateway event loop."""
    interval = 0.5
    deadline = time.monotonic() + float(os.getenv("GATEWAY_JOB_MONITOR_TIMEOUT", "86400"))
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.get(
                        f"{worker['url']}/v1/jobs/{job_id}/status",
                        headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
                    )
                    if response.status_code == 404:
                        _mark_job_failed(job_id, "worker lost job state")
                        return
                    response.raise_for_status()
                    data = response.json()
                    status = _record_worker_status(job_id, data)
                    if status in {"succeeded", "partial", "failed", "cancelled", "paused", "invalid_input"}:
                        return
                    interval = min(3.0, interval * 1.25)
                except Exception as exc:
                    log.warning("job=%s status poll failed: %s", job_id, exc)
                await asyncio.sleep(interval)
        _mark_job_failed(job_id, "worker job monitor timeout")
    finally:
        _MONITOR_TASKS.pop(job_id, None)


def _start_worker_monitor(job_id: str, worker: Dict[str, Any], status: str) -> None:
    if _canonical_status(status) in {"succeeded", "partial", "failed", "cancelled", "paused", "invalid_input"}:
        return
    existing = _MONITOR_TASKS.get(job_id)
    if existing and not existing.done():
        return
    _MONITOR_TASKS[job_id] = asyncio.create_task(_monitor_worker_job(job_id, worker))


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
                _record_worker_status(row["job_id"], data)
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


@app.delete("/api/cluster/workers/{worker_id}")
async def remove_worker(worker_id: str, _: bool = Depends(_verify_internal)):
    """Remove a worker from the cluster. Returns 200 on success, 404 if not found."""
    workers = _load_workers()
    new_workers = [w for w in workers if w["id"] != worker_id]
    if len(new_workers) == len(workers):
        raise HTTPException(404, f"worker '{worker_id}' not found")
    _save_workers(new_workers)
    return {"ok": True, "removed": worker_id, "total": len(new_workers)}


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
    worker = await _pick_worker(workers)
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
        # Mark failed
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE v3_jobs
                    SET status='failed', error=%s, finished_at=%s
                    WHERE job_id=%s
                """, (str(e), time.time(), job_id))
            conn.commit()
        finally:
            _pg_release(conn)
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
        _record_worker_status(job_id, result)

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
    _record_worker_status(job_id, result)
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
    _record_worker_status(job_id, result)
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
    _record_worker_status(job_id, result)
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
                (job_id, user_id, worker_id, tc, status, settings, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (job_id, user, worker["id"], tc, "queued",
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
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE v3_jobs SET status='failed', error=%s, finished_at=%s WHERE job_id=%s",
                            (str(e), time.time(), job_id))
            conn.commit()
        finally:
            _pg_release(conn)
        raise HTTPException(502, f"worker dispatch failed: {e}")

    status = _canonical_status(result.get("status", "queued"))
    if status in {"queued", "running"}:
        _start_worker_monitor(job_id, worker, status)
    else:
        _record_worker_status(job_id, result)
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
