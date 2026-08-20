"""Cluster router (Phase 3.2)."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from ..deps import _verify_internal
from ..services.jobs import list_live_jobs
from ..services.metrics import (
    anonymize_workers,
    job_metrics,
    live_jobs_feed,
    public_metrics_view,
)
from ..services.workers import (
    add_worker as _add_worker,
    init_workers,
    load_workers,
    probe_all_workers,
    probe_all_workers_with_inflight,
    remove_worker,
    update_worker as _update_worker,
    worker_health,
)


router = APIRouter()


@router.get("/api/cluster/health")
async def cluster_health():
    """Public cluster health summary."""
    workers = init_workers()
    if not workers:
        return {"ok": True, "workers": [], "summary": {"total": 0, "healthy": 0, "active": 0}}
    enriched = await probe_all_workers(workers, "")
    healthy = sum(1 for w in enriched if w["healthy"])
    enabled = sum(1 for w in enriched if w["enabled"])
    active = sum(w["active_jobs"] for w in enriched)
    return {
        "ok": True,
        "cluster": [
            {
                "slot": i + 1, "max_concurrent": w["max_concurrent"],
                "active": w["active_jobs"], "healthy": w["healthy"], "enabled": w["enabled"],
            }
            for i, w in enumerate(enriched)
        ],
        "summary": {
            "total": len(enriched), "enabled": enabled, "healthy": healthy, "active": active,
        },
    }


@router.post("/api/cluster/workers/reload")
async def cluster_workers_reload():
    workers = load_workers()
    return {"ok": True, "count": len(workers)}


@router.post("/api/cluster/workers")
async def cluster_workers_add(worker: Dict[str, Any]):
    workers = _add_worker(worker)
    return {"ok": True, "total": len(workers)}


@router.patch("/api/cluster/workers/{worker_id}")
async def cluster_workers_update(worker_id: str, fields: Dict[str, Any]):
    updated = _update_worker(worker_id, **fields)
    if not updated:
        raise HTTPException(404, f"worker '{worker_id}' not found")
    return {"ok": True, "worker": updated}


@router.delete("/api/cluster/workers/{worker_id}")
async def cluster_workers_delete(worker_id: str):
    workers = remove_worker(worker_id)
    return {"ok": True, "removed": worker_id, "total": len(workers)}


@router.post("/api/cluster/workers/{worker_id}/test")
async def cluster_workers_test(worker_id: str, _: bool = Depends(_verify_internal)):
    workers = load_workers()
    target = next((w for w in workers if w["id"] == worker_id), None)
    if not target:
        raise HTTPException(404, f"worker '{worker_id}' not found")
    health = await worker_health(target, "")
    return {"ok": True, "url": target["url"], "data": health}


@router.get("/api/v1/workers/monitor")
async def workers_monitor(_: bool = Depends(_verify_internal)):
    """Extended monitor with in-flight jobs."""
    workers = init_workers()
    enriched = await probe_all_workers_with_inflight(workers, "")
    return {
        "ok": True,
        "workers": enriched,
        "summary": {
            "total": len(enriched),
            "enabled": sum(1 for w in enriched if w["enabled"]),
            "healthy": sum(1 for w in enriched if w["healthy"]),
            "active": sum(w["active_jobs"] for w in enriched),
        },
    }


@router.get("/api/cluster/dashboard")
async def cluster_dashboard(hours: int = 24, _: bool = Depends(_verify_internal)):
    """Comprehensive cluster dashboard (FIX 2026-08-19)."""
    workers = init_workers()
    enriched = await probe_all_workers_with_inflight(workers, "")
    live = await live_jobs_feed(limit=50)
    metrics = await job_metrics(hours=hours)
    enabled = sum(1 for w in enriched if w["enabled"])
    healthy = sum(1 for w in enriched if w["healthy"])
    return {
        "ok": True,
        "server_time": __import__("time").time(),
        "summary": {
            "total_workers": len(enriched),
            "enabled_workers": enabled,
            "healthy_workers": healthy,
            "down_workers": sum(1 for w in enriched if w["enabled"] and not w["healthy"]),
            "disabled_workers": sum(1 for w in enriched if not w["enabled"]),
            "total_capacity": sum(w["max_concurrent"] for w in enriched if w["enabled"]),
            "active_jobs": sum(w["active_jobs"] for w in enriched),
            "live_jobs_in_db": len(live),
        },
        "cluster": enriched,
        "live_jobs": live,
        "metrics": metrics,
    }


@router.get("/api/cluster/jobs/live")
async def cluster_jobs_live(_: bool = Depends(_verify_internal)):
    return {"ok": True, "server_time": __import__("time").time(), "jobs": list_live_jobs(limit=100)}


@router.get("/api/cluster/metrics")
async def cluster_metrics(hours: int = 24, _: bool = Depends(_verify_internal)):
    hours = max(1, min(hours, 168))
    return {"ok": True, "server_time": __import__("time").time(), "metrics": await job_metrics(hours=hours)}


@router.get("/api/cluster/public")
async def cluster_public(hours: int = 24):
    """Public cluster status (no auth) (FIX 2026-08-19)."""
    workers = init_workers()
    enriched = await probe_all_workers(workers, "")
    metrics = await job_metrics(hours=hours)
    enabled = sum(1 for w in enriched if w["enabled"])
    healthy = sum(1 for w in enriched if w["healthy"])
    return {
        "ok": True,
        "server_time": __import__("time").time(),
        "service": "V3 Cluster",
        "summary": {
            "total_nodes": len(enriched),
            "enabled_nodes": enabled,
            "online_nodes": healthy,
            "offline_nodes": sum(1 for w in enriched if w["enabled"] and not w["healthy"]),
            "disabled_nodes": sum(1 for w in enriched if not w["enabled"]),
            "total_capacity": sum(w["max_concurrent"] for w in enriched if w["enabled"]),
            "active_jobs": sum(w["active_jobs"] for w in enriched),
            "window_hours": hours,
        },
        "nodes": anonymize_workers(enriched),
        "metrics": public_metrics_view(metrics),
    }
