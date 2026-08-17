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
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Header, Depends, Response
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
    """Pick best worker (lowest active count, must be healthy)."""
    if not workers:
        return None
    candidates = []
    for w in workers:
        if not w.get("enabled", True):
            continue
        healthy = await _worker_alive(w)
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


# ============================================================
# V3 WebApp-compatible API endpoints (UI calls these)
# ============================================================

@app.post("/api/render/{tc}")
async def api_render_tc(tc: str, request: Request):
    """V3 UI-compatible render endpoint. Accepts multipart FormData with files + settings."""
    tc = tc.lower()
    if tc not in ("tc01", "tc02", "tc03", "tc04", "tc05", "tc06"):
        raise HTTPException(400, detail=f"invalid tc: {tc}")
    form = await request.form()
    file_map = {"product": [], "background": [], "cover": [], "audio": [], "source": []}
    settings = {}
    for role in ("product", "background", "cover", "audio"):
        f = form.get(role)
        if f and hasattr(f, "read"):
            data = await f.read()
            file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
            (UPLOADS_DIR / file_id).write_bytes(data)
            file_map[role].append(file_id)
    for f in form.getlist("sources"):
        if hasattr(f, "read"):
            data = await f.read()
            file_id = f"source_{int(time.time())}_{secrets.token_hex(8)}"
            (UPLOADS_DIR / file_id).write_bytes(data)
            file_map["source"].append(file_id)
    for fld, role in (("products", "product"), ("backgrounds", "background"), ("audios", "audio")):
        for f in form.getlist(fld):
            if hasattr(f, "read"):
                data = await f.read()
                file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
                (UPLOADS_DIR / file_id).write_bytes(data)
                file_map[role].append(file_id)
    for k, v in form.items():
        if k in ("product", "background", "cover", "audio", "sources", "products", "backgrounds", "audios"):
            continue
        if hasattr(v, "read"):
            continue
        settings[k] = v
    if not file_map["product"] and not file_map["source"]:
        raise HTTPException(400, detail="missing product or source files")
    workers = _load_workers()
    worker = await _pick_worker(workers)
    if not worker:
        raise HTTPException(503, detail="no worker available")
    job_id = f"v3_{int(time.time())}_{secrets.token_hex(6)}"
    user = "ui"
    settings["mode"] = tc
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v3_jobs (job_id, user_id, worker_id, tc, status, settings, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (job_id, user, worker["id"], tc, "queued", json.dumps(settings), time.time())
            )
        conn.commit()
    finally:
        _pg_release(conn)
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT) as c:
            for role in ("product", "background", "cover", "audio", "source"):
                for file_id in file_map[role]:
                    src_path = UPLOADS_DIR / file_id
                    if not src_path.is_file():
                        continue
                    r = await c.post(
                        f"{worker['url']}/v1/jobs/{job_id}/upload/{role}",
                        content=src_path.read_bytes(),
                        headers={"X-Cutdee-Internal": INTERNAL_TOKEN, "Content-Disposition": f"attachment; filename={file_id}"},
                    )
                    r.raise_for_status()
            render_payload = {"mode": tc, "settings": settings}
            if tc == "tc05":
                render_payload["source_ids"] = file_map["source"]
            else:
                if file_map["product"]:
                    render_payload["product_ids"] = file_map["product"]
                    render_payload["product_id"] = file_map["product"][0]
                if file_map["background"]:
                    render_payload["background_ids"] = file_map["background"]
                    render_payload["background_id"] = file_map["background"][0]
                if file_map["cover"]:
                    render_payload["cover_ids"] = file_map["cover"]
                    render_payload["cover_id"] = file_map["cover"][0]
                if file_map["audio"]:
                    render_payload["audio_ids"] = file_map["audio"]
                    render_payload["audio_id"] = file_map["audio"][0]
            url1 = f"{worker['url']}/v1/{tc}/render/{job_id}"
            r = await c.post(url1, json=render_payload, headers={"X-Cutdee-Internal": INTERNAL_TOKEN})
            if r.status_code == 404:
                r = await c.post(
                    f"{worker['url']}/v1/jobs/{job_id}/render",
                    json=render_payload,
                    headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
                )
            r.raise_for_status()
            result = r.json()
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"dispatch to {worker['id']} failed: {e}")
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE v3_jobs SET status='failed', error=%s, finished_at=%s WHERE job_id=%s",
                    (str(e), time.time(), job_id)
                )
            conn.commit()
        finally:
            _pg_release(conn)
        raise HTTPException(502, detail=f"worker dispatch failed: {e}")
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v3_jobs SET status=%s, output_file=%s, output_size=%s, started_at=%s, finished_at=%s WHERE job_id=%s",
                (result.get("status", "unknown"), result.get("output_file"), result.get("output_size"), time.time(), time.time(), job_id)
            )
        conn.commit()
    finally:
        _pg_release(conn)
    return {
        "job_id": job_id,
        "tc": tc,
        "worker_id": worker["id"],
        "status": result.get("status"),
        "output_file": result.get("output_file"),
        "output_size": result.get("output_size"),
        "duration_sec": result.get("duration_sec"),
    }


