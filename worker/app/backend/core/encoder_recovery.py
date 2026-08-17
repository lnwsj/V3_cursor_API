"""Shared encoder-failure classification and strict evidence audit trail."""
from __future__ import annotations

import copy
import os
import threading
from typing import Any, Callable, Mapping, Optional, Sequence


HARDWARE_ENCODERS = frozenset(
    {"h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_qsv", "h264_amf"}
)
_RUNNER_WATCHDOG_MARKERS = (
    "ffmpeg idle >",
    "ffmpeg wall-clock >",
    "ffmpeg idle timeout reached",
    "ffmpeg wall-clock cap exceeded",
    "reframe task timeout budget exhausted",
)
_attempt_lock = threading.RLock()
_attempts: list[dict[str, Any]] = []
_active_session_id = ""
MAX_AUDIT_ATTEMPTS = 1000


def command_video_encoder(command: Sequence[Any]) -> str:
    values = [str(value) for value in command]
    for option in ("-c:v", "-codec:v", "-vcodec"):
        try:
            return values[values.index(option) + 1]
        except (ValueError, IndexError):
            continue
    return ""


def is_hardware_encoder(encoder: str) -> bool:
    return str(encoder).strip().lower() in HARDWARE_ENCODERS


def should_retry_with_cpu(
    command: Sequence[Any],
    result: Any,
    *,
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Allow one CPU retry only for a real hardware-encoder process failure."""

    if not is_hardware_encoder(command_video_encoder(command)):
        return False
    if bool(getattr(result, "success", False)) or bool(
        getattr(result, "cancelled", False)
    ):
        return False
    if int(getattr(result, "returncode", -1)) <= 0:
        return False
    try:
        if stop_check is not None and bool(stop_check()):
            return False
    except Exception:
        return False
    error = str(getattr(result, "error", "") or "").lower()
    return not any(marker in error for marker in _RUNNER_WATCHDOG_MARKERS)


def remove_partial(path: os.PathLike[str] | str) -> None:
    try:
        candidate = os.fspath(path)
        if candidate and os.path.isfile(candidate):
            os.remove(candidate)
    except OSError:
        pass


def start_encoder_audit(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("encoder audit session_id must be non-empty")
    global _active_session_id
    with _attempt_lock:
        _attempts.clear()
        _active_session_id = session_id.strip()


def end_encoder_audit(session_id: str) -> None:
    global _active_session_id
    with _attempt_lock:
        if _active_session_id == session_id:
            _active_session_id = ""


def record_encoder_attempt(
    command: Sequence[Any],
    result: Any,
    *,
    injected: bool = False,
    details: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    event = {
        "sequence": 0,
        "encoder": command_video_encoder(command),
        "success": bool(getattr(result, "success", False)),
        "returncode": int(getattr(result, "returncode", -1)),
        "cancelled": bool(getattr(result, "cancelled", False)),
        "error": str(getattr(result, "error", "") or "")[:1000],
        "output_path": str(command[-1]) if command else "",
        "injected": bool(injected),
        "details": dict(details or {}),
    }
    with _attempt_lock:
        if not _active_session_id:
            return {}
        event["session_id"] = _active_session_id
        event["sequence"] = len(_attempts) + 1
        _attempts.append(event)
        if len(_attempts) > MAX_AUDIT_ATTEMPTS:
            del _attempts[: len(_attempts) - MAX_AUDIT_ATTEMPTS]
    return copy.deepcopy(event)


def snapshot_encoder_attempts(session_id: str) -> list[dict[str, Any]]:
    with _attempt_lock:
        return copy.deepcopy(
            [
                attempt
                for attempt in _attempts
                if attempt.get("session_id") == session_id
            ]
        )


def evaluate_encoder_expectation(
    expectation: str,
    attempts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expectation = str(expectation or "").strip()
    hardware_success = any(
        is_hardware_encoder(str(item.get("encoder", "")))
        and item.get("success") is True
        for item in attempts
    )
    hardware_failure_indexes = [
        index
        for index, item in enumerate(attempts)
        if is_hardware_encoder(str(item.get("encoder", "")))
        and item.get("success") is False
        and item.get("cancelled") is not True
    ]
    cpu_success_indexes = [
        index
        for index, item in enumerate(attempts)
        if item.get("encoder") == "libx264" and item.get("success") is True
    ]
    fallback_sequence = any(
        hardware_index < cpu_index
        for hardware_index in hardware_failure_indexes
        for cpu_index in cpu_success_indexes
    )
    if expectation == "gpu_success":
        passed = hardware_success
    elif expectation == "gpu_to_cpu_fallback":
        passed = fallback_sequence
    elif expectation:
        passed = False
    else:
        passed = True
    return {
        "expectation": expectation,
        "attempts": [dict(item) for item in attempts],
        "hardware_success": hardware_success,
        "gpu_to_cpu_fallback": fallback_sequence,
        "passed": passed,
    }


__all__ = [
    "HARDWARE_ENCODERS",
    "command_video_encoder",
    "end_encoder_audit",
    "evaluate_encoder_expectation",
    "is_hardware_encoder",
    "record_encoder_attempt",
    "remove_partial",
    "start_encoder_audit",
    "should_retry_with_cpu",
    "snapshot_encoder_attempts",
]
