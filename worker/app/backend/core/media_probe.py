"""Fail-closed metadata and decode probes for render media streams."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .ffmpeg_runner import NO_WINDOW_FLAGS


_CACHE_LOCK = threading.RLock()
# FIX (B-25, 2026-07-31): use OrderedDict + LRU eviction instead of
# wholesale clear(). Wholesale clear() discards every cached probe result
# at once when the cache fills, which forces every subsequent probe to
# re-spawn ffprobe. LRU keeps warm entries and only evicts the oldest
# cold entries.
from collections import OrderedDict
_STREAM_STATE_CACHE: "OrderedDict[Tuple[object, ...], 'MediaStreamState']" = OrderedDict()
_MAX_CACHE_ENTRIES = 512


class MediaProbeCancelled(RuntimeError):
    """Raised when the caller cancels an in-flight media preflight."""


class MediaStreamState(str, Enum):
    """Truth-bearing result for a metadata + decode stream probe.

    ``ABSENT`` is reserved for a successful metadata query that definitively
    reports no selected stream. Tool failures, timeouts, missing files and
    decode failures are ``ERROR`` so stream-preserving callers never silently
    reinterpret an unavailable probe as genuine absence.
    """

    PRESENT = "present"
    ABSENT = "absent"
    ERROR = "error"


def _cancel_requested(stop_check: Optional[Callable[[], bool]]) -> bool:
    if stop_check is None:
        return False
    try:
        return bool(stop_check())
    except Exception:
        # A broken cancellation callback cannot safely authorize more work.
        return True


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.communicate(timeout=0.5)
    except Exception:
        try:
            process.kill()
            process.communicate(timeout=0.5)
        except Exception:
            pass


def _run_probe(
    cmd: List[str],
    *,
    timeout: float,
    stop_check: Optional[Callable[[], bool]],
) -> subprocess.CompletedProcess:
    """Run a probe and poll cancellation without leaving a child process."""

    if _cancel_requested(stop_check):
        raise MediaProbeCancelled("media preflight cancelled")
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "creationflags": NO_WINDOW_FLAGS,
    }
    if stop_check is None:
        return subprocess.run(cmd, timeout=timeout, **kwargs)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW_FLAGS,
    )
    deadline = time.monotonic() + timeout
    while True:
        if _cancel_requested(stop_check):
            _stop_process(process)
            raise MediaProbeCancelled("media preflight cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(process)
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=int(process.returncode or 0),
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            continue


def clear_media_probe_cache() -> None:
    """Clear definitive stream-probe results (primarily for tests)."""

    with _CACHE_LOCK:
        _STREAM_STATE_CACHE.clear()


def _cache_key(
    path: str,
    selector: str,
    expected_type: str,
    ffprobe_cmd: str,
    ffmpeg_cmd: str,
) -> Tuple[object, ...] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (
        os.path.normcase(os.path.abspath(path)),
        stat.st_size,
        stat.st_mtime_ns,
        selector,
        expected_type,
        ffprobe_cmd,
        ffmpeg_cmd,
    )


def _metadata_stream_state(
    path: str,
    selector: str,
    expected_type: str,
    ffprobe_cmd: str,
    stop_check: Optional[Callable[[], bool]],
) -> MediaStreamState:
    try:
        result = _run_probe(
            [
                ffprobe_cmd,
                "-v",
                "error",
                "-select_streams",
                selector,
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                os.fspath(path),
            ],
            timeout=10,
            stop_check=stop_check,
        )
    except MediaProbeCancelled:
        raise
    except Exception:
        return MediaStreamState.ERROR
    if result.returncode != 0:
        return MediaStreamState.ERROR
    present = expected_type in {
        line.strip().lower()
        for line in (result.stdout or "").splitlines()
        if line.strip()
    }
    return MediaStreamState.PRESENT if present else MediaStreamState.ABSENT


def _decode_stream_state(
    path: str,
    selector: str,
    expected_type: str,
    ffmpeg_cmd: str,
    stop_check: Optional[Callable[[], bool]],
) -> MediaStreamState:
    frame_args = ["-frames:v", "1"] if expected_type == "video" else ["-frames:a", "1"]
    try:
        result = _run_probe(
            [
                ffmpeg_cmd,
                "-v",
                "error",
                "-nostdin",
                "-i",
                os.fspath(path),
                "-map",
                f"0:{selector}",
                *frame_args,
                "-f",
                "null",
                "-",
            ],
            timeout=15,
            stop_check=stop_check,
        )
    except MediaProbeCancelled:
        raise
    except Exception:
        return MediaStreamState.ERROR
    return (
        MediaStreamState.PRESENT
        if result.returncode == 0
        else MediaStreamState.ERROR
    )


def media_stream_state(
    path: str,
    selector: str,
    expected_type: str,
    *,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> MediaStreamState:
    """Return ``PRESENT``, definitive ``ABSENT`` or transient/fatal ``ERROR``.

    PRESENT and ABSENT are safe to cache by normalized path + file stat. ERROR
    is deliberately never cached so a transient tool/decode failure can recover
    on the next probe.
    """

    if _cancel_requested(stop_check):
        raise MediaProbeCancelled("media preflight cancelled")
    if not path or not os.path.isfile(path):
        return MediaStreamState.ERROR
    path = os.fspath(path)
    key = _cache_key(path, selector, expected_type, ffprobe_cmd, ffmpeg_cmd)
    if key is None:
        return MediaStreamState.ERROR
    with _CACHE_LOCK:
        cached = _STREAM_STATE_CACHE.get(key)
        if cached is not None:
            return cached

    metadata_state = _metadata_stream_state(
        path,
        selector,
        expected_type,
        ffprobe_cmd,
        stop_check,
    )
    if metadata_state is MediaStreamState.PRESENT:
        state = _decode_stream_state(
            path,
            selector,
            expected_type,
            ffmpeg_cmd,
            stop_check,
        )
    else:
        state = metadata_state

    if state in (MediaStreamState.PRESENT, MediaStreamState.ABSENT):
        with _CACHE_LOCK:
            # FIX (B-25): LRU eviction. OrderedDict.move_to_end on hit,
            # popitem(last=False) on overflow.
            if key in _STREAM_STATE_CACHE:
                _STREAM_STATE_CACHE.move_to_end(key)
            _STREAM_STATE_CACHE[key] = state
            while len(_STREAM_STATE_CACHE) > _MAX_CACHE_ENTRIES:
                _STREAM_STATE_CACHE.popitem(last=False)
    return state


def has_media_stream(
    path: str,
    selector: str,
    expected_type: str,
    *,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Compatibility bool API: True only for a definitively PRESENT stream."""

    return media_stream_state(
        path,
        selector,
        expected_type,
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
        stop_check=stop_check,
    ) is MediaStreamState.PRESENT


