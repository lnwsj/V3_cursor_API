"""Metrics aggregation (per-TC / per-worker / hourly throughput)."""
from __future__ import annotations

import time
from typing import Any, Dict, List

from .db import pg_conn


# ---------------------------------------------------------------------------
# Aggregated metrics (windowed by `hours`)
# ---------------------------------------------------------------------------
async def job_metrics(hours: int = 24) -> Dict[str, Any]:
    """Per-TC latency + success rate + hourly throughput (last `hours`)."""
    now = time.time()
    since = now - hours * 3600
    with pg_conn() as conn:
        with conn.cursor() as cur:
            # Per-TC stats
            cur.execute("""
                SELECT tc,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status IN ('succeeded','SUCCEEDED')) AS ok,
                       COUNT(*) FILTER (WHERE status='failed') AS fail,
                       COUNT(*) FILTER (WHERE status='INVALID_INPUT') AS invalid,
                       ROUND(AVG(finished_at - started_at) FILTER (WHERE finished_at > started_at AND status IN ('succeeded','SUCCEEDED')))::int AS avg_sec,
                       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY finished_at - started_at)
                             FILTER (WHERE finished_at > started_at AND status IN ('succeeded','SUCCEEDED')))::int AS p50_sec,
                       ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY finished_at - started_at)
                             FILTER (WHERE finished_at > started_at AND status IN ('succeeded','SUCCEEDED')))::int AS p95_sec,
                       ROUND(AVG(output_size))::bigint AS avg_bytes
                FROM v3_jobs
                WHERE created_at > %s
                GROUP BY tc ORDER BY tc
            """, (since,))
            tc_stats = []
            for row in cur.fetchall():
                tc_stats.append({
                    "tc": row["tc"], "total": row["total"], "ok": row["ok"],
                    "fail": row["fail"], "invalid": row["invalid"],
                    "avg_sec": row["avg_sec"] or 0, "p50_sec": row["p50_sec"] or 0,
                    "p95_sec": row["p95_sec"] or 0, "avg_bytes": row["avg_bytes"] or 0,
                    "success_rate": round(100.0 * row["ok"] / max(row["total"], 1), 1),
                })

            # Per-worker stats
            cur.execute("""
                SELECT worker_id,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status IN ('succeeded','SUCCEEDED')) AS ok,
                       ROUND(AVG(finished_at - started_at) FILTER (WHERE finished_at > started_at))::int AS avg_sec
                FROM v3_jobs
                WHERE created_at > %s AND worker_id IS NOT NULL AND worker_id <> ''
                GROUP BY worker_id ORDER BY total DESC
            """, (since,))
            worker_stats = []
            for row in cur.fetchall():
                worker_stats.append({
                    "worker_id": row["worker_id"], "total": row["total"],
                    "ok": row["ok"], "avg_sec": row["avg_sec"] or 0,
                    "success_rate": round(100.0 * row["ok"] / max(row["total"], 1), 1),
                })

            # Hourly throughput
            cur.execute("""
                SELECT
                    EXTRACT(EPOCH FROM date_trunc('hour', to_timestamp(created_at)))::bigint AS hour_epoch,
                    COUNT(*) AS n_jobs,
                    COUNT(*) FILTER (WHERE status IN ('succeeded','SUCCEEDED')) AS n_ok
                FROM v3_jobs
                WHERE created_at > %s
                GROUP BY 1 ORDER BY 1
            """, (since,))
            hourly = [{"hour": row["hour_epoch"], "total": row["n_jobs"], "ok": row["n_ok"]} for row in cur.fetchall()]

            # Total summary
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status IN ('succeeded','SUCCEEDED')) AS ok,
                    COUNT(*) FILTER (WHERE status='failed') AS failed,
                    COUNT(*) FILTER (WHERE status='INVALID_INPUT') AS invalid
                FROM v3_jobs WHERE created_at > %s
            """, (since,))
            row = cur.fetchone()
            totals = {
                "total": row["total"], "ok": row["ok"],
                "failed": row["failed"], "invalid": row["invalid"],
                "success_rate": round(100.0 * row["ok"] / max(row["total"], 1), 1),
            }
    return {
        "window_hours": hours,
        "totals": totals,
        "by_tc": tc_stats,
        "by_worker": worker_stats,
        "hourly_throughput": hourly,
    }


# ---------------------------------------------------------------------------
# Public view (anonymize worker IDs for /v3api/api/cluster/public)
# ---------------------------------------------------------------------------
TIER_LABELS = {"low": "Standard", "mid": "Performance", "high": "Compute+GPU"}
GPU_ENCODER_PREFIXES = ("h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_videotoolbox")


def anonymize_workers(workers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip URLs/IPs/hostnames/internal IDs. Return only safe public fields."""
    out = []
    for i, w in enumerate(workers, start=1):
        tier = (w.get("tier") or "low").lower()
        tier_label = TIER_LABELS.get(tier, "Compute")
        encoder = w.get("encoder") or ""
        out.append({
            "name": f"Node-{i}",
            "tier": tier_label,
            "tier_tone": tier,
            "enabled": w.get("enabled", True),
            "healthy": w.get("healthy", False),
            "active_jobs": w.get("active_jobs", 0),
            "max_concurrent": w.get("max_concurrent", 1),
            "encoder_kind": "GPU" if encoder.startswith(GPU_ENCODER_PREFIXES) else "CPU",
            "last_seen_ago": (
                int(time.time() - w["last_seen"]) if w.get("last_seen") else None
            ),
        })
    return out


