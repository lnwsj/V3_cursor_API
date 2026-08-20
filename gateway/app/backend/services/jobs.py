"""Job CRUD + status management (Phase 1.3 refactor)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .db import pg_conn


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------
TERMINAL_JOB_STATUSES = {"succeeded", "partial", "failed", "cancelled", "paused", "invalid_input"}
_STATUS_ALIASES = {
    "success": "succeeded",
    "completed": "succeeded",
    "done": "succeeded",
    "canceled": "cancelled",
    "invalid-input": "invalid_input",
}


def canonical_status(value: Any) -> str:
    status = str(value or "unknown").lower()
    return _STATUS_ALIASES.get(status, status)


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------
def insert_job(
    job_id: str,
    user_id: str,
    worker_id: str,
    tc: str,
    priority: int,
    max_retries: int,
    settings: Dict[str, Any],
) -> None:
    """Insert a newly-dispatched job row (status=queued)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO v3_jobs
                    (job_id, user_id, worker_id, tc, status, priority, max_retries, settings, created_at)
                    VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s, %s)""",
                (job_id, user_id, worker_id, tc, priority, max_retries,
                 json.dumps(settings), time.time()),
            )
        conn.commit()


def get_job(job_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch a job by id (optionally scoped to user)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            if user_id:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user_id))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            return cur.fetchone()


def get_job_owner(job_id: str) -> Optional[str]:
    """Return the user_id owning a job, or None if not found."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM v3_jobs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            return row["user_id"] if row else None


def list_jobs(
    user_id: Optional[str] = None,
    statuses: Optional[List[str]] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List jobs, optionally filtered by user_id and statuses."""
    where = []
    params: List[Any] = []
    if user_id:
        where.append("user_id = %s")
        params.append(user_id)
    if statuses:
        placeholders = ",".join(["%s"] * len(statuses))
        where.append(f"status IN ({placeholders})")
        params.extend(statuses)
    sql = "SELECT * FROM v3_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def list_active_for_user(user_id: str) -> int:
    """Count jobs currently in-flight for a user (queued or running)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM v3_jobs WHERE user_id=%s AND status IN ('queued','running')",
                (user_id,))
            row = cur.fetchone()
            return row["count"] if row else 0


def list_user_jobs(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """User-scoped job list (newest first)."""
    return list_jobs(user_id=user_id, limit=limit)


def list_live_jobs(limit: int = 100) -> List[Dict[str, Any]]:
    """All jobs in queued/running/cancelling state (admin)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM v3_jobs WHERE status IN ('queued','running','cancelling') "
                "ORDER BY created_at DESC LIMIT %s", (limit,))
            return list(cur.fetchall())



def list_active_jobs(limit: int = 100) -> List[Dict[str, Any]]:
    """All jobs currently in-flight (queued/running/cancelling) — used by lifespan reconcile (Phase 4 fix)."""
    return list_live_jobs(limit=limit)

# ---------------------------------------------------------------------------
# Job status updates
# ---------------------------------------------------------------------------
def mark_job_failed(job_id: str, message: str) -> None:
    """Mark job as failed (only if not already terminal)."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v3_jobs SET status='failed', error=%s, finished_at=%s "
                "WHERE job_id=%s AND status NOT IN "
                "('succeeded','partial','failed','cancelled','paused','invalid_input')",
                (str(message), time.time(), job_id),
            )
        conn.commit()


def mark_job_soft_deleted(job_id: str, user_id: str, admin: bool = False) -> bool:
    """Soft-delete: set status='deleted'. Returns True if a row was updated."""
    where = "WHERE job_id = %s" + ("" if admin else " AND user_id = %s")
    with pg_conn() as conn:
        with conn.cursor() as cur:
            if admin:
                cur.execute(where, (job_id,))
            else:
                cur.execute(where, (job_id, user_id))
            row = cur.fetchone()
        conn.commit()
    return row is not None


def get_retry_info(job_id: str) -> Optional[Dict[str, Any]]:
    """Read max_retries, retry_count, user_id, tc, settings for a job."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max_retries, retry_count, user_id, tc, settings "
                "FROM v3_jobs WHERE job_id=%s", (job_id,))
            return cur.fetchone()


def increment_retry(job_id: str, new_worker_id: str) -> None:
    """Increment retry_count + reset to queued + assign new worker."""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v3_jobs SET status='queued', worker_id=%s, "
                "retry_count=retry_count+1, error=NULL, started_at=NULL, finished_at=NULL "
                "WHERE job_id=%s",
                (new_worker_id, job_id))
        conn.commit()


def record_worker_status(job_id: str, data: Dict[str, Any]) -> str:
    """Persist one canonical Worker status snapshot. Returns the canonical status.

    Skips update if the job is already in a terminal state (prevents late
    callbacks from overwriting 'cancelled'/'succeeded' with stale progress).
    """
    status = canonical_status(data.get("status"))
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
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM v3_jobs WHERE job_id=%s FOR UPDATE", (job_id,))
            current_row = cur.fetchone()
            current_status = canonical_status(current_row["status"]) if current_row and current_row.get("status") else None
            if current_status in TERMINAL_JOB_STATUSES and current_status != status:
                conn.commit()
                return current_status
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
                    data.get("finished_at") if status in TERMINAL_JOB_STATUSES else None,
                    job_id,
                ),
            )
        conn.commit()
    return status


# ---------------------------------------------------------------------------
# Output file parsing
# ---------------------------------------------------------------------------
def output_names(row: Dict[str, Any], safe_output_name) -> List[str]:
    """Resolve output file names from a job row (handles output_files list + fallback)."""
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
        if safe_output_name:
            try:
                name = safe_output_name(raw_name)
            except Exception:
                continue
        else:
            from pathlib import Path
            name = Path(raw_name).name
        if name not in names:
            names.append(name)
    return names
