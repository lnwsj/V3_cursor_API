"""Worker registry + health probing (Phase 1.4 refactor)."""
from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Dict, List, Optional

import httpx

from .db import pg_conn


# ---------------------------------------------------------------------------
# Worker config (env-driven paths)
# ---------------------------------------------------------------------------
# These get patched at module-load from main.py via init_config()
WORKERS_FILE_PATH = "workers.json"  # absolute path set by init_config
DEFAULT_WORKERS: List[Dict[str, Any]] = []


def init_config(workers_file_path: str, default_workers: List[Dict[str, Any]]) -> None:
    """Wire up paths from main.py (called once at app startup)."""
    global WORKERS_FILE_PATH, DEFAULT_WORKERS
    WORKERS_FILE_PATH = workers_file_path
    DEFAULT_WORKERS = default_workers


# ---------------------------------------------------------------------------
# Worker registry CRUD
# ---------------------------------------------------------------------------
def load_workers() -> List[Dict[str, Any]]:
    """Read workers.json from disk. Returns empty list if missing."""
    from pathlib import Path
    wf = Path(WORKERS_FILE_PATH)
    if not wf.exists():
        return []
    with wf.open() as f:
        data = json.load(f)
    return data.get("workers", [])


def save_workers(workers: List[Dict[str, Any]]) -> None:
    """Persist workers.json atomically (best-effort)."""
    from pathlib import Path
    wf = Path(WORKERS_FILE_PATH)
    wf.write_text(json.dumps({"workers": workers}, indent=2))


def init_workers() -> List[Dict[str, Any]]:
    """Load workers from disk; seed with DEFAULT_WORKERS if empty."""
    workers = load_workers()
    if not workers:
        workers = list(DEFAULT_WORKERS)
        if workers:
            save_workers(workers)
    return workers


def reload_workers() -> List[Dict[str, Any]]:
    """Force re-read from disk (admin /api/cluster/workers/reload)."""
    return load_workers()


