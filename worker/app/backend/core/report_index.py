"""Fail-closed evidence bundle validation and latest-pointer promotion.

Audit and release pointers are deliberately separate.  A structurally useful
audit may be indexed even when it reports FAIL/BLOCKED, while ``latest_release``
requires current packaged-Tk, real-FFmpeg, six-TC paired evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


REQUIRED_TCS: Tuple[str, ...] = (
    "TC01",
    "TC02",
    "TC03",
    "TC04",
    "TC05",
    "TC06",
)
REQUIRED_SCREENSHOT_STEPS: Tuple[str, ...] = (
    "01_open_page",
    "02_input_ready",
    "03_click_generate",
    "04_result_state",
    "05_audio_ready_or_error",
)
REQUIRED_ROOT_FILES: Tuple[str, ...] = (
    "report.md",
    "report.html",
    "summary.json",
    "test_matrix.json",
)
REQUIRED_DIRS: Tuple[str, ...] = (
    "screenshots",
    "api",
    "pairs",
    "logs",
)
COMPLETION_MARKER = "release.complete.json"
V20_CHROMA_DEFAULTS: Dict[str, Any] = {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "bitrate": "6000k",
    "encoder": "h264_nvenc",
    "preset": "medium",
    "key_color": "#00FF00",
    "similarity": 0.29,
    "blend": 0.04,
    "despill": 0.32,
}
HARDWARE_ENCODERS = frozenset(
    {"h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_qsv", "h264_amf"}
)


@dataclass
class ReportValidation:
    report_dir: str
    kind: str
    eligible: bool
    reasons: List[str] = field(default_factory=list)
    run_id: str = ""
    app_version: str = ""
    pair_completeness_pct: float = 0.0
    pair_consistency_pct: float = 0.0
    pairs_total: int = 0
    pairs_complete: int = 0
    pairs_consistent: int = 0
    outputs_expected: int = 0
    outputs_valid: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
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


def _load_json(path: Path, reasons: List[str], label: str) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        reasons.append(f"missing {label}: {path}")
        return None
    try:
        if path.stat().st_size <= 0:
            reasons.append(f"zero-byte {label}: {path}")
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        reasons.append(f"invalid {label}: {path}: {exc}")
        return None
    if not isinstance(value, dict) or not value:
        reasons.append(f"empty/non-object {label}: {path}")
        return None
    return value


def _nonzero_file(path: Path, reasons: List[str], label: str) -> bool:
    try:
        valid = path.is_file() and path.stat().st_size > 0
    except OSError:
        valid = False
    if not valid:
        reasons.append(f"missing/zero-byte {label}: {path}")
    return valid


def _inside_reference(
    report_dir: Path,
    raw: Any,
    reasons: List[str],
    label: str,
) -> Optional[Path]:
    if not isinstance(raw, str) or not raw.strip():
        reasons.append(f"missing {label} reference")
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = report_dir / candidate
    try:
        resolved = candidate.resolve()
        base = report_dir.resolve()
        if os.path.commonpath((str(base), str(resolved))) != str(base):
            reasons.append(f"{label} escapes report directory: {raw}")
            return None
    except (OSError, ValueError) as exc:
        reasons.append(f"invalid {label} reference {raw!r}: {exc}")
        return None
    if not _nonzero_file(resolved, reasons, label):
        return None
    return resolved


def _output_reference(raw: Any, reasons: List[str], label: str) -> Optional[Path]:
    if not isinstance(raw, str) or not raw.strip():
        reasons.append(f"missing {label} path")
        return None
    try:
        path = Path(raw).resolve()
    except (OSError, ValueError) as exc:
        reasons.append(f"invalid {label} path {raw!r}: {exc}")
        return None
    if not _nonzero_file(path, reasons, label):
        return None
    return path


def _structural_validation(report_dir: Path) -> Tuple[List[str], Dict[str, Any], Dict[str, Any]]:
    reasons: List[str] = []
    if not report_dir.is_dir():
        return [f"report directory does not exist: {report_dir}"], {}, {}
    for filename in REQUIRED_ROOT_FILES:
        _nonzero_file(report_dir / filename, reasons, filename)
    for dirname in REQUIRED_DIRS:
        if not (report_dir / dirname).is_dir():
            reasons.append(f"missing required directory: {dirname}")
    summary = _load_json(report_dir / "summary.json", reasons, "summary.json") or {}
    matrix = _load_json(report_dir / "test_matrix.json", reasons, "test_matrix.json") or {}
    if not isinstance(summary.get("run_id"), str) or not summary.get("run_id"):
        reasons.append("summary.run_id is required")
    if not isinstance(summary.get("status"), str) or not summary.get("status"):
        reasons.append("summary.status is required")
    return reasons, summary, matrix


def validate_audit_bundle(report_dir: os.PathLike[str] | str) -> ReportValidation:
    root = Path(report_dir).resolve()
    reasons, summary, _matrix = _structural_validation(root)
    return ReportValidation(
        report_dir=str(root),
        kind="audit",
        eligible=not reasons,
        reasons=reasons,
        run_id=str(summary.get("run_id", "")),
        app_version=str(summary.get("app_version", "")),
    )


def _require_equal(
    values: Iterable[Tuple[str, Any]],
    reasons: List[str],
    label: str,
) -> bool:
    items = list(values)
    if not items:
        reasons.append(f"{label} has no values")
        return False
    first = items[0][1]
    if first in (None, ""):
        reasons.append(f"{label} is missing")
        return False
    mismatched = [name for name, value in items if value != first]
    if mismatched:
        reasons.append(f"{label} mismatch at: {', '.join(mismatched)}")
        return False
    return True

def _validate_serialized_success(
    payload: Mapping[str, Any],
    *,
    label: str,
    reasons: List[str],
) -> Dict[str, int]:
    """Validate PipelineResult/StageResult counts without Python bool coercion."""

    count_names = (
        "expected",
        "succeeded",
        "validated_resumed",
        "failed",
        "skipped",
        "cancelled",
        "accounted",
        "completed",
        "valid_output_count",
    )
    counts: Dict[str, int] = {}
    for name in count_names:
        value = payload.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            reasons.append(f"{label} {name} must be a non-negative integer")
            counts[name] = 0
        else:
            counts[name] = value

    expected = counts["expected"]
    completed = counts["completed"]
    if expected <= 0:
        reasons.append(f"{label} expected must be > 0")
    derived_accounted = sum(
        counts[name]
        for name in (
            "succeeded",
            "validated_resumed",
            "failed",
            "skipped",
            "cancelled",
        )
    )
    if counts["accounted"] != expected or derived_accounted != expected:
        reasons.append(f"{label} accounted count invariant mismatch")
    if counts["succeeded"] + counts["validated_resumed"] != completed:
        reasons.append(f"{label} produced/resumed count invariant mismatch")
    if completed != expected or counts["valid_output_count"] != expected:
        reasons.append(f"{label} completion/valid count mismatch")
    for name in ("failed", "skipped", "cancelled"):
        if counts[name] != 0:
            reasons.append(f"{label} {name} must be zero")
    produced = payload.get("produced_this_run")
    if produced is not None and (
        not isinstance(produced, int)
        or isinstance(produced, bool)
        or produced != counts["succeeded"]
    ):
        reasons.append(f"{label} produced_this_run mismatch")

    if payload.get("status") != "SUCCEEDED" or payload.get("is_success") is not True:
        reasons.append(f"{label} is not SUCCEEDED")
    if payload.get("finalized") is not True:
        reasons.append(f"{label} is not finalized")
    if payload.get("outputs_validated") is not True:
        reasons.append(f"{label} outputs_validated is not true")
    if payload.get("invalid_outputs") != []:
        reasons.append(f"{label} invalid_outputs is not empty")
    for error_name in ("errors", "invariant_errors", "all_errors"):
        if payload.get(error_name) != []:
            reasons.append(f"{label} {error_name} is not empty")

    serialized_outputs = payload.get("outputs")
    if not isinstance(serialized_outputs, list):
        reasons.append(f"{label} outputs must be a list")
        serialized_outputs = []
    if len(serialized_outputs) != completed:
        reasons.append(f"{label} serialized output count mismatch")
    normalized_outputs: List[str] = []
    for index, raw_path in enumerate(serialized_outputs):
        path = _output_reference(raw_path, reasons, f"{label} output[{index}]")
        if path is not None:
            normalized_outputs.append(str(path))
    if len(set(normalized_outputs)) != len(normalized_outputs):
        reasons.append(f"{label} serialized output paths are not unique")
    return counts


def _validate_v20_cover_once(
    report_dir: Path,
    *,
    tc_id: str,
    pair_id: str,
    binding: Mapping[str, Any],
    response: Mapping[str, Any],
    report_html: str,
    reasons: List[str],
) -> Tuple[bool, bool]:
    """Validate the TC01 every-frame Cover-once proof fail-closed."""

    complete = True
    consistent = True
    binding_checks = binding.get("media_contract_checks")
    response_checks = response.get("media_contract_checks")
    if binding_checks != response_checks:
        reasons.append(f"{tc_id} binding/response media contract checks mismatch")
        consistent = False
    if not isinstance(response_checks, dict):
        reasons.append(f"{tc_id} packaged media contract checks are required")
        response_checks = {}
        consistent = False
    cover_once = response_checks.get("cover_once")
    if not isinstance(cover_once, dict):
        reasons.append(f"{tc_id} Cover-once check is required")
        cover_once = {}
        consistent = False

    if cover_once.get("verdict") != "PASS" or cover_once.get("passed") is not True:
        reasons.append(f"{tc_id} Cover-once verdict is not PASS")
        consistent = False

    counts: Dict[str, int] = {}
    for name in ("frames_total", "cover_frames", "post_cover_frames"):
        value = cover_once.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            reasons.append(f"{tc_id} Cover-once {name} must be a positive integer")
            counts[name] = 0
            consistent = False
        else:
            counts[name] = value
    if counts["frames_total"] != counts["cover_frames"] + counts["post_cover_frames"]:
        reasons.append(f"{tc_id} Cover-once frame counts do not balance")
        consistent = False
    for flag in ("contiguous_cover_prefix", "all_frames_match_expected_window"):
        if cover_once.get(flag) is not True:
            reasons.append(f"{tc_id} Cover-once {flag} is not true")
            consistent = False

    evidence_paths: Dict[str, Optional[Path]] = {}
    for field_name, label in (
        ("artifact", "Cover-once artifact"),
        ("contact_sheet", "Cover-once contact sheet"),
    ):
        evidence_paths[field_name] = _inside_reference(
            report_dir,
            cover_once.get(field_name),
            reasons,
            f"{tc_id} {label}",
        )
        if evidence_paths[field_name] is None:
            complete = False
            continue
        expected_hash = cover_once.get(f"{field_name}_sha256")
        try:
            actual_hash = _sha256(evidence_paths[field_name])
        except OSError as exc:
            reasons.append(f"{tc_id} {label} hash failed: {exc}")
            complete = False
            continue
        if expected_hash != actual_hash:
            reasons.append(f"{tc_id} {label} sha256 mismatch")
            consistent = False

    contact_reference = cover_once.get("contact_sheet")
    if isinstance(contact_reference, str) and contact_reference:
        escaped = html.escape(contact_reference, quote=True)
        if (
            report_html.count(f'href="{escaped}"') != 1
            or report_html.count(f'src="{escaped}"') != 1
        ):
            reasons.append(
                f"{tc_id} report.html does not embed Cover-once contact sheet "
                f"exactly once: {contact_reference}"
            )
            complete = False

    artifact_path = evidence_paths.get("artifact")
    artifact = (
        _load_json(artifact_path, reasons, f"{tc_id} Cover-once artifact")
        if artifact_path is not None
        else None
    ) or {}
    if not artifact:
        complete = False
        return complete, False
    if artifact.get("pair_id") != pair_id:
        reasons.append(f"{tc_id} Cover-once artifact pair_id mismatch")
        consistent = False
    if artifact.get("verdict") != "PASS":
        reasons.append(f"{tc_id} Cover-once artifact verdict is not PASS")
        consistent = False
    if artifact.get("contact_sheet") != contact_reference:
        reasons.append(f"{tc_id} Cover-once artifact contact sheet mismatch")
        consistent = False
    for name, expected in counts.items():
        artifact_count = artifact.get(name)
        if (
            not isinstance(artifact_count, int)
            or isinstance(artifact_count, bool)
            or artifact_count != expected
        ):
            reasons.append(f"{tc_id} Cover-once artifact {name} mismatch")
            consistent = False
    for flag in ("contiguous_cover_prefix", "all_frames_match_expected_window"):
        if artifact.get(flag) is not True:
            reasons.append(f"{tc_id} Cover-once artifact {flag} is not true")
            consistent = False

    frames = artifact.get("frames")
    if not isinstance(frames, list):
        reasons.append(f"{tc_id} Cover-once artifact frames must be a list")
        frames = []
        consistent = False
    if len(frames) != counts["frames_total"]:
        reasons.append(f"{tc_id} Cover-once artifact frame record count mismatch")
        consistent = False

    observed_flags: List[bool] = []
    frame_references: List[str] = []
    previous_pts: Optional[float] = None
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            reasons.append(f"{tc_id} Cover-once frame[{index}] is not an object")
            consistent = False
            continue
        frame_index = frame.get("frame_index")
        if (
            not isinstance(frame_index, int)
            or isinstance(frame_index, bool)
            or frame_index != index
        ):
            reasons.append(f"{tc_id} Cover-once frame[{index}] index mismatch")
            consistent = False
        pts = frame.get("pts_sec")
        if (
            not isinstance(pts, (int, float))
            or isinstance(pts, bool)
            or not math.isfinite(float(pts))
            or pts < 0
        ):
            reasons.append(f"{tc_id} Cover-once frame[{index}] PTS is invalid")
            consistent = False
        else:
            numeric_pts = float(pts)
            if previous_pts is not None and numeric_pts < previous_pts:
                reasons.append(
                    f"{tc_id} Cover-once frame[{index}] PTS is not monotonic"
                )
                consistent = False
            previous_pts = numeric_pts

        expected_cover = frame.get("expected_cover")
        observed_cover = frame.get("observed_cover")
        if not isinstance(expected_cover, bool) or not isinstance(
            observed_cover, bool
        ):
            reasons.append(
                f"{tc_id} Cover-once frame[{index}] expected/observed flags "
                "must be boolean"
            )
            consistent = False
        else:
            observed_flags.append(observed_cover)
            if expected_cover != observed_cover:
                reasons.append(
                    f"{tc_id} Cover-once frame[{index}] expected/observed mismatch"
                )
                consistent = False
        if frame.get("matches_expected") is not True:
            reasons.append(
                f"{tc_id} Cover-once frame[{index}] matches_expected is not true"
            )
            consistent = False

        frame_reference = frame.get("frame")
        frame_path = _inside_reference(
            report_dir,
            frame_reference,
            reasons,
            f"{tc_id} Cover-once frame[{index}] image",
        )
        if frame_path is None:
            complete = False
            continue
        frame_references.append(str(frame_reference))
        try:
            actual_hash = _sha256(frame_path)
        except OSError as exc:
            reasons.append(
                f"{tc_id} Cover-once frame[{index}] hash failed: {exc}"
            )
            complete = False
            continue
        if frame.get("frame_sha256") != actual_hash:
            reasons.append(f"{tc_id} Cover-once frame[{index}] sha256 mismatch")
            consistent = False

    if len(set(frame_references)) != len(frame_references):
        reasons.append(f"{tc_id} Cover-once frame references are not unique")
        consistent = False
    first_content = next(
        (index for index, is_cover in enumerate(observed_flags) if not is_cover),
        len(observed_flags),
    )
    sequence_valid = (
        len(observed_flags) == counts["frames_total"]
        and 0 < first_content < len(observed_flags)
        and all(observed_flags[:first_content])
        and not any(observed_flags[first_content:])
    )
    if not sequence_valid:
        reasons.append(
            f"{tc_id} Cover-once sequence is not one cover prefix followed by content"
        )
        consistent = False
    elif (
        first_content != counts["cover_frames"]
        or len(observed_flags) - first_content != counts["post_cover_frames"]
    ):
        reasons.append(f"{tc_id} Cover-once observed sequence/count mismatch")
        consistent = False

    return complete, consistent


def _validate_pair(
    report_dir: Path,
    tc_id: str,
    pair_id: str,
    *,
    require_v20_ui_contract: bool = False,
) -> Tuple[bool, bool, int, int, List[str]]:
    reasons: List[str] = []
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", pair_id) or ".." in pair_id:
        reasons.append(f"{tc_id} pair_id contains unsafe path characters")
        return False, False, 0, 0, reasons
    binding_path = _inside_reference(
        report_dir,
        f"pairs/{pair_id}__binding.json",
        reasons,
        f"{tc_id} pair binding",
    )
    binding = (
        _load_json(binding_path, reasons, f"{tc_id} pair binding")
        if binding_path is not None
        else None
    ) or {}

    complete = bool(binding)
    if binding.get("pair_id") != pair_id:
        reasons.append(f"{tc_id} binding pair_id mismatch")
    if binding.get("tc_id") != tc_id:
        reasons.append(f"{tc_id} binding tc_id mismatch")
    if binding.get("verdict") != "PASS":
        reasons.append(f"{tc_id} pair verdict is not PASS")
    if binding.get("actual_tk") is not True:
        reasons.append(f"{tc_id} missing actual Tk source evidence")

    screenshot_sets: Dict[str, List[str]] = {}
    for field_name, evidence_label in (
        ("screenshots", "browser screenshot"),
        ("actual_tk_screenshots", "actual Tk screenshot"),
    ):
        references = binding.get(field_name)
        if not isinstance(references, list) or len(references) < len(
            REQUIRED_SCREENSHOT_STEPS
        ):
            reasons.append(
                f"{tc_id} requires at least five {evidence_label} files"
            )
            references = []
            complete = False
        screenshot_sets[field_name] = [
            reference for reference in references if isinstance(reference, str)
        ]
        names = [Path(reference).name for reference in screenshot_sets[field_name]]
        for step in REQUIRED_SCREENSHOT_STEPS:
            if not any(step in name for name in names):
                reasons.append(
                    f"{tc_id} missing {evidence_label} step {step}"
                )
                complete = False
        for index, reference in enumerate(references):
            if _inside_reference(
                report_dir,
                reference,
                reasons,
                f"{tc_id} {evidence_label}[{index}]",
            ) is None:
                complete = False
    if set(screenshot_sets["screenshots"]) & set(
        screenshot_sets["actual_tk_screenshots"]
    ):
        reasons.append(f"{tc_id} browser and actual Tk screenshots must be distinct")
        complete = False
    if require_v20_ui_contract:
        try:
            report_html = (report_dir / "report.html").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            reasons.append(f"{tc_id} cannot read report.html gallery: {exc}")
            report_html = ""
            complete = False
        for field_name in ("screenshots", "actual_tk_screenshots"):
            for reference in screenshot_sets[field_name]:
                escaped = html.escape(reference, quote=True)
                if (
                    report_html.count(f'href="{escaped}"') != 1
                    or report_html.count(f'src="{escaped}"') != 1
                ):
                    reasons.append(
                        f"{tc_id} report.html does not embed screenshot exactly once: "
                        f"{reference}"
                    )
                    complete = False
    browser_evidence = _inside_reference(
        report_dir,
        binding.get("browser_evidence"),
        reasons,
        f"{tc_id} browser evidence",
    )
    if browser_evidence is None:
        complete = False

    api = binding.get("api")
    if not isinstance(api, dict):
        reasons.append(f"{tc_id} binding.api is required")
        api = {}
        complete = False
    api_paths: Dict[str, Optional[Path]] = {}
    for name in ("request", "response", "timing"):
        api_paths[name] = _inside_reference(
            report_dir,
            api.get(name),
            reasons,
            f"{tc_id} API {name}",
        )
        if api_paths[name] is None:
            complete = False
    request = (
        _load_json(api_paths["request"], reasons, f"{tc_id} request")
        if api_paths.get("request")
        else None
    ) or {}
    response = (
        _load_json(api_paths["response"], reasons, f"{tc_id} response")
        if api_paths.get("response")
        else None
    ) or {}
    timing = (
        _load_json(api_paths["timing"], reasons, f"{tc_id} timing")
        if api_paths.get("timing")
        else None
    ) or {}

    consistency = True
    common_fields = ("pair_id", "request_id", "tc_id", "mode", "text_hash", "input_hash", "settings_hash")
    for field_name in common_fields:
        if not _require_equal(
            (
                ("binding", binding.get(field_name)),
                ("request", request.get(field_name)),
                ("response", response.get(field_name)),
                ("timing", timing.get(field_name)),
            ),
            reasons,
            f"{tc_id} {field_name}",
        ):
            consistency = False
    if binding.get("request_id_match") is not True:
        reasons.append(f"{tc_id} request_id_match is not true")
        consistency = False
    if binding.get("mode_text_hash_match") is not True:
        reasons.append(f"{tc_id} mode_text_hash_match is not true")
        consistency = False
    if binding.get("pair_consistency") is not True:
        reasons.append(f"{tc_id} pair_consistency is not true")
        consistency = False

    if request.get("mode") != "real" or request.get("dry_run") is not False:
        reasons.append(f"{tc_id} request is not a real non-dry run")
        consistency = False
    execution = response.get("execution")
    if not isinstance(execution, dict):
        reasons.append(f"{tc_id} response.execution is required")
        execution = {}
    if execution.get("surface") != "packaged_tk":
        reasons.append(f"{tc_id} response is not packaged Tk execution")
        consistency = False
    if execution.get("real_ffmpeg") is not True or execution.get("dry_run") is not False:
        reasons.append(f"{tc_id} response is not real FFmpeg execution")
        consistency = False

    response_checks: Dict[str, Any] = {}
    if require_v20_ui_contract:
        binding_checks = binding.get("ui_contract_checks")
        raw_response_checks = response.get("ui_contract_checks")
        response_checks = (
            raw_response_checks if isinstance(raw_response_checks, dict) else {}
        )
        if binding_checks != response_checks:
            reasons.append(f"{tc_id} binding/response UI contract checks mismatch")
            consistency = False
        if not isinstance(raw_response_checks, dict):
            reasons.append(f"{tc_id} packaged UI contract checks are required")
            consistency = False

        request_settings = request.get("settings")
        if not isinstance(request_settings, dict):
            reasons.append(f"{tc_id} request.settings must be an object")
            consistency = False
        settings_application = response_checks.get("settings_application")
        if not isinstance(settings_application, dict):
            reasons.append(f"{tc_id} packaged settings application check is required")
            settings_application = {}
            consistency = False
        if settings_application.get("tc_id") != tc_id:
            reasons.append(f"{tc_id} settings application identity mismatch")
            consistency = False
        if settings_application.get("requested") != request_settings:
            reasons.append(f"{tc_id} requested settings do not match request.settings")
            consistency = False
        if settings_application.get("observed") != request_settings:
            reasons.append(f"{tc_id} observed settings do not match request.settings")
            consistency = False
        if settings_application.get("passed") is not True:
            reasons.append(f"{tc_id} settings application did not pass")
            consistency = False
        if settings_application.get("errors") != []:
            reasons.append(f"{tc_id} settings application errors must be empty")
            consistency = False

    if require_v20_ui_contract and tc_id in {"TC01", "TC02", "TC03", "TC04"}:
        chroma = response_checks.get("chroma_defaults_reset")
        if not isinstance(chroma, dict):
            reasons.append(f"{tc_id} packaged Chroma defaults/Reset check is required")
            chroma = {}
            consistency = False
        if chroma.get("tc_id") != tc_id or chroma.get("panel") != "video":
            reasons.append(f"{tc_id} packaged Chroma check identity mismatch")
            consistency = False
        expected_defaults = chroma.get("expected")
        if expected_defaults != V20_CHROMA_DEFAULTS:
            reasons.append(f"{tc_id} packaged Chroma expected defaults mismatch")
            consistency = False
        if (
            chroma.get("initial") != V20_CHROMA_DEFAULTS
            or chroma.get("after_reset") != V20_CHROMA_DEFAULTS
        ):
            reasons.append(f"{tc_id} packaged Chroma initial/Reset values mismatch")
            consistency = False
        for flag in (
            "initial_matches",
            "mutation_applied",
            "reset_invoked_through_button",
            "reset_matches",
            "passed",
        ):
            if chroma.get(flag) is not True:
                reasons.append(f"{tc_id} packaged Chroma {flag} is not true")
                consistency = False
        mutations = chroma.get("mutations")
        mutated = chroma.get("mutated")
        mutation_keys = {"key_color", "similarity", "blend", "despill"}
        if not isinstance(mutations, dict) or set(mutations) != mutation_keys:
            reasons.append(f"{tc_id} packaged Chroma mutation set mismatch")
            consistency = False
        elif (
            mutated != mutations
            or any(mutations[key] == V20_CHROMA_DEFAULTS[key] for key in mutation_keys)
        ):
            reasons.append(f"{tc_id} packaged Chroma mutation evidence mismatch")
            consistency = False

        required_encoder_expectation = {
            "TC01": "gpu_to_cpu_fallback",
            "TC02": "gpu_success",
        }.get(tc_id, "")
        if required_encoder_expectation:
            if execution.get("encoder_expectation") != required_encoder_expectation:
                reasons.append(f"{tc_id} encoder expectation declaration mismatch")
                consistency = False
            encoder_check = response_checks.get("encoder_recovery")
            if not isinstance(encoder_check, dict):
                reasons.append(f"{tc_id} packaged encoder recovery check is required")
                encoder_check = {}
                consistency = False
            if (
                encoder_check.get("expectation") != required_encoder_expectation
                or encoder_check.get("passed") is not True
                or encoder_check.get("audit_session_id") != pair_id
                or encoder_check.get("attempts_correlated") is not True
            ):
                reasons.append(f"{tc_id} packaged encoder expectation did not pass")
                consistency = False
            attempts = encoder_check.get("attempts")
            if not isinstance(attempts, list):
                attempts = []
            hardware_failures = [
                index
                for index, attempt in enumerate(attempts)
                if isinstance(attempt, dict)
                and attempt.get("encoder") in HARDWARE_ENCODERS
                and attempt.get("success") is False
                and attempt.get("cancelled") is not True
            ]
            hardware_successes = [
                index
                for index, attempt in enumerate(attempts)
                if isinstance(attempt, dict)
                and attempt.get("encoder") in HARDWARE_ENCODERS
                and attempt.get("success") is True
            ]
            cpu_successes = [
                index
                for index, attempt in enumerate(attempts)
                if isinstance(attempt, dict)
                and attempt.get("encoder") == "libx264"
                and attempt.get("success") is True
            ]
            if tc_id == "TC01":
                ordered_fallback = any(
                    hardware_index < cpu_index
                    for hardware_index in hardware_failures
                    for cpu_index in cpu_successes
                )
                injected_failure = any(
                    isinstance(attempt, dict)
                    and attempt.get("encoder") in HARDWARE_ENCODERS
                    and attempt.get("success") is False
                    and attempt.get("injected") is True
                    and isinstance(attempt.get("details"), dict)
                    and attempt["details"].get("failure_stage")
                    == "after_partial_and_progress"
                    and _positive_number(
                        attempt["details"].get("partial_bytes_before_failure")
                    )
                    and _positive_number(
                        attempt["details"].get("progress_pct_before_failure")
                    )
                    for attempt in attempts
                )
                if (
                    not ordered_fallback
                    or not injected_failure
                    or encoder_check.get("forced_hardware_failure_requested") is not True
                    or encoder_check.get("forced_hardware_failure_injected") is not True
                    or encoder_check.get("injected_partials_removed") is not True
                ):
                    reasons.append(f"{tc_id} GPU-to-CPU fallback sequence is invalid")
                    consistency = False
            elif not hardware_successes:
                reasons.append(f"{tc_id} fresh GPU success attempt is missing")
                consistency = False

    if require_v20_ui_contract and tc_id == "TC01":
        media_complete, media_consistent = _validate_v20_cover_once(
            report_dir,
            tc_id=tc_id,
            pair_id=pair_id,
            binding=binding,
            response=response,
            report_html=report_html,
            reasons=reasons,
        )
        complete = complete and media_complete
        consistency = consistency and media_consistent

    pipeline = response.get("pipeline_result")
    if not isinstance(pipeline, dict):
        reasons.append(f"{tc_id} pipeline_result is required")
        pipeline = {}
    pipeline_counts = _validate_serialized_success(
        pipeline,
        label=f"{tc_id} pipeline",
        reasons=reasons,
    )
    expected = pipeline_counts["expected"]
    stages = pipeline.get("stages", [])
    if not isinstance(stages, list):
        reasons.append(f"{tc_id} pipeline stages must be a list")
    else:
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                reasons.append(f"{tc_id} pipeline stage[{index}] is not an object")
                continue
            required = stage.get("required")
            if not isinstance(required, bool):
                reasons.append(f"{tc_id} pipeline stage[{index}].required must be boolean")
                continue
            if required:
                _validate_serialized_success(
                    stage,
                    label=f"{tc_id} required stage[{index}]",
                    reasons=reasons,
                )
    outputs = response.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        reasons.append(f"{tc_id} response outputs are empty")
        outputs = []
    valid_outputs = 0
    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            reasons.append(f"{tc_id} output[{index}] is not an object")
            continue
        path = _output_reference(output.get("path"), reasons, f"{tc_id} output[{index}]")
        if path is None:
            continue
        actual_size = path.stat().st_size
        if output.get("size") != actual_size:
            reasons.append(f"{tc_id} output[{index}] size mismatch")
            continue
        try:
            actual_hash = _sha256(path)
        except OSError as exc:
            reasons.append(f"{tc_id} output[{index}] hash failed: {exc}")
            continue
        if output.get("sha256") != actual_hash:
            reasons.append(f"{tc_id} output[{index}] sha256 mismatch")
            continue
        if output.get("ffprobe_valid") is not True:
            reasons.append(f"{tc_id} output[{index}] lacks ffprobe validation")
            continue
        valid_outputs += 1
    if len(outputs) != expected or valid_outputs != expected:
        reasons.append(
            f"{tc_id} response output validation mismatch expected={expected} records={len(outputs)} valid={valid_outputs}"
        )
    pipeline_outputs = pipeline.get("outputs")
    response_paths = [
        output.get("path")
        for output in outputs
        if isinstance(output, dict) and isinstance(output.get("path"), str)
    ]
    if not isinstance(pipeline_outputs, list) or len(pipeline_outputs) != expected:
        reasons.append(f"{tc_id} serialized pipeline outputs count mismatch")
    else:
        try:
            normalized_pipeline = [str(Path(path).resolve()) for path in pipeline_outputs]
            normalized_response = [str(Path(path).resolve()) for path in response_paths]
        except (TypeError, OSError, ValueError):
            reasons.append(f"{tc_id} serialized pipeline output path is invalid")
        else:
            if normalized_pipeline != normalized_response:
                reasons.append(f"{tc_id} pipeline/response output paths mismatch")

    if require_v20_ui_contract:
        expected_output_count = execution.get("expected_output_count")
        if (
            not isinstance(expected_output_count, int)
            or isinstance(expected_output_count, bool)
            or expected_output_count <= 0
        ):
            reasons.append(
                f"{tc_id} execution.expected_output_count must be a positive integer"
            )
            consistency = False
        else:
            if expected_output_count != expected:
                reasons.append(
                    f"{tc_id} execution.expected_output_count/pipeline expected mismatch"
                )
                consistency = False
            if (
                not isinstance(pipeline_outputs, list)
                or expected_output_count != len(pipeline_outputs)
            ):
                reasons.append(
                    f"{tc_id} execution.expected_output_count/pipeline output records mismatch"
                )
                consistency = False
            if expected_output_count != len(outputs):
                reasons.append(
                    f"{tc_id} execution.expected_output_count/response output records mismatch"
                )
                consistency = False

    elapsed_ms = timing.get("elapsed_ms")
    if not isinstance(elapsed_ms, (int, float)) or isinstance(elapsed_ms, bool) or elapsed_ms < 0:
        reasons.append(f"{tc_id} timing.elapsed_ms is invalid")
        consistency = False

    complete = complete and bool(request) and bool(response) and bool(timing)
    consistency = consistency and complete and not reasons
    return complete, consistency, int(expected), valid_outputs, reasons


def validate_release_bundle(
    report_dir: os.PathLike[str] | str,
    *,
    expected_version: str,
) -> ReportValidation:
    root = Path(report_dir).resolve()
    reasons, summary, matrix = _structural_validation(root)
    run_id = str(summary.get("run_id", ""))
    app_version = str(summary.get("app_version", ""))
    version_match = re.fullmatch(
        r"V(\d+)\.(\d+)\.(\d+)\.(\d+)",
        str(expected_version),
    )
    if version_match is None:
        reasons.append("expected_version must use V<major>.<minor>.<patch>.<build>")
        require_v20_ui_contract = False
    else:
        require_v20_ui_contract = tuple(
            int(value) for value in version_match.groups()
        ) >= (1, 0, 0, 20)

    if summary.get("status") != "PASS":
        reasons.append("release summary.status must be PASS")
    if summary.get("release_state") != "GO":
        reasons.append("release summary.release_state must be GO")
    if app_version != expected_version:
        reasons.append(
            f"release version mismatch expected={expected_version} actual={app_version or '<missing>'}"
        )
    revision = summary.get("source_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{7,40}", revision):
        reasons.append("summary.source_revision must be a git revision")
    if summary.get("worktree_dirty") is not False:
        reasons.append("release worktree_dirty must be false")
    if summary.get("errors") not in ([], None):
        reasons.append("release summary contains errors")
    if summary.get("critical_errors") != 0:
        reasons.append("release critical_errors must be zero")
    checks_total = summary.get("checks_total")
    checks_passed = summary.get("checks_passed")
    if not isinstance(checks_total, int) or checks_total <= 0 or checks_passed != checks_total:
        reasons.append("release checks_passed must equal positive checks_total")

    execution = summary.get("execution")
    if not isinstance(execution, dict):
        reasons.append("summary.execution is required")
        execution = {}
    if execution.get("surface") != "packaged_tk":
        reasons.append("summary execution surface must be packaged_tk")
    if execution.get("real_ffmpeg") is not True or execution.get("dry_run") is not False:
        reasons.append("summary execution must be real FFmpeg and non-dry-run")
    tested_tcs = execution.get("tested_tcs")
    if not isinstance(tested_tcs, list) or set(tested_tcs) != set(REQUIRED_TCS):
        reasons.append("summary execution.tested_tcs must contain TC01-TC06 exactly")
    package_path = _output_reference(
        execution.get("packaged_executable"),
        reasons,
        "packaged executable",
    )
    if package_path is not None:
        try:
            package_hash = _sha256(package_path)
        except OSError as exc:
            reasons.append(f"packaged executable hash failed: {exc}")
        else:
            if execution.get("packaged_sha256") != package_hash:
                reasons.append("packaged executable sha256 mismatch")
    for key in ("packaged_file_version", "packaged_product_version", "window_title_version"):
        if execution.get(key) != expected_version:
            reasons.append(f"summary execution.{key} must equal {expected_version}")

    cases = matrix.get("testcases")
    if not isinstance(cases, list):
        reasons.append("test_matrix.testcases must be a list")
        cases = []
    case_by_tc: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        if isinstance(case, dict) and isinstance(case.get("tc_id"), str):
            tc_id = case["tc_id"]
            if tc_id in case_by_tc:
                reasons.append(f"duplicate testcase in matrix: {tc_id}")
            case_by_tc[tc_id] = case
    if set(case_by_tc) != set(REQUIRED_TCS):
        reasons.append("test matrix must contain TC01-TC06 exactly")

    pairs_total = len(REQUIRED_TCS)
    pairs_complete = 0
    pairs_consistent = 0
    outputs_expected = 0
    outputs_valid = 0
    for tc_id in REQUIRED_TCS:
        case = case_by_tc.get(tc_id, {})
        pair_id = case.get("pair_id")
        if case.get("status") != "PASS":
            reasons.append(f"{tc_id} matrix status is not PASS")
        if not isinstance(pair_id, str) or not pair_id:
            reasons.append(f"{tc_id} matrix pair_id is required")
            continue
        complete, consistent, expected, valid, pair_reasons = _validate_pair(
            root,
            tc_id,
            pair_id,
            require_v20_ui_contract=require_v20_ui_contract,
        )
        reasons.extend(pair_reasons)
        pairs_complete += int(complete)
        pairs_consistent += int(consistent)
        outputs_expected += expected
        outputs_valid += valid

    completeness_pct = pairs_complete / pairs_total * 100.0
    consistency_pct = pairs_consistent / pairs_total * 100.0
    metrics = summary.get("pair_metrics")
    if not isinstance(metrics, dict):
        reasons.append("summary.pair_metrics is required")
        metrics = {}
    expected_metrics = {
        "pairs_total": pairs_total,
        "pairs_complete": pairs_complete,
        "pairs_consistent": pairs_consistent,
        "pair_completeness_pct": completeness_pct,
        "pair_consistency_pct": consistency_pct,
    }
    for key, value in expected_metrics.items():
        if metrics.get(key) != value:
            reasons.append(f"summary pair metric mismatch: {key}")
    if completeness_pct != 100.0:
        reasons.append("pair completeness is not 100%")
    if consistency_pct != 100.0:
        reasons.append("pair consistency is not 100%")
    if summary.get("outputs_expected") != outputs_expected:
        reasons.append("summary.outputs_expected mismatch")
    if summary.get("outputs_valid") != outputs_valid or outputs_valid != outputs_expected:
        reasons.append("summary.outputs_valid mismatch or incomplete")

    marker_name = summary.get("completion_marker")
    if marker_name != COMPLETION_MARKER:
        reasons.append(f"summary.completion_marker must be {COMPLETION_MARKER}")
    marker = _load_json(root / COMPLETION_MARKER, reasons, COMPLETION_MARKER) or {}
    if marker.get("complete") is not True:
        reasons.append("release completion marker is not complete")
    for key, expected in (
        ("run_id", run_id),
        ("app_version", expected_version),
        ("source_revision", revision),
    ):
        if marker.get(key) != expected:
            reasons.append(f"release completion marker {key} mismatch")

    reasons = list(dict.fromkeys(reasons))
    return ReportValidation(
        report_dir=str(root),
        kind="release",
        eligible=not reasons,
        reasons=reasons,
        run_id=run_id,
        app_version=app_version,
        pair_completeness_pct=completeness_pct,
        pair_consistency_pct=consistency_pct,
        pairs_total=pairs_total,
        pairs_complete=pairs_complete,
        pairs_consistent=pairs_consistent,
        outputs_expected=outputs_expected,
        outputs_valid=outputs_valid,
    )


def promote_report(
    report_dir: os.PathLike[str] | str,
    *,
    index_dir: os.PathLike[str] | str,
    kind: str,
    expected_version: str = "",
) -> ReportValidation:
    """Validate, then atomically update only the corresponding latest pointer."""

    if kind == "audit":
        validation = validate_audit_bundle(report_dir)
    elif kind == "release":
        if not expected_version:
            raise ValueError("expected_version is required for release promotion")
        validation = validate_release_bundle(
            report_dir,
            expected_version=expected_version,
        )
    else:
        raise ValueError("kind must be 'audit' or 'release'")

    index_root = Path(index_dir).resolve()
    payload = {
        "kind": kind,
        "report_dir": validation.report_dir,
        "run_id": validation.run_id,
        "app_version": validation.app_version,
        "promoted_at": _now_iso(),
        "validation": validation.to_dict(),
    }
    if validation.eligible:
        _atomic_json(index_root / f"latest_{kind}.json", payload)
    else:
        run_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", validation.run_id or "unknown")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        _atomic_json(
            index_root / ".report-index" / "rejected" / f"{run_label}_{stamp}.json",
            payload,
        )
    return validation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="Report bundle directory")
    parser.add_argument("--kind", choices=("audit", "release"), required=True)
    parser.add_argument("--index-dir", default="docs/reports")
    parser.add_argument("--expected-version", default="")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    validation = promote_report(
        args.report,
        index_dir=args.index_dir,
        kind=args.kind,
        expected_version=args.expected_version,
    )
    print(json.dumps(validation.to_dict(), indent=2, ensure_ascii=False))
    return 0 if validation.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
