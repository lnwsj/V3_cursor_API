"""Auto DragDrop file discovery and classification.

This module is intentionally UI-free so routing can be tested directly and
reused by every render tab and the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Dict, Iterable, List, Optional

# FIX (B-13, 2026-07-31): use the shared NO_WINDOW_FLAGS constant from
# core.ffmpeg_runner instead of duplicating the CREATE_NO_WINDOW hex.
from .ffmpeg_runner import NO_WINDOW_FLAGS


BACKGROUND_VIDEO_MAX_SECONDS = 13.0
AUTO_CONTEXT_MIXED = "mixed"
AUTO_CONTEXT_SOURCE_ONLY = "source_only"

PRODUCT_HINT_TOKENS = frozenset({
    "product", "source", "foreground", "fg", "greenscreen",
})
BACKGROUND_HINT_TOKENS = frozenset({"background", "bg", "backdrop"})

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".heic", ".heif",
}
AUDIO_EXTS = {".wav", ".wave", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}


@dataclass
class AutoDragDropDetail:
    path: str
    kind: str
    target: str
    reason: str
    duration_seconds: Optional[float] = None


@dataclass
class AutoDragDropResult:
    product: List[str] = field(default_factory=list)
    background: List[str] = field(default_factory=list)
    audio: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)
    details: List[AutoDragDropDetail] = field(default_factory=list)
    source: List[str] = field(default_factory=list)

    @property
    def scanned_count(self) -> int:
        return len(self.details)

    @property
    def assigned_count(self) -> int:
        return (
            len(self.product)
            + len(self.source)
            + len(self.background)
            + len(self.audio)
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "product": list(self.product),
            "source": list(self.source),
            "background": list(self.background),
            "audio": list(self.audio),
            "unknown": list(self.unknown),
            "scanned_count": self.scanned_count,
            "assigned_count": self.assigned_count,
            "details": [
                {
                    "path": item.path,
                    "kind": item.kind,
                    "target": item.target,
                    "reason": item.reason,
                    "duration_seconds": item.duration_seconds,
                }
                for item in self.details
            ],
        }


DurationProbe = Callable[[str], Optional[float]]


def collect_auto_dragdrop_files(paths: Iterable[str]) -> List[str]:
    """Expand dropped files/folders into a stable, de-duplicated file list."""
    seen = set()
    files: List[str] = []
    for raw in paths:
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        try:
            path = path.resolve()
        except Exception:
            path = Path(os.path.abspath(str(path)))

        candidates: Iterable[Path]
        if path.is_dir():
            candidates = sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: str(p).lower())
        elif path.is_file():
            candidates = [path]
        else:
            continue

        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            files.append(key)
    return files


def _positive_float(value: object) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _positive_ratio(value: object) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    if "/" not in text:
        return _positive_float(text)
    numerator, denominator = text.split("/", 1)
    top = _positive_float(numerator)
    bottom = _positive_float(denominator)
    if top is None or bottom is None:
        return None
    ratio = top / bottom
    return ratio if math.isfinite(ratio) and ratio > 0 else None


def _duration_tag_seconds(value: object) -> Optional[float]:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
    except (TypeError, ValueError):
        return None
    duration = hours * 3600.0 + minutes * 60.0 + seconds
    return duration if math.isfinite(duration) and duration > 0 else None


def _video_stream_duration(stream: object) -> Optional[float]:
    """Read duration only from the selected video stream metadata."""

    if not isinstance(stream, dict):
        return None
    direct = _positive_float(stream.get("duration"))
    if direct is not None:
        return direct

    duration_ts = _positive_float(stream.get("duration_ts"))
    time_base = _positive_ratio(stream.get("time_base"))
    if duration_ts is not None and time_base is not None:
        timestamp_duration = duration_ts * time_base
        if math.isfinite(timestamp_duration) and timestamp_duration > 0:
            return timestamp_duration

    tags = stream.get("tags")
    if isinstance(tags, dict):
        tagged = _duration_tag_seconds(
            tags.get("DURATION") or tags.get("duration")
        )
        if tagged is not None:
            return tagged

    frame_count = _positive_float(
        stream.get("nb_read_frames") or stream.get("nb_frames")
    )
    frame_rate = _positive_ratio(
        stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    )
    if frame_count is not None and frame_rate is not None:
        counted_duration = frame_count / frame_rate
        if math.isfinite(counted_duration) and counted_duration > 0:
            return counted_duration
    return None


def probe_media_duration(path: str, ffprobe_cmd: str = "ffprobe") -> Optional[float]:
    """Return first-video-stream duration; never use format/audio-tail duration."""

    creationflags = NO_WINDOW_FLAGS
    try:
        proc = subprocess.run(
            [
                ffprobe_cmd,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                (
                    "stream=duration,duration_ts,time_base,nb_frames,"
                    "nb_read_frames,avg_frame_rate,r_frame_rate:"
                    "stream_tags=DURATION"
                ),
                "-of", "json",
                path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=creationflags,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams")
        stream = streams[0] if isinstance(streams, list) and streams else None
    except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return _video_stream_duration(stream)


def _path_hint_tokens(path: str) -> set[str]:
    """Return exact semantic tokens from filename and parent path parts."""

    tokens: set[str] = set()
    for part in Path(path).parts:
        stem = Path(part).stem.casefold()
        tokens.update(re.findall(r"[a-z0-9]+", stem))
        compact = re.sub(r"[^a-z0-9]+", "", stem)
        if compact:
            tokens.add(compact)
    return tokens


def _semantic_video_target(path: str) -> Optional[str]:
    tokens = _path_hint_tokens(path)
    product_hint = bool(tokens & PRODUCT_HINT_TOKENS)
    background_hint = bool(tokens & BACKGROUND_HINT_TOKENS)
    if product_hint == background_hint:
        return None
    return "product" if product_hint else "background"


def _is_direct_single_video(raw_paths: List[str]) -> bool:
    if len(raw_paths) != 1:
        return False
    path = Path(raw_paths[0]).expanduser()
    return path.is_file() and path.suffix.lower() in VIDEO_EXTS


def classify_auto_dragdrop(
    paths: Iterable[str],
    *,
    duration_probe: Optional[DurationProbe] = None,
    context: str = AUTO_CONTEXT_MIXED,
) -> AutoDragDropResult:
    """Classify dropped files using explicit UI/CLI routing context.

    Priority for video:
    1. source-only context => Source regardless of duration;
    2. one directly dropped video => Product (or Source in source-only context);
    3. unambiguous filename/parent semantic token;
    4. legacy 13-second threshold for folders/conflicts/no hint.
    Images and audio continue to route by extension.
    """

    if context not in {AUTO_CONTEXT_MIXED, AUTO_CONTEXT_SOURCE_ONLY}:
        raise ValueError(f"unsupported Auto DragDrop context: {context!r}")

    raw_paths = [str(path) for path in paths if path]
    direct_single_video = _is_direct_single_video(raw_paths)
    duration_probe = duration_probe or probe_media_duration
    result = AutoDragDropResult()

    for path in collect_auto_dragdrop_files(raw_paths):
        ext = Path(path).suffix.lower()
        if ext in IMAGE_EXTS:
            result.background.append(path)
            result.details.append(
                AutoDragDropDetail(path, "image", "background", "image_extension")
            )
            continue
        if ext in AUDIO_EXTS:
            result.audio.append(path)
            result.details.append(
                AutoDragDropDetail(path, "audio", "audio", "audio_extension")
            )
            continue
        if ext in VIDEO_EXTS:
            if context == AUTO_CONTEXT_SOURCE_ONLY:
                result.source.append(path)
                result.details.append(
                    AutoDragDropDetail(
                        path, "video", "source", "source_only_video", None
                    )
                )
                continue

            if direct_single_video:
                result.product.append(path)
                result.details.append(
                    AutoDragDropDetail(
                        path, "video", "product", "direct_single_video", None
                    )
                )
                continue

            semantic_target = _semantic_video_target(path)
            if semantic_target == "product":
                result.product.append(path)
                result.details.append(
                    AutoDragDropDetail(
                        path, "video", "product", "semantic_path_product", None
                    )
                )
                continue
            if semantic_target == "background":
                result.background.append(path)
                result.details.append(
                    AutoDragDropDetail(
                        path,
                        "video",
                        "background",
                        "semantic_path_background",
                        None,
                    )
                )
                continue

            duration = duration_probe(path)
            if duration is None:
                result.unknown.append(path)
                result.details.append(
                    AutoDragDropDetail(
                        path, "video", "unknown", "duration_probe_failed", None
                    )
                )
            elif duration < BACKGROUND_VIDEO_MAX_SECONDS:
                result.background.append(path)
                result.details.append(
                    AutoDragDropDetail(
                        path,
                        "video",
                        "background",
                        "video_duration_lt_13s",
                        duration,
                    )
                )
            else:
                result.product.append(path)
                result.details.append(
                    AutoDragDropDetail(
                        path,
                        "video",
                        "product",
                        "video_duration_gte_13s",
                        duration,
                    )
                )
            continue

        result.unknown.append(path)
        result.details.append(
            AutoDragDropDetail(
                path, "unknown", "unknown", "unsupported_extension"
            )
        )
    return result