def video_stream_state(
    path: str,
    *,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> MediaStreamState:
    """Return tri-state truth for the first video/still-image stream."""

    return media_stream_state(
        path,
        "v:0",
        "video",
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
        stop_check=stop_check,
    )


def audio_stream_state(
    path: str,
    *,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> MediaStreamState:
    """Return tri-state truth for the first audio stream."""

    return media_stream_state(
        path,
        "a:0",
        "audio",
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
        stop_check=stop_check,
    )

def has_video_stream(
    path: str,
    *,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Return True for a decodable first video or still-image stream."""

    return has_media_stream(
        path,
        "v:0",
        "video",
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
        stop_check=stop_check,
    )


def has_audio_stream(
    path: str,
    *,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Return True for a decodable first audio stream."""

    return has_media_stream(
        path,
        "a:0",
        "audio",
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
        stop_check=stop_check,
    )


def invalid_video_stream_paths(
    paths: Iterable[str],
    *,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> List[str]:
    """Return paths that do not expose a decodable video/image stream."""

    invalid: List[str] = []
    for path in paths:
        if _cancel_requested(stop_check):
            raise MediaProbeCancelled("media preflight cancelled")
        value = os.fspath(path)
        if not has_video_stream(
            value,
            ffprobe_cmd=ffprobe_cmd,
            ffmpeg_cmd=ffmpeg_cmd,
            stop_check=stop_check,
        ):
            invalid.append(value)
    return invalid


def invalid_audio_stream_paths(
    paths: Iterable[str],
    *,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> List[str]:
    """Return paths that do not expose a decodable audio stream."""

    invalid: List[str] = []
    for path in paths:
        if _cancel_requested(stop_check):
            raise MediaProbeCancelled("media preflight cancelled")
        value = os.fspath(path)
        if not has_audio_stream(
            value,
            ffprobe_cmd=ffprobe_cmd,
            ffmpeg_cmd=ffmpeg_cmd,
            stop_check=stop_check,
        ):
            invalid.append(value)
    return invalid


# === NVDEC input decoder helpers (added 2026-08-18) ===
# When V3_NVDEC=1, insert ``-c:v <codec>_cuvid`` BEFORE each ``-i <video>``
# in the inputs list. Image inputs (PNG/JPG) and audio-only inputs are skipped
# via the codec probe. Pattern ported from V3 Cursor WebApp/core/media_probe.py.

_INPUT_CODEC_CACHE: Dict[str, str] = {}
_INPUT_CODEC_LOCK = threading.RLock()
_INPUT_CODEC_CACHE_MAX = 1024


def probe_video_codec(ffprobe_cmd: str, path: str) -> str:
    """Return video codec name (hevc/h264/...) or '' on failure.

    Cached by (path, mtime_ns, size) — file changes invalidate stale entries.
    Strips trailing commas from CSV output.
    """
    if not path or not os.path.isfile(path):
        return ""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    cache_key = f"{os.path.normcase(os.path.abspath(path))}@{st.st_mtime_ns}:{st.st_size}"
    with _INPUT_CODEC_LOCK:
        if cache_key in _INPUT_CODEC_CACHE:
            return _INPUT_CODEC_CACHE[cache_key]
    codec = ""
    try:
        proc = subprocess.run(
            [ffprobe_cmd, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, creationflags=NO_WINDOW_FLAGS,
        )
        codec = (proc.stdout or "").strip().split(",")[0].strip()
    except Exception:
        codec = ""
    with _INPUT_CODEC_LOCK:
        if len(_INPUT_CODEC_CACHE) > _INPUT_CODEC_CACHE_MAX:
            _INPUT_CODEC_CACHE.clear()
        _INPUT_CODEC_CACHE[cache_key] = codec
    return codec


def input_decoder_args(ffprobe_cmd: str, inputs: List[str]) -> List[str]:
    """Insert ``-c:v <codec>_cuvid`` BEFORE each ``-i <video>`` in inputs.

    When ``V3_NVDEC != "1"`` returns ``inputs`` unchanged (no-op). Image
    inputs (PNG/JPG) are skipped (image2 demuxer handles them). Audio-only
    inputs (no video stream) are also skipped via the codec probe.
    """
    if os.getenv("V3_NVDEC", "").strip() != "1":
        return inputs
    out: List[str] = []
    i = 0
    while i < len(inputs):
        a = inputs[i]
        if a == "-i" and i + 1 < len(inputs):
            path = inputs[i + 1]
            codec = probe_video_codec(ffprobe_cmd, path)
            if codec == "hevc":
                out += ["-c:v", "hevc_cuvid"]
            elif codec == "h264":
                out += ["-c:v", "h264_cuvid"]
            # else: leave alone (image, audio, unsupported codec)
            out.append(a)         # -i
            out.append(path)      # path
            i += 2
        else:
            out.append(a)
            i += 1
    return out
