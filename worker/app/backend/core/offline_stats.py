"""Privacy-safe offline statistics derived from the local job history.

The desktop history is intentionally capped at 30 records, so every value in
this module describes the *recent local history*, never a lifetime or server
total.  The aggregator has no network dependency and never exposes paths,
settings, identity fields, or telemetry credentials.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from core.render_usability import load_job_history


OFFLINE_STATS_SCHEMA_VERSION = 1
OFFLINE_STATS_SCOPE_LIMIT = 30
TC_IDS = ("TC01", "TC02", "TC03", "TC04", "TC05", "TC06")

_SUCCESS_STATES = {"SUCCEEDED", "SUCCESS", "DONE", "COMPLETED", "PASS"}
_ATTENTION_STATES = {
    "FAILED",
    "FAIL",
    "PARTIAL",
    "INVALID_INPUT",
    "ERROR",
}
_STOPPED_STATES = {"PAUSED", "CANCELLED", "CANCELED", "STOPPED"}


@dataclass(frozen=True)
class TcOfflineStats:
    jobs: int = 0
    succeeded: int = 0
    attention: int = 0
    stopped: int = 0
    outputs: int = 0
    elapsed_sec: float = 0.0


@dataclass(frozen=True)
class OfflineStatsSnapshot:
    schema_version: int
    scope_limit: int
    scope_label: str
    local_date: str
    total_jobs: int
    today_jobs: int
    succeeded_jobs: int
    attention_jobs: int
    stopped_jobs: int
    output_count: int
    today_outputs: int
    elapsed_sec: float
    success_rate_pct: float
    latest_at: str
    latest_tc: str
    latest_status: str
    ignored_records: int
    by_tc: dict[str, TcOfflineStats]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise_status(record: Mapping[str, Any]) -> str:
    pipeline_result = record.get("pipeline_result")
    if isinstance(pipeline_result, Mapping):
        raw = pipeline_result.get("status")
        if raw is not None:
            status = str(raw).strip().upper()
            if status:
                return status
    return str(record.get("status") or "UNKNOWN").strip().upper() or "UNKNOWN"


def _status_bucket(status: str) -> str:
    if status in _SUCCESS_STATES:
        return "succeeded"
    if status in _STOPPED_STATES:
        return "stopped"
    if status in _ATTENTION_STATES:
        return "attention"
    return "attention"


def _published_output_count(raw_outputs: Any) -> int:
    if not isinstance(raw_outputs, Sequence) or isinstance(raw_outputs, (str, bytes)):
        return 0
    seen: set[str] = set()
    for raw in raw_outputs:
        value = str(raw or "").strip()
        if not value or ".partial." in value.casefold():
            continue
        seen.add(value.casefold())
    return len(seen)


def _elapsed_seconds(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value >= 0.0 else 0.0


def _record_date(created_at: Any) -> str:
    text = str(created_at or "").strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return ""


def summarize_job_history(
    records: Iterable[Mapping[str, Any]],
    *,
    today: date | str | None = None,
    scope_limit: int = OFFLINE_STATS_SCOPE_LIMIT,
) -> OfflineStatsSnapshot:
    """Return a deterministic, scalar-only summary of recent local jobs."""

    limit = max(1, int(scope_limit))
    local_date = (
        today.isoformat()
        if isinstance(today, date)
        else str(today or date.today().isoformat())
    )
    rows = list(records)[:limit]
    mutable_by_tc: dict[str, dict[str, float | int]] = {
        tc: {
            "jobs": 0,
            "succeeded": 0,
            "attention": 0,
            "stopped": 0,
            "outputs": 0,
            "elapsed_sec": 0.0,
        }
        for tc in TC_IDS
    }
    total_jobs = today_jobs = succeeded_jobs = attention_jobs = stopped_jobs = 0
    output_count = today_outputs = 0
    elapsed_sec = 0.0
    ignored_records = 0
    latest_at = latest_tc = latest_status = ""

    for record in rows:
        if not isinstance(record, Mapping):
            ignored_records += 1
            continue
        tc_id = str(record.get("label") or "").strip().upper()
        if tc_id not in mutable_by_tc:
            ignored_records += 1
            continue
        status = _normalise_status(record)
        bucket = _status_bucket(status)
        outputs = _published_output_count(record.get("outputs"))
        elapsed = _elapsed_seconds(record.get("elapsed_sec"))
        is_today = _record_date(record.get("created_at")) == local_date

        total_jobs += 1
        output_count += outputs
        elapsed_sec += elapsed
        mutable = mutable_by_tc[tc_id]
        mutable["jobs"] = int(mutable["jobs"]) + 1
        mutable[bucket] = int(mutable[bucket]) + 1
        mutable["outputs"] = int(mutable["outputs"]) + outputs
        mutable["elapsed_sec"] = float(mutable["elapsed_sec"]) + elapsed

        if bucket == "succeeded":
            succeeded_jobs += 1
        elif bucket == "stopped":
            stopped_jobs += 1
        else:
            attention_jobs += 1
        if is_today:
            today_jobs += 1
            today_outputs += outputs
        if not latest_tc:
            latest_at = str(record.get("created_at") or "").strip()
            latest_tc = tc_id
            latest_status = status

    by_tc = {
        tc: TcOfflineStats(
            jobs=int(values["jobs"]),
            succeeded=int(values["succeeded"]),
            attention=int(values["attention"]),
            stopped=int(values["stopped"]),
            outputs=int(values["outputs"]),
            elapsed_sec=round(float(values["elapsed_sec"]), 3),
        )
        for tc, values in mutable_by_tc.items()
    }
    success_rate = (succeeded_jobs / total_jobs * 100.0) if total_jobs else 0.0
    return OfflineStatsSnapshot(
        schema_version=OFFLINE_STATS_SCHEMA_VERSION,
        scope_limit=limit,
        scope_label=f"{limit} งานล่าสุดในเครื่อง",
        local_date=local_date,
        total_jobs=total_jobs,
        today_jobs=today_jobs,
        succeeded_jobs=succeeded_jobs,
        attention_jobs=attention_jobs,
        stopped_jobs=stopped_jobs,
        output_count=output_count,
        today_outputs=today_outputs,
        elapsed_sec=round(elapsed_sec, 3),
        success_rate_pct=round(success_rate, 1),
        latest_at=latest_at,
        latest_tc=latest_tc,
        latest_status=latest_status,
        ignored_records=ignored_records,
        by_tc=by_tc,
    )


def load_offline_stats(
    *,
    today: date | str | None = None,
    scope_limit: int = OFFLINE_STATS_SCOPE_LIMIT,
) -> OfflineStatsSnapshot:
    """Load and summarize local history.  This function never uses network I/O."""

    return summarize_job_history(
        load_job_history(limit=scope_limit),
        today=today,
        scope_limit=scope_limit,
    )


__all__ = [
    "OFFLINE_STATS_SCHEMA_VERSION",
    "OFFLINE_STATS_SCOPE_LIMIT",
    "TC_IDS",
    "TcOfflineStats",
    "OfflineStatsSnapshot",
    "summarize_job_history",
    "load_offline_stats",
]
