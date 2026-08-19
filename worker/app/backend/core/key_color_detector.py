"""Auto-detect the dominant background color from a video.

Used to set the chroma-key `key_color` automatically instead of
hardcoding #00FF00 (which fails when the actual bg is teal, blue,
or any non-pure-green color).

Three strategies are available, in order of increasing precision:
  1. `edges`  — sample the 4 outer borders of a single frame; works
     well for studio shots where the product is centered and the bg
     fills the edges.
  2. `dominant` — round every pixel to a 16-step bucket and pick the
     most common color. Faster but noisier (product can dominate if
     it covers >50% of the frame).
  3. `corner` — sample 4 fixed corner patches. Fastest, but only
     works for studio shots with uniform backdrop.

The default `strategy="auto"` runs `edges` first; if the edge mode
is unanimous (>=80% same color), use that. Otherwise falls back to
`dominant`.

Output is normalized to `#RRGGBB` (uppercase, with the leading `#`).
"""

from __future__ import annotations

import io
import os
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# Lazy imports so this module can be imported without numpy/PIL if
# the user only uses the `ffprobe-based` strategies in the future.
try:
    from PIL import Image  # type: ignore
    _PIL_OK = True
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    _PIL_OK = False


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyColorResult:
    """Result of an auto-detect run.

    Attributes:
        color: `#RRGGBB` (uppercase, 7 chars).
        strategy: which detection strategy produced the result.
        coverage_pct: how much of the analyzed region was the same color
            (0..100). Useful for the caller to decide whether to trust
            the result or fall back to a hardcoded color.
        sample_pixels: number of pixels actually analyzed.
        frame_index: which video frame was sampled (for reproducibility).
        second: timestamp of the sampled frame in seconds (for logs).
    """
    color: str
    strategy: str
    coverage_pct: float
    sample_pixels: int
    frame_index: int
    second: float

    def is_reliable(self, min_coverage_pct: float = 40.0) -> bool:
        """Whether the result is likely a real uniform background.

        Default threshold is 40% — appropriate for typical product shots
        where the bg fills the backdrop behind a centered subject.
        Lower the threshold for videos where the product dominates
        most of the frame.
        """
        return self.coverage_pct >= min_coverage_pct


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_key_color(
    video_path: str | os.PathLike,
    *,
    strategy: str = "auto",
    frame_index: int = 0,
    edge_band: int = 40,
    round_to: int = 16,
    min_coverage_pct: float = 40.0,
    fallback_color: str = "#00FF00",
) -> KeyColorResult:
    """Detect the dominant background color in `video_path`.

    Args:
        video_path: path to a readable video file.
        strategy: `"auto"`, `"edges"`, `"dominant"`, or `"corner"`.
        frame_index: which frame to sample (default: first frame).
        edge_band: thickness in pixels of the border band (for `edges`).
        round_to: bucket size for color quantization (smaller = more
            precise but more noise; default 16 works well for video).
        min_coverage_pct: see `KeyColorResult.is_reliable`.
        fallback_color: if detection fails or coverage is too low,
            return this color.

    Returns:
        `KeyColorResult` with the detected color and diagnostics.
    """
    if not _PIL_OK:
        return KeyColorResult(
            color=fallback_color, strategy="fallback_pil_missing",
            coverage_pct=0.0, sample_pixels=0, frame_index=frame_index,
            second=0.0,
        )
    try:
        frame_rgb, second = _extract_frame_pil(video_path, frame_index)
    except Exception:
        return KeyColorResult(
            color=fallback_color, strategy="fallback_extract_failed",
            coverage_pct=0.0, sample_pixels=0, frame_index=frame_index,
            second=0.0,
        )

    if strategy == "auto":
        # Try edges first; if confident, return. Otherwise dominant.
        res = _analyze_edges(frame_rgb, edge_band, round_to, frame_index, second)
        if res.is_reliable(min_coverage_pct):
            return res
        res = _analyze_dominant(frame_rgb, round_to, frame_index, second)
        if res.is_reliable(min_coverage_pct):
            return res
        # Neither confident enough — return dominant but caller can check
        return res
    elif strategy == "edges":
        return _analyze_edges(frame_rgb, edge_band, round_to, frame_index, second)
    elif strategy == "dominant":
        return _analyze_dominant(frame_rgb, round_to, frame_index, second)
    elif strategy == "corner":
        return _analyze_corners(frame_rgb, edge_band, round_to, frame_index, second)
    else:
        raise ValueError(f"Unknown strategy: {strategy!r}")