@app.get("/api/job/{job_id}")
async def api_job_get_singular(job_id: str):
    """Singular alias for /api/jobs/{job_id} (V3 UI uses this)."""
    return await api_jobs_get(job_id)


@app.post("/api/job/{job_id}/cancel")
async def api_job_cancel_singular(job_id: str):
    """Singular alias for /api/jobs/{job_id}/cancel (V3 UI uses this)."""
    return await api_jobs_cancel(job_id)


@app.get("/api/job/{job_id}/thumbnails")
async def api_job_thumbnails(job_id: str):
    """Return thumbnail URLs for the job (V3 UI uses this for preview)."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT output_file FROM v3_jobs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row or not row.get("output_file"):
        return {"thumbnails": []}
    filename = row["output_file"].split("/")[-1]
    return {"thumbnails": [{"job_id": job_id, "url": f"/api/v1/jobs/{job_id}/download/{filename}", "time_offset": 0}]}


@app.get("/api/jobs/history")
async def api_jobs_history(limit: int = 50):
    """Alias for /api/jobs/list (V3 UI uses this)."""
    return await api_jobs_list(tc=None, limit=limit)


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
    return {"ok": True, "service": "v3-cursor-api-gateway", "version": "1.0.0"}


@app.get("/api/cluster/health", response_class=JSONResponse)
async def cluster_health():
    """Public cluster status (for dashboard). Includes per-worker system stats."""
    workers = _load_workers()
    results = []
    # Fetch all worker healths in parallel
    health_results = await asyncio.gather(*[_worker_health(w) for w in workers], return_exceptions=True)
    for w, h in zip(workers, health_results):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:120]}
        is_healthy = h.get("ok") is True
        sys_stats = h.get("system") if is_healthy else None
        gpu = h.get("gpu") if is_healthy else None
        encoder = h.get("encoder") if is_healthy else None
        result = {
            "id": w["id"],
            "name": w.get("name", w["id"]),
            "url": w["url"],
            "tier": w.get("tier", "low"),
            "max_concurrent": w.get("max_concurrent", 1),
            "active": h.get("active_jobs", 0) if is_healthy else w.get("active", 0),
            "healthy": is_healthy,
            "enabled": w.get("enabled", True),
        }
        if sys_stats:
            result["system"] = sys_stats
        if gpu:
            result["gpu_capabilities"] = gpu
        if encoder:
            result["encoder"] = encoder
        if not is_healthy and h.get("error"):
            result["error"] = h["error"]
        results.append(result)
    healthy_count = sum(1 for r in results if r["healthy"])
    total_capacity = sum(r["max_concurrent"] for r in results if r["healthy"] and r.get("enabled", True))
    active_count = sum(r.get("active", 0) for r in results)
    return {
        "ok": True,
        "cluster": results,
        "healthy": healthy_count,
        "total": len(results),
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



# =====================================================================
# === V3 WebApp-compatible API routes ===
# =====================================================================
# These match the V3 WebApp frontend's expected endpoints (TC01-TC06).
# The gateway translates V3-style requests to internal cluster calls,
# and translates responses back to V3 format.

# --- System endpoints (return aggregated info from workers) ---

@app.get("/api/health")
async def api_health():
    """Aggregated health: workers + GPU + ffmpeg."""
    workers = _load_workers()
    results = []
    health_results = await asyncio.gather(*[_worker_health(w) for w in workers], return_exceptions=True)
    for w, h in zip(workers, health_results):
        if isinstance(h, Exception): h = {"ok": False, "error": str(h)[:120]}
        is_healthy = h.get("ok") is True
        results.append({"id": w["id"], "healthy": is_healthy, "url": w["url"],
                        "encoder": h.get("encoder", ["libx264"])[0] if is_healthy else "libx264",
                        "system": h.get("system"), "gpu": h.get("gpu")})
    return {
        "status": "ok" if any(r["healthy"] for r in results) else "degraded",
        "service": "v3-cursor-api-gateway",
        "version": "1.1.0",
        "workers": results,
        "total_workers": len(results),
        "healthy_workers": sum(1 for r in results if r["healthy"]),
        "disk_free_gb": 100,
    }

@app.get("/api/version")
async def api_version():
    import sys
    return {"version": "1.1.0-cluster", "python": sys.version}

@app.get("/api/ffmpeg")
async def api_ffmpeg():
    """Use first worker's ffmpeg info."""
    workers = _load_workers()
    for w in workers:
        h = await _worker_health(w)
        if h.get("ok"):
            return {"path": w["url"], "version": h.get("ffmpeg_version", "unknown"), "from_worker": w["id"]}
    return {"path": "ffmpeg", "version": "unknown"}