def public_metrics_view(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Filter internal metric view → only public-safe aggregates."""
    # Per-TC stats are OK to expose (no IP/PII)
    by_tc_public = []
    for t in metrics.get("by_tc", []):
        by_tc_public.append({
            "tc": t["tc"], "total": t["total"], "ok": t["ok"],
            "fail": t["fail"], "invalid": t["invalid"],
            "avg_sec": t["avg_sec"], "p50_sec": t["p50_sec"], "p95_sec": t["p95_sec"],
            "success_rate": t["success_rate"],
        })
    # Per-worker stats: keep aggregate only (rename to Node-N, drop worker_id)
    by_node_public = []
    for i, w in enumerate(metrics.get("by_worker", []), start=1):
        by_node_public.append({
            "node": f"Node-{i}",
            "total": w["total"], "ok": w["ok"],
            "avg_sec": w["avg_sec"], "success_rate": w["success_rate"],
        })
    return {
        "window_hours": metrics.get("window_hours", 24),
        "totals": metrics.get("totals", {}),
        "by_tc": by_tc_public,
        "by_node": by_node_public,
        "hourly_throughput": metrics.get("hourly_throughput", []),
    }


# ---------------------------------------------------------------------------
# Live jobs feed (for /api/cluster/jobs/live)
# ---------------------------------------------------------------------------
async def live_jobs_feed(limit: int = 50) -> List[Dict[str, Any]]:
    """Real-time running/queued jobs from PG (for admin dashboard)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT job_id, user_id, worker_id, tc, status, progress,
                       created_at, started_at, finished_at, error
                FROM v3_jobs
                WHERE status IN ('queued','running','paused')
                ORDER BY created_at DESC LIMIT %s
            """, (limit,))
            jobs = []
            for row in cur.fetchall():
                jobs.append({
                    "job_id": row["job_id"],
                    "user_id": row["user_id"],
                    "worker_id": row["worker_id"],
                    "tc": row["tc"],
                    "status": row["status"],
                    "progress": float(row["progress"]) if row["progress"] is not None else 0.0,
                    "created_at": float(row["created_at"]) if row["created_at"] else None,
                    "started_at": float(row["started_at"]) if row["started_at"] else None,
                    "elapsed_sec": (
                        round(time.time() - float(row["started_at"]), 1) if row["started_at"] else
                        round(time.time() - float(row["created_at"]), 1) if row["created_at"] else 0
                    ),
                    "error": row["error"],
                })
            return jobs