def detect_key_color_batch(
    video_paths: Iterable[str | os.PathLike],
    **kwargs,
) -> List[KeyColorResult]:
    """Detect the bg color for each video; useful for batch runs."""
    return [detect_key_color(p, **kwargs) for p in video_paths]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_frame_pil(
    video_path: str | os.PathLike,
    frame_index: int,
) -> Tuple["Image.Image", float]:
    """Use ffmpeg to pull a single RGB frame as a PIL Image.

    Returns `(image, second)`. The image is the FIRST decoded frame
    at the requested `frame_index`.
    """
    # First, get fps + duration so we can compute the time
    info = _ffprobe_info(video_path)
    fps = info.get("fps", 30.0) or 30.0
    duration = info.get("duration", 0.0) or 0.0
    second = (frame_index / fps) if fps > 0 else 0.0
    if duration > 0 and second > duration:
        second = max(0.0, duration - 0.05)

    # Use ffmpeg to dump a single raw RGB24 frame
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{second:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-300:]}")
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB"), second


def _ffprobe_info(video_path: str | os.PathLike) -> dict:
    """Return {fps, duration} via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,duration:format=duration",
        "-of", "json",
        str(video_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=15)
    except Exception:
        return {}
    import json
    try:
        data = json.loads(proc.stdout or b"{}")
    except Exception:
        return {}
    out = {"fps": 0.0, "duration": 0.0}
    streams = data.get("streams") or []
    if streams:
        s = streams[0]
        r = s.get("r_frame_rate", "0/1")
        try:
            n, d = r.split("/")
            out["fps"] = float(n) / float(d) if float(d) else 30.0
        except Exception:
            out["fps"] = 30.0
        if "duration" in s:
            try:
                out["duration"] = float(s["duration"])
            except Exception:
                pass
    fmt = data.get("format") or {}
    if "duration" in fmt and not out["duration"]:
        try:
            out["duration"] = float(fmt["duration"])
        except Exception:
            pass
    return out


def _quantize(rgb: Tuple[int, int, int], step: int) -> Tuple[int, int, int]:
    return tuple((c // step) * step for c in rgb)


def _analyze_dominant(
    img: "Image.Image",
    round_to: int,
    frame_index: int,
    second: float,
) -> KeyColorResult:
    """Most common quantized color across the entire frame."""
    pixels = list(img.getdata())
    counter = Counter(_quantize(p, round_to) for p in pixels)
    color, count = counter.most_common(1)[0]
    return KeyColorResult(
        color=_to_hex(color), strategy="dominant",
        coverage_pct=count * 100.0 / len(pixels),
        sample_pixels=len(pixels), frame_index=frame_index, second=second,
    )


def _analyze_edges(
    img: "Image.Image",
    edge_band: int,
    round_to: int,
    frame_index: int,
    second: float,
) -> KeyColorResult:
    """Most common quantized color in the 4 outer bands (top/bottom/left/right)."""
    W, H = img.size
    pixels: list[tuple[int, int, int]] = []
    band = max(4, min(edge_band, min(W, H) // 4))
    # top
    for y in range(band):
        for x in range(W):
            pixels.append(img.getpixel((x, y)))
    # bottom
    for y in range(H - band, H):
        for x in range(W):
            pixels.append(img.getpixel((x, y)))
    # left middle band
    for y in range(band, H - band):
        for x in range(band):
            pixels.append(img.getpixel((x, y)))
    # right middle band
    for y in range(band, H - band):
        for x in range(W - band, W):
            pixels.append(img.getpixel((x, y)))
    counter = Counter(_quantize(p, round_to) for p in pixels)
    color, count = counter.most_common(1)[0]
    return KeyColorResult(
        color=_to_hex(color), strategy="edges",
        coverage_pct=count * 100.0 / len(pixels),
        sample_pixels=len(pixels), frame_index=frame_index, second=second,
    )


def _analyze_corners(
    img: "Image.Image",
    edge_band: int,
    round_to: int,
    frame_index: int,
    second: float,
) -> KeyColorResult:
    """Most common quantized color in the 4 corner patches."""
    W, H = img.size
    b = max(4, min(edge_band, min(W, H) // 2))
    patches = [
        (0, 0, b, b),               # top-left
        (W - b, 0, W, b),           # top-right
        (0, H - b, b, H),           # bottom-left
        (W - b, H - b, W, H),       # bottom-right
    ]
    pixels: list[tuple[int, int, int]] = []
    for (x0, y0, x1, y1) in patches:
        for y in range(y0, y1):
            for x in range(x0, x1):
                pixels.append(img.getpixel((x, y)))
    counter = Counter(_quantize(p, round_to) for p in pixels)
    color, count = counter.most_common(1)[0]
    return KeyColorResult(
        color=_to_hex(color), strategy="corner",
        coverage_pct=count * 100.0 / len(pixels),
        sample_pixels=len(pixels), frame_index=frame_index, second=second,
    )


def _to_hex(rgb: Tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