@app.get("/api/encoders")
async def api_encoders():
    """Aggregated encoder list."""
    workers = _load_workers()
    available = set()
    for w in workers:
        h = await _worker_health(w)
        if h.get("ok"):
            for enc in h.get("gpu", {}).get("available", []):
                available.add(enc)
    return {"available": [{"name": e} for e in sorted(available)]}

@app.get("/api/lens")
async def api_lens():
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
async def api_config():
    return {"config": {
        "version": "1.1.0",
        "cluster_mode": True,
        "supported_tcs": ["tc01", "tc02", "tc03", "tc04", "tc05", "tc06"],
    }}

# --- Job endpoints (V3 format) ---

# 1. Upload (V3 frontend uses POST /api/jobs/upload with Form file)
@app.post("/api/jobs/upload")
async def api_jobs_upload(file: UploadFile = File(...), role_hint: Optional[str] = Form(None)):
    """Upload a file. Returns {id, original_name, kind, size} in V3 format."""
    role = role_hint or "file"
    if role not in ("product", "background", "cover", "audio", "source", "file"):
        role = "file"
    body = await file.read()
    if len(body) == 0:
        raise HTTPException(400, "empty file")
    file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
    target = UPLOADS_DIR / file_id
    target.write_bytes(body)
    log.info(f"uploaded {file_id} ({len(body)} bytes) as {role}")
    return {
        "id": file_id,
        "original_name": file.filename or file_id,
        "kind": role,
        "size": len(body),
        "uploaded_at": time.time(),
    }