def add_worker(worker: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Append worker + persist."""
    workers = load_workers()
    workers.append(worker)
    save_workers(workers)
    return workers


def remove_worker(worker_id: str) -> List[Dict[str, Any]]:
    """Remove worker by id + persist. Returns updated list."""
    workers = load_workers()
    new_workers = [w for w in workers if w["id"] != worker_id]
    save_workers(new_workers)
    return new_workers


def update_worker(worker_id: str, **fields) -> Optional[Dict[str, Any]]:
    """Update worker fields. Returns the updated worker or None if not found."""
    workers = load_workers()
    updated = None
    for w in workers:
        if w["id"] == worker_id:
            w.update({k: v for k, v in fields.items() if v is not None})
            updated = w
            break
    if updated:
        save_workers(workers)
    return updated


# ---------------------------------------------------------------------------
# Worker health probe
# ---------------------------------------------------------------------------
def encoder_names(health: Dict[str, Any]) -> List[str]:
    """Resolve the worker's reported encoder list (handles string/list/dict)."""
    raw = health.get("encoder")
    if isinstance(raw, dict):
        raw = raw.get("available") or raw.get("preferred") or []
    if isinstance(raw, str):
        raw = [raw]
    names = [item for item in (raw or []) if isinstance(item, str) and item]
    gpu = health.get("gpu")
    if isinstance(gpu, dict):
        names.extend(item for item in (gpu.get("available") or []) if isinstance(item, str) and item)
    return list(dict.fromkeys(names))


def anonymize_worker(worker: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Strip sensitive fields + return public-safe view (Node-N naming)."""
    return {
        "id": worker["id"],
        "name": f"Node-{index}",
        "url": worker["url"],
        "enabled": worker.get("enabled", True),
        "healthy": worker.get("healthy", False),
        "tier": worker.get("tier", "low"),
        "max_concurrent": worker.get("max_concurrent", 1),
        "active_jobs": worker.get("active_jobs", 0),
        "in_flight_jobs": worker.get("in_flight_jobs", []),
        "encoder": (encoder_names(worker) or ["?"])[0] if worker.get("healthy") else "?",
        "encoders_all": encoder_names(worker) if worker.get("healthy") else [],
        "system": worker.get("system") if worker.get("healthy") else None,
        "gpu": worker.get("gpu") if worker.get("healthy") else None,
        "worker_id": worker.get("worker_id"),
        "version": worker.get("version"),
        "commit": worker.get("commit"),
        "data_dir": worker.get("data_dir"),
        "last_seen": worker.get("last_seen"),
    }


async def worker_health(worker: Dict[str, Any], internal_token: str,
                        timeout: float = 5.0) -> Dict[str, Any]:
    """Probe a single worker's /health endpoint. Returns parsed health or {ok:false, error}."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{worker['url']}/health", headers={"X-Cutdee-Internal": internal_token})
            if r.status_code == 404:
                return {"ok": False, "error": "404 not found"}
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}


async def fetch_worker_active_jobs(worker: Dict[str, Any],
                                  internal_token: str,
                                  timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Fetch worker's in-flight jobs via /v1/active_jobs."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(
                f"{worker['url']}/v1/active_jobs",
                headers={"X-Cutdee-Internal": internal_token},
            )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            jobs = data.get("active_jobs", []) or data.get("jobs", [])
            return [
                {
                    "job_id": j.get("job_id"),
                    "status": j.get("status"),
                    "started_at": j.get("started_at"),
                    "tc": j.get("tc"),
                    "log_tail": (j.get("log_tail") or [])[-5:],
                }
                for j in jobs
            ]
    except Exception:
        return []


async def probe_all_workers(workers: List[Dict[str, Any]], internal_token: str,
                            timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Parallel health probe for all workers (returns enriched list)."""
    if not workers:
        return []
    results = await asyncio.gather(
        *(worker_health(w, internal_token, timeout=timeout) for w in workers),
        return_exceptions=True,
    )
    enriched = []
    for w, h in zip(workers, results):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:120]}
        enriched.append({
            "id": w["id"],
            "url": w["url"],
            "enabled": w.get("enabled", True),
            "healthy": h.get("ok") is True,
            "tier": w.get("tier", "low"),
            "max_concurrent": w.get("max_concurrent", 1),
            "active_jobs": h.get("active_jobs", 0) if h.get("ok") else 0,
            "in_flight_jobs": h.get("in_flight_jobs", []) if h.get("ok") else [],
            "encoder": (encoder_names(h) or ["?"])[0] if h.get("ok") else "?",
            "encoders_all": encoder_names(h) if h.get("ok") else [],
            "system": h.get("system") if h.get("ok") else None,
            "gpu": h.get("gpu") if h.get("ok") else None,
            "worker_id": h.get("worker_id") if h.get("ok") else None,
            "version": h.get("version") if h.get("ok") else None,
            "commit": h.get("commit") if h.get("ok") else None,
            "data_dir": h.get("data_dir") if h.get("ok") else None,
            "last_seen": h.get("server_time") if h.get("ok") else None,
            "error": h.get("error"),
        })
    return enriched


async def probe_all_workers_with_inflight(workers: List[Dict[str, Any]],
                                          internal_token: str,
                                          timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Like probe_all_workers but also fetches in-flight jobs from each worker."""
    if not workers:
        return []
    health_results = await asyncio.gather(
        *(worker_health(w, internal_token, timeout=timeout) for w in workers),
        return_exceptions=True,
    )
    inflight_results = await asyncio.gather(
        *(fetch_worker_active_jobs(w, internal_token, timeout=timeout) for w in workers),
        return_exceptions=True,
    )
    enriched = []
    for w, h, jobs in zip(workers, health_results, inflight_results):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:120]}
        if isinstance(jobs, Exception):
            jobs = []
        enriched.append({
            "id": w["id"],
            "url": w["url"],
            "enabled": w.get("enabled", True),
            "healthy": h.get("ok") is True,
            "tier": w.get("tier", "low"),
            "max_concurrent": w.get("max_concurrent", 1),
            "active_jobs": (h.get("active_jobs", 0) if h.get("ok") else 0) + len(jobs),
            "in_flight_jobs": jobs,
            "encoder": (encoder_names(h) or ["?"])[0] if h.get("ok") else "?",
            "encoders_all": encoder_names(h) if h.get("ok") else [],
            "system": h.get("system") if h.get("ok") else None,
            "gpu": h.get("gpu") if h.get("ok") else None,
            "worker_id": h.get("worker_id") if h.get("ok") else None,
            "version": h.get("version") if h.get("ok") else None,
            "commit": h.get("commit") if h.get("ok") else None,
            "data_dir": h.get("data_dir") if h.get("ok") else None,
            "last_seen": h.get("server_time") if h.get("ok") else None,
            "error": h.get("error"),
        })
    return enriched
