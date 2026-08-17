"""
Deterministic render checkpoint storage.

The legacy public API (``save_checkpoint`` / ``load_checkpoint``) remains for
older callers.  New pipelines should use the schema-v2 document API so a
completed output is bound to a stable task identity instead of being guessed
from list position.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


_CHECKPOINT_FILENAME = ".render_checkpoint.json"
_PAUSE_MARKER = ".render_paused"
CHECKPOINT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CheckpointLoadResult:
    """Result of reading the checkpoint file without destroying evidence."""

    kind: str
    data: Optional[Dict[str, Any]] = None
    reason: str = ""
    path: str = ""


def _checkpoint_path(out_dir: str) -> Path:
    return Path(out_dir) / _CHECKPOINT_FILENAME


def _json_value(value: Any) -> Any:
    """Convert common Python values to deterministic JSON-compatible values."""

    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return os.fspath(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def stable_json_hash(value: Any) -> str:
    """Return a SHA-256 hash of canonical UTF-8 JSON."""

    payload = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalized_path(path: str) -> str:
    """Normalize a path for identity comparisons without requiring existence."""

    return os.path.normcase(os.path.abspath(os.fspath(path)))


def file_signature(path: str, *, include_sha256: bool = False) -> Dict[str, Any]:
    """Capture path, size and mtime; optionally include a content hash."""

    normalized = normalized_path(path)
    signature: Dict[str, Any] = {
        "path": normalized,
        "exists": False,
        "size": None,
        "mtime_ns": None,
    }
    try:
        stat = os.stat(normalized)
    except OSError:
        return signature

    signature.update(
        {
            "exists": True,
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
    )
    if include_sha256:
        digest = hashlib.sha256()
        try:
            with open(normalized, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            signature.update({"exists": False, "sha256": None})
        else:
            signature["sha256"] = digest.hexdigest()
    return signature


def build_job_fingerprint(
    *,
    pipeline: str,
    inputs: Mapping[str, Sequence[str]],
    settings: Mapping[str, Any],
) -> str:
    """Bind a job to ordered input metadata plus effective settings."""

    input_metadata = {
        str(group): [file_signature(path) for path in paths]
        for group, paths in sorted(inputs.items())
    }
    return stable_json_hash(
        {
            "pipeline": str(pipeline),
            "inputs": input_metadata,
            "settings": _json_value(settings),
        }
    )


def build_task_id(
    *,
    pipeline: str,
    slot: int,
    source_signature: Mapping[str, Any],
    settings_hash: str,
) -> str:
    """Build a stable task identity; slot keeps duplicate basenames distinct."""

    return stable_json_hash(
        {
            "pipeline": str(pipeline),
            "slot": int(slot),
            "source": _json_value(source_signature),
            "settings_hash": str(settings_hash),
        }
    )


def validate_completed_output(path: str, expected: Mapping[str, Any]) -> bool:
    """Accept a resumed output only when path, size, mtime and SHA-256 match."""

    if not isinstance(expected, Mapping):
        return False
    try:
        if normalized_path(path) != normalized_path(str(expected.get("path", ""))):
            return False
    except (TypeError, ValueError):
        return False
    actual = file_signature(path, include_sha256=True)
    if not actual.get("exists") or int(actual.get("size") or 0) <= 0:
        return False
    return all(
        actual.get(key) == expected.get(key)
        for key in ("size", "mtime_ns", "sha256")
    )


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                _json_value(data),
                handle,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def save_checkpoint_document(out_dir: str, data: Mapping[str, Any]) -> None:
    """Atomically persist a schema-v2 checkpoint or raise on invalid data."""

    if not out_dir:
        raise ValueError("out_dir is required")
    if not isinstance(data, Mapping):
        raise TypeError("checkpoint data must be a mapping")
    if data.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("checkpoint schema_version must be 2")
    if not isinstance(data.get("tasks"), Mapping):
        raise ValueError("checkpoint tasks must be a mapping")
    _atomic_write_json(_checkpoint_path(out_dir), data)


def load_checkpoint_document(out_dir: str) -> CheckpointLoadResult:
    """Classify the on-disk checkpoint as missing, v2, legacy or corrupt."""

    if not out_dir:
        return CheckpointLoadResult(kind="missing", reason="empty output directory")
    path = _checkpoint_path(out_dir)
    if not path.is_file():
        return CheckpointLoadResult(kind="missing", path=str(path))
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        return CheckpointLoadResult(
            kind="corrupt",
            reason=f"invalid checkpoint JSON: {exc}",
            path=str(path),
        )
    if not isinstance(data, dict):
        return CheckpointLoadResult(
            kind="corrupt",
            reason="checkpoint root must be an object",
            path=str(path),
        )
    if data.get("schema_version") == CHECKPOINT_SCHEMA_VERSION:
        return CheckpointLoadResult(kind="v2", data=data, path=str(path))
    if "completed" in data and "schema_version" not in data:
        return CheckpointLoadResult(
            kind="legacy",
            data=data,
            reason="legacy checkpoint has no deterministic task mapping",
            path=str(path),
        )
    return CheckpointLoadResult(
        kind="unsupported",
        data=data,
        reason=f"unsupported checkpoint schema: {data.get('schema_version')!r}",
        path=str(path),
    )


def archive_checkpoint(out_dir: str, reason: str) -> Optional[str]:
    """Move an unusable checkpoint aside; never remove rendered outputs.

    FIX (B-18, 2026-07-31): bounded retry with backoff. On Windows, the
    checkpoint file may be open by another process (backup tool, virus
    scanner, the user opening it in Notepad). The first ``os.replace``
    raises PermissionError; retrying a few times covers the typical
    handle-release window. If all retries fail we return None and log
    so the caller can decide what to do.
    """

    if not out_dir:
        return None
    source = _checkpoint_path(out_dir)
    if not source.is_file():
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(reason).lower()).strip("-")[:32]
    slug = slug or "unusable"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = source.with_name(f"{source.stem}.{slug}.{stamp}{source.suffix}")
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            os.replace(source, target)
            return str(target)
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                import time as _time
                _time.sleep(0.05 * (attempt + 1))
                continue
            break
    # All retries failed. Log and return None — caller decides whether
    # to surface the failure to the UI.
    if last_error is not None:
        try:
            import logging
            logging.getLogger(__name__).warning(
                "archive_checkpoint failed for %s: %s", source, last_error,
            )
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Legacy compatibility API
# ---------------------------------------------------------------------------

def save_checkpoint(out_dir: str, completed: List[str]) -> None:
    """Write the legacy list checkpoint for callers not yet migrated."""

    if not out_dir:
        return
    try:
        _atomic_write_json(
            _checkpoint_path(out_dir),
            {
                "completed": list(completed),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "count": len(completed),
            },
        )
    except Exception:
        pass


def load_checkpoint(out_dir: str) -> List[str]:
    """Return existing completed paths from either legacy or v2 documents."""

    loaded = load_checkpoint_document(out_dir)
    if loaded.kind == "legacy" and isinstance(loaded.data, dict):
        completed = loaded.data.get("completed", [])
        if not isinstance(completed, list):
            return []
        return [
            path
            for path in completed
            if isinstance(path, str) and os.path.isfile(path)
        ]
    if loaded.kind == "v2" and isinstance(loaded.data, dict):
        tasks = loaded.data.get("tasks", {})
        if not isinstance(tasks, dict):
            return []
        completed_paths: List[str] = []
        for task in tasks.values():
            if not isinstance(task, dict) or task.get("status") != "completed":
                continue
            path = task.get("output_path")
            if isinstance(path, str) and os.path.isfile(path):
                completed_paths.append(path)
        return completed_paths
    return []


def clear_checkpoint(out_dir: str) -> None:
    """Delete only the active checkpoint after a fully successful job."""

    try:
        path = _checkpoint_path(out_dir)
        if path.is_file():
            path.unlink()
    except Exception:
        pass


def is_paused(out_dir: str) -> bool:
    if not out_dir:
        return False
    try:
        return (Path(out_dir) / _PAUSE_MARKER).is_file()
    except Exception:
        return False


def set_paused(out_dir: str, paused: bool) -> None:
    if not out_dir:
        return
    try:
        marker = Path(out_dir) / _PAUSE_MARKER
        if paused:
            marker.write_text(
                datetime.now().isoformat(timespec="seconds"),
                encoding="utf-8",
            )
        elif marker.is_file():
            marker.unlink()
    except Exception:
        pass


def get_checkpoint_info(out_dir: str) -> dict:
    loaded = load_checkpoint_document(out_dir)
    completed = load_checkpoint(out_dir)
    return {
        "completed_count": len(completed),
        "paused": is_paused(out_dir),
        "path": str(_checkpoint_path(out_dir)),
        "kind": loaded.kind,
        "schema_version": (
            loaded.data.get("schema_version")
            if isinstance(loaded.data, dict)
            else None
        ),
        "reason": loaded.reason,
    }