# 2. List jobs (V3 returns {jobs: [...]})
@app.get("/api/jobs/list")
async def api_jobs_list(tc: Optional[str] = None, limit: int = 50):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if tc:
                cur.execute("SELECT * FROM v3_jobs WHERE tc=%s ORDER BY created_at DESC LIMIT %s", (tc, limit))
            else:
                cur.execute("SELECT * FROM v3_jobs ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
    finally:
        _pg_release(conn)
    jobs = []
    for r in rows:
        jobs.append(_v3_job_dict(r))
    return {"jobs": jobs}

# 3. Get job (V3 format with progress, current_step, files, logs)
@app.get("/api/jobs/{job_id}")
async def api_jobs_get(job_id: str):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    return _v3_job_dict(row)

# 4. Cancel/pause/resume (stub for now)
@app.post("/api/jobs/{job_id}/cancel")
async def api_jobs_cancel(job_id: str):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE v3_jobs SET status='cancelled', finished_at=%s WHERE job_id=%s AND status IN ('queued','running')",
                        (time.time(), job_id))
            ok = cur.rowcount > 0
        conn.commit()
    finally:
        _pg_release(conn)
    if not ok:
        raise HTTPException(404, "job not found or already terminal")
    return {"job_id": job_id, "cancelled": True}

@app.post("/api/jobs/{job_id}/pause")
async def api_jobs_pause(job_id: str):
    return {"job_id": job_id, "paused": True, "note": "pause not yet implemented"}

@app.post("/api/jobs/{job_id}/resume")
async def api_jobs_resume(job_id: str):
    return {"job_id": job_id, "resumed": True, "note": "resume not yet implemented"}

