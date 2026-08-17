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
import hashlib
import logging
import threading
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Header, Depends
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# === Config ===
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8788"))
INTERNAL_TOKEN = os.getenv("CUTDEE_INTERNAL_TOKEN", "dev-internal-token-change-me")
DATA_DIR = Path(os.getenv("GATEWAY_DATA_DIR", "/var/lib/v3-cursor-api/gateway"))
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("v3-gateway")

# === PG setup ===
_JOBS_LOCK = threading.Lock()
_PG_POOL: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _pg_conn():
    """Get a PG connection (or use the pool if available)."""
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


def _init_schema():
    """Create gateway tables."""
    schema = """
    CREATE TABLE IF NOT EXISTS v3_jobs (
        job_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        worker_id TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        reserved_credits INTEGER NOT NULL DEFAULT 0,
        settled_credits INTEGER NOT NULL DEFAULT 0,
        product_path TEXT,
        background_path TEXT,
        cover_path TEXT,
        audio_path TEXT,
        settings JSONB,
        output_file TEXT,
        output_size BIGINT,
        error TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        started_at DOUBLE PRECISION,
        finished_at DOUBLE PRECISION
    );
    CREATE INDEX IF NOT EXISTS idx_v3_jobs_user ON v3_jobs(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_v3_jobs_status ON v3_jobs(status);
    """
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(schema)
        conn.commit()
        log.info("PG schema initialized")
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
app = FastAPI(title="V3_cursor_API Gateway", version="1.0.0")


@app.on_event("startup")
async def startup():
    _init_schema()
    _init_workers()


# === Auth ===
def _verify_internal(x_cutdee_internal: Optional[str] = Header(None)):
    if x_cutdee_internal != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Cutdee-Internal header")
    return True


def _verify_user(authorization: Optional[str] = Header(None)):
    """Bearer cutdee_vdo_<43 chars> or anon (for now)."""
    if not authorization or not authorization.startswith("Bearer "):
        # Anon mode for v1.0 — assign anonymous user
        return "anon"
    token = authorization[7:]
    if not token.startswith("cutdee_vdo_") or len(token) != len("cutdee_vdo_") + 43:
        raise HTTPException(status_code=401, detail="invalid API key format")
    return f"u_{token[11:23]}"  # use first 12 chars as user id


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


async def _worker_health(w: Dict[str, Any]) -> bool:
    """Check worker /health (no auth required)."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{w['url']}/health")
            return r.status_code == 200
    except Exception:
        return False


async def _pick_worker(workers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick best worker (lowest active count, must be healthy)."""
    if not workers:
        return None
    candidates = []
    for w in workers:
        if not w.get("enabled", True):
            continue
        healthy = await _worker_health(w)
        if not healthy:
            continue
        active = w.get("active", 0)
        max_c = w.get("max_concurrent", 1)
        if active >= max_c:
            continue
        candidates.append((active, w))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], secrets.token_hex(2)))
    return candidates[0][1]


# === Pydantic models ===
class CreateJobRequest(BaseModel):
    product_id: str
    background_id: str
    cover_id: Optional[str] = None
    audio_id: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None


# === Endpoints ===
@app.get("/healthz", response_class=JSONResponse)
async def healthz():
    return {"ok": True, "service": "v3-cursor-api-gateway", "version": "1.0.0"}


@app.get("/api/cluster/health", response_class=JSONResponse)
async def cluster_health():
    """Public cluster status (for dashboard)."""
    workers = _load_workers()
    results = []
    for w in workers:
        healthy = await _worker_health(w)
        results.append({
            "id": w["id"],
            "name": w.get("name", w["id"]),
            "url": w["url"],
            "tier": w.get("tier", "low"),
            "max_concurrent": w.get("max_concurrent", 1),
            "active": w.get("active", 0),
            "healthy": healthy,
            "enabled": w.get("enabled", True),
        })
    healthy_count = sum(1 for r in results if r["healthy"])
    return {
        "ok": True,
        "cluster": results,
        "healthy": healthy_count,
        "total": len(results),
    }


@app.post("/api/cluster/workers/reload")
async def reload_workers(_: bool = Depends(_verify_internal)):
    """Reload workers.json from disk."""
    return {"ok": True, "count": len(_load_workers())}


# === Uploads ===
@app.post("/api/v1/uploads/{role}")
async def upload_file(
    role: str,
    request: Request,
    user: str = Depends(_verify_user),
):
    """Upload a file (product/background/cover/audio). Returns file_id."""
    if role not in ("product", "background", "cover", "audio"):
        raise HTTPException(status_code=400, detail=f"invalid role: {role}")
    body = await request.body()
    if len(body) == 0:
        raise HTTPException(status_code=400, detail="empty body")
    file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
    target = UPLOADS_DIR / file_id
    target.write_bytes(body)
    log.info(f"user={user} uploaded {role} -> {file_id} ({len(body)} bytes)")
    return {
        "file_id": file_id,
        "role": role,
        "size": len(body),
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
                src = UPLOADS_DIR / file_id
                if not src.is_file():
                    raise HTTPException(400, detail=f"file {file_id} not found")
                r = await c.post(
                    f"{worker['url']}/v1/jobs/{job_id}/upload/{role}",
                    content=src.read_bytes(),
                    headers={
                        "X-Cutdee-Internal": INTERNAL_TOKEN,
                        "Content-Disposition": f"attachment; filename={file_id}",
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
    except HTTPException:
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

    # Update PG with result
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE v3_jobs
                SET status=%s, output_file=%s, output_size=%s,
                    started_at=%s, finished_at=%s
                WHERE job_id=%s
            """, (
                result.get("status", "unknown"),
                result.get("output_file"),
                result.get("output_size"),
                t0,
                time.time(),
                job_id,
            ))
        conn.commit()
    finally:
        _pg_release(conn)

    log.info(f"job={job_id} worker={worker['id']} status={result.get('status')}")
    return {
        "job_id": job_id,
        "worker_id": worker["id"],
        "status": result.get("status"),
        "output_file": result.get("output_file"),
        "output_size": result.get("output_size"),
        "duration_sec": result.get("duration_sec"),
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
            cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(status_code=404, detail="job not found")

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
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT worker_id FROM v3_jobs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
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


if __name__ == "__main__":
    import uvicorn
    log.info(f"starting V3_cursor_API gateway on 0.0.0.0:{GATEWAY_PORT}")
    log.info(f"data_dir={DATA_DIR}")
    log.info(f"internal_token={'set' if INTERNAL_TOKEN != 'dev-internal-token-change-me' else 'DEFAULT (change me!)'}")
    uvicorn.run(app, host="0.0.0.0", port=GATEWAY_PORT, log_level="info")