# 5. Outputs / downloads
@app.get("/api/outputs")
async def api_outputs():
    """List all output files in the cluster (across workers)."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT job_id, output_file, output_size, finished_at, worker_id FROM v3_jobs WHERE status='succeeded' AND output_file IS NOT NULL ORDER BY finished_at DESC LIMIT 100")
            rows = cur.fetchall()
    finally:
        _pg_release(conn)
    return {"outputs": [
        {"job_id": r["job_id"], "filename": r["output_file"], "size": r["output_size"],
         "finished_at": r["finished_at"], "worker": r["worker_id"]}
        for r in rows
    ]}

@app.get("/api/download/{file_path:path}")
async def api_download(file_path: str):
    """Proxy download. file_path is the worker-relative path."""
    # Find which worker has this file
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT worker_id FROM v3_jobs WHERE output_file=%s ORDER BY finished_at DESC LIMIT 1", (file_path.split('/')[-1],))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        # Maybe it's a job_id/filename combo
        parts = file_path.split('/')
        if len(parts) == 2:
            jid, fn = parts
            conn = _pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT worker_id FROM v3_jobs WHERE job_id=%s", (jid,))
                    row = cur.fetchone()
            finally:
                _pg_release(conn)
            if row:
                file_path = fn
    if not row:
        raise HTTPException(404, "file not found")
    worker_id = row["worker_id"]
    workers = _load_workers()
    worker = next((w for w in workers if w["id"] == worker_id), None)
    if not worker:
        raise HTTPException(404, f"worker {worker_id} not found")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{worker['url']}/v1/jobs/_/output",
            params={"filename": file_path.split('/')[-1]},
            headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, detail=r.text)
        return Response(content=r.content, media_type="video/mp4",
                       headers={"Content-Disposition": f'attachment; filename="{file_path.split("/")[-1]}"'})


def _v3_job_dict(row) -> Dict[str, Any]:
    """Convert a v3_jobs DB row to V3 frontend format."""
    files = row.get("output_files") or []
    if isinstance(files, str):
        try: files = json.loads(files)
        except: files = []
    logs = row.get("log") or []
    if isinstance(logs, str):
        try: logs = json.loads(logs)
        except: logs = []
    result = row.get("result") or {}
    if isinstance(result, str):
        try: result = json.loads(result)
        except: result = {}
    out = {
        "job_id": row["job_id"],
        "tc": row.get("tc", "tc01"),
        "status": row.get("status", "unknown"),
        "progress": row.get("progress", 0) or 0,
        "current_step": row.get("current_step"),
        "message": row.get("error") or (row.get("result") or {}).get("message") or "",
        "files": files if isinstance(files, list) else [],
        "logs": logs if isinstance(logs, list) else [],
        "result": result,
        "worker_id": row.get("worker_id"),
        "encoder": (result.get("encoder") if isinstance(result, dict) else None),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "output_file": row.get("output_file"),
        "output_size": row.get("output_size"),
        "settings": row.get("settings") or {},
    }
    if isinstance(out["settings"], str):
        try: out["settings"] = json.loads(out["settings"])
        except: out["settings"] = {}
    return out


# --- TC render endpoints (V3 frontend calls POST /api/{tc}/render) ---

class V3RenderPayload(BaseModel):
    files: Dict[str, List[str]] = {}
    settings: Dict[str, Any] = {}
    values: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None  # for TC06 {root: ...}


async def _dispatch_tc_render(tc: str, payload: V3RenderPayload, user: str = "anon") -> Dict[str, Any]:
    """Pick a worker, upload files, dispatch to /v1/{tc}/render/{job_id}."""
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
            for fid in products + backgrounds + covers + audios + sources:
                src = UPLOADS_DIR / fid
                if not src.is_file():
                    raise HTTPException(400, f"file {fid} not found")
                # determine role
                role = "product" if fid in products else "background" if fid in backgrounds else "cover" if fid in covers else "audio" if fid in audios else "source"
                r = await c.post(
                    f"{worker['url']}/v1/jobs/{job_id}/upload/{role}",
                    content=src.read_bytes(),
                    headers={"X-Cutdee-Internal": INTERNAL_TOKEN, "Content-Disposition": f"attachment; filename={fid}"},
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
                    "settings": payload.settings or {},
                    "values": payload.values or payload.settings or {},
                },
                headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
            )
            r.raise_for_status()
            result = r.json()
    except HTTPException:
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

    # Update PG
    elapsed = time.time() - t0
    output_files = result.get("output_files", [])
    if result.get("output_file") and result["output_file"] not in output_files:
        output_files = [result["output_file"]] + output_files
    log_lines = result.get("log_lines", [])
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE v3_jobs
                SET status=%s, output_file=%s, output_size=%s, output_files=%s,
                    log=%s, result=%s, progress=100, started_at=%s, finished_at=%s
                WHERE job_id=%s""",
                (result.get("status", "unknown"),
                 result.get("output_file"),
                 result.get("output_size"),
                 json.dumps(output_files),
                 json.dumps(log_lines),
                 json.dumps(result),
                 t0, time.time(), job_id))
        conn.commit()
    finally:
        _pg_release(conn)
    return {
        "job_id": job_id,
        "tc": tc,
        "worker_id": worker["id"],
        "status": result.get("status", "succeeded"),
        "output_file": result.get("output_file"),
        "output_files": output_files,
        "output_size": result.get("output_size"),
        "duration_sec": result.get("duration_sec", elapsed),
        "encoder": result.get("encoder"),
        "message": f"{tc} {result.get('status', 'done')}",
    }


# Add /api/{tc}/render and /api/{tc}/dry-run for tc01..tc06
for _tc_key in ("tc01", "tc02", "tc03", "tc04", "tc05", "tc06"):
    def _make_render_handler(t: str = _tc_key):
        async def _h(payload: V3RenderPayload):
            return await _dispatch_tc_render(t, payload)
        _h.__name__ = f"render_{_tc_key}"
        return _h
    app.post(f"/api/{_tc_key}/render")(_make_render_handler())
    def _make_dryrun_handler(t: str = _tc_key):
        async def _h(payload: V3RenderPayload):
            # Return a simple plan without running
            files = payload.files or {}
            products = len(files.get("product", files.get("products", [])))
            backgrounds = len(files.get("bg", files.get("background", files.get("backgrounds", []))))
            sources = len(files.get("source", files.get("sources", [])))
            return {
                "tc": t, "products": products, "backgrounds": backgrounds, "sources": sources,
                "plan": {
                    "tc": t,
                    "planned_output_count": products,
                    "final_count": products,
                    "composition_count": 1,
                    "reframe_per_source": 1,
                    "segment_count_assumption": 1,
                    "values": payload.settings or {},
                    "files": {k: len(v) if isinstance(v, list) else 0 for k, v in files.items()},
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
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
