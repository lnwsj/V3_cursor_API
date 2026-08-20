"""
Batch Mode + Ping-Pong Matching for Green render.

Standalone, framework-agnostic port from green.sj88ai.com:
  - services/ping_pong.py    → create_ping_pong_segments
  - services/random_matcher.py → RandomMatcher / SequentialMatcher / ShuffleOnceMatcher
  - services/batch_engine.py → _ShufflePool / _CyclePool / no-repeat policy

ความสามารถ:
  1. split product video เป�น TimeRange segments
  2. สร้าง ping-pong sequence (F-B-F-B-F) เพื่อ seamless loop
  3. match segments ↔ backgrounds ↔ audios ด้วย 3 modes
"""


import concurrent.futures
import functools
import math
import os
import random
import re
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .ffmpeg_runner import FfmpegRunner, FfmpegProgress
from .audio_master import probe_audio_duration
from .green_render import (
    GreenSettings,
    _duration_matches_expected,
    _duration_tolerance,
    _probe_duration,
    render_green,
)
from .gpu_detector import effective_video_encoder, resolve_encoder_alias
from .media_probe import has_video_stream
from .encoder_recovery import should_retry_with_cpu


VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
DURATION_SPLIT_BOUNDARY_ULPS = 8


# ==================== TimeRange + PingPong ====================

@dataclass(frozen=True)
class TimeRange:
    """A fixed source segment."""
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class PingPongSegment:
    """A segment in a ping-pong sequence."""
    time_range: TimeRange
    direction: int  # 1 = forward, -1 = backward
    segment_index: int

    @property
    def duration(self) -> float:
        return self.time_range.duration

    @property
    def is_forward(self) -> bool:
        return self.direction == 1

    def __repr__(self) -> str:
        return f"PP({self.segment_index}, {'F' if self.is_forward else 'B'}, {self.duration:.1f}s)"


def _is_floating_segment_boundary(
    duration: float,
    segment_duration: float,
    segment_count: int,
) -> bool:
    """True only for machine-rounding noise around an exact segment boundary."""

    if segment_count <= 0:
        return False
    boundary = segment_count * segment_duration
    tolerance = DURATION_SPLIT_BOUNDARY_ULPS * max(
        math.ulp(duration),
        math.ulp(boundary),
        math.ulp(segment_duration) * segment_count,
    )
    return abs(duration - boundary) <= tolerance


def build_forward_segment_ranges(
    duration: float,
    segment_duration: float,
) -> List[TimeRange]:
    """Build strict ceil(duration / segment_duration) forward boundaries.

    Every positive real tail is retained, including tails at or below 50 ms.
    A quotient that differs from an integer by only floating-point ULP noise is
    treated as the exact boundary so values such as 0.1 + 0.2 do not create a
    phantom extra segment.
    """

    if not math.isfinite(segment_duration) or segment_duration <= 0:
        raise ValueError("segment_duration must be a finite value > 0")
    if not math.isfinite(duration) or duration <= 0:
        return []

    quotient = duration / segment_duration
    nearest_count = int(round(quotient))
    if _is_floating_segment_boundary(
        duration,
        segment_duration,
        nearest_count,
    ):
        segment_count = nearest_count
    else:
        segment_count = math.ceil(quotient)

    ranges: List[TimeRange] = []
    for index in range(segment_count):
        start = index * segment_duration
        end = min((index + 1) * segment_duration, duration)
        ranges.append(TimeRange(start=start, end=end))
    return ranges

def create_ping_pong_segments(segment_duration: float, target_duration: float) -> List[PingPongSegment]:
    """
    Create ping-pong segments that fill target_duration.

    Algorithm (port ตรงจาก green.sj88ai.com/services/ping_pong.py):
      - สลับ F (forward) / B (backward) เพื่อ seamless loop
      - แต่ละ segment ใช้ block เดิม แต่เล่นย้อนกลับ
      - ตัวอย่าง: dur=10, target=45 → F(10)+B(10)+F(10)+B(10)+F(5) = 45s

    Raises:
        ValueError: ถ้า segment_duration หรือ target_duration <= 0
    """
    if segment_duration <= 0:
        raise ValueError(f"segment_duration must be positive, got {segment_duration}")
    if target_duration <= 0:
        raise ValueError(f"target_duration must be positive, got {target_duration}")

    segments: List[PingPongSegment] = []
    current_time = 0.0
    direction = 1  # เริ่ม forward
    segment_start = 0.0
    segment_index = 0

    while current_time < target_duration:
        remaining = target_duration - current_time

        if direction == 1:  # Forward
            next_boundary = segment_start + segment_duration
            if segment_start + remaining >= next_boundary:
                segments.append(PingPongSegment(
                    time_range=TimeRange(start=segment_start, end=next_boundary),
                    direction=1,
                    segment_index=segment_index,
                ))
                current_time += segment_duration
                segment_start = next_boundary
                direction = -1
            else:
                # partial segment at the tail
                segments.append(PingPongSegment(
                    time_range=TimeRange(start=segment_start, end=segment_start + remaining),
                    direction=1,
                    segment_index=segment_index,
                ))
                current_time = target_duration
        else:  # Backward
            next_boundary = segment_start
            if segment_start - remaining <= next_boundary - segment_duration:
                segments.append(PingPongSegment(
                    time_range=TimeRange(
                        start=next_boundary - segment_duration,
                        end=next_boundary,
                    ),
                    direction=-1,
                    segment_index=segment_index,
                ))
                current_time += segment_duration
                segment_start = next_boundary - segment_duration
                direction = 1
            else:
                # partial segment at the tail (backward direction)
                segments.append(PingPongSegment(
                    time_range=TimeRange(
                        start=next_boundary - remaining,
                        end=next_boundary,
                    ),
                    direction=-1,
                    segment_index=segment_index,
                ))
                current_time = target_duration

        segment_index += 1

    return segments


def get_ping_pong_pattern(segments: List[PingPongSegment]) -> str:
    """คืน pattern เช่น 'F-B-F-B-F'"""
    return "-".join("F" if s.is_forward else "B" for s in segments)


def calculate_ping_pong_metrics(segment_duration: float, target_duration: float) -> dict:
    """สรุปสถิติของ ping-pong sequence"""
    segs = create_ping_pong_segments(segment_duration, target_duration)
    f_count = sum(1 for s in segs if s.is_forward)
    b_count = len(segs) - f_count
    return {
        "segment_duration": segment_duration,
        "target_duration": target_duration,
        "total_segments": len(segs),
        "forward_segments": f_count,
        "backward_segments": b_count,
        "pattern": get_ping_pong_pattern(segs),
        "actual_duration": sum(s.duration for s in segs),
    }


# ==================== Pools ====================

class _ShufflePool:
    """No-repeat random pool; reshuffle เมื่อใช้หมด (TENER-style)

    Pattern port จาก green.sj88ai.com/services/batch_engine.py::_ShufflePool
    """

    def __init__(self, items: List, rng: random.Random):
        if not items:
            raise ValueError("Pool cannot be empty")
        self._items = list(items)
        self._rng = rng
        self._index = len(items)  # trigger first shuffle
        self._has_last = False
        self._last_item = None

    def next(self):
        if self._index >= len(self._items):
            self._rng.shuffle(self._items)
            # A shuffled cycle is internally no-repeat when asset paths are
            # unique, but its first item can still equal the previous cycle's
            # last item. Repair that boundary deterministically without
            # consuming more RNG state or retrying indefinitely.
            if (
                len(self._items) > 1
                and self._has_last
                and self._items[0] == self._last_item
            ):
                replacement_index = next(
                    (
                        index
                        for index in range(1, len(self._items))
                        if self._items[index] != self._last_item
                    ),
                    None,
                )
                if replacement_index is not None:
                    self._items[0], self._items[replacement_index] = (
                        self._items[replacement_index],
                        self._items[0],
                    )
            self._index = 0
        item = self._items[self._index]
        self._index += 1
        self._last_item = item
        self._has_last = True
        return item


class _ShuffleOncePool:
    """Shuffle exactly once, then repeat that fixed order forever."""

    def __init__(self, items: List, rng: random.Random):
        if not items:
            raise ValueError("Pool cannot be empty")
        self._items = list(items)
        rng.shuffle(self._items)
        self._index = 0

    def next(self):
        item = self._items[self._index]
        self._index = (self._index + 1) % len(self._items)
        return item


class _CyclePool:
    """Ordered cycle pool — deterministic sequential matching"""

    def __init__(self, items: List):
        if not items:
            raise ValueError("Pool cannot be empty")
        self._items = list(items)
        self._index = 0

    def next(self):
        item = self._items[self._index]
        self._index = (self._index + 1) % len(self._items)
        return item


# ==================== Match Strategies ====================

class MatchMode(str, Enum):
    RANDOM = "random"             # random.choice (อนุญาตซ้ำ)
    SEQUENTIAL = "sequential"     # cycle ตามลำดับ
    SHUFFLE_ONCE = "shuffle_once" # shuffle 1 ครั้งแล้ว cycle
    NO_REPEAT = "no_repeat"       # no-repeat random (TENER-style) — สำคัญที่สุด


@dataclass
class BatchMatch:
    """Match ของ product_index + segment + cover + background + audio"""
    product_index: int
    product_path: str
    segment: PingPongSegment
    cover_path: Optional[str]
    background_path: Optional[str]
    audio_path: Optional[str]
    output_index: int

    def to_dict(self) -> dict:
        return {
            "output_index": self.output_index,
            "product_index": self.product_index,
            "product": self.product_path,
            "segment_index": self.segment.segment_index,
            "direction": "F" if self.segment.is_forward else "B",
            "start": self.segment.time_range.start,
            "end": self.segment.time_range.end,
            "duration": self.segment.duration,
            "cover": self.cover_path,
            "background": self.background_path,
            "audio": self.audio_path,
        }


# ==================== Batch Settings ====================

@dataclass
class BatchSettings:
    """ตั้งค่า batch mode (port fields จาก green.sj88ai.com core/models.py Settings)"""
    # matchers
    cover_mode: MatchMode = MatchMode.NO_REPEAT
    background_mode: MatchMode = MatchMode.NO_REPEAT
    audio_mode: MatchMode = MatchMode.NO_REPEAT

    # ping-pong
    product_ping_pong: bool = True
    background_ping_pong: bool = True  # ถ้า True และ backgrounds เป็นวิดีโอ → ping-pong backgrounds ด้วย

    # outputs
    segment_duration: float = 5.0   # ความยาว segment สำหรับ ping-pong (วินาที)
    num_outputs: int = 5            # จำนวน outputs ที่ต้องการ
    split_by_duration: bool = False  # ถ้า True: num_outputs มาจากความยาวคลิป / segment_duration

    # audio
    use_uploaded_audio: bool = False
    use_product_audio: bool = True
    uploaded_audio_controls_duration: bool = False

    # misc
    seed: Optional[int] = None      # สำหรับ reproducible


# ==================== Build Matches ====================

def snapshot_product_durations(
    products: List[str],
    ffprobe_cmd: str = "ffprobe",
    stop_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, float]:
    """Probe each distinct product once for reuse across planning stages.

    The returned mapping is an immutable-in-practice planning snapshot: pass it
    to validation, count estimation, match building, and rendering so those
    stages all use the same source-duration truth without launching ffprobe
    again. Duplicate paths in ``products`` are probed only once.
    """

    snapshot: Dict[str, float] = {}
    for product in products:
        key = os.fspath(product)
        if key not in snapshot:
            snapshot[key] = _probe_duration(
                key,
                ffprobe_cmd,
                stop_check=stop_check,
            )
    return snapshot


def _snapshot_duration(
    product: str,
    duration_snapshot: Mapping[str, float],
) -> float:
    """Read one duration from a complete snapshot without probing fallback."""

    key = os.fspath(product)
    if key not in duration_snapshot:
        raise ValueError(f"duration snapshot missing product: {key}")
    try:
        duration = float(duration_snapshot[key])
    except (TypeError, ValueError):
        return 0.0
    return duration if math.isfinite(duration) and duration > 0 else 0.0


def estimate_duration_split_count(
    products: List[str],
    segment_duration: float,
    *,
    duration_snapshot: Optional[Mapping[str, float]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    ffprobe_cmd: str = "ffprobe",
) -> int:
    """Count canonical forward ranges from one product-duration snapshot."""
    if not math.isfinite(segment_duration) or segment_duration <= 0:
        raise ValueError("segment_duration must be a finite value > 0")
    snapshot = (
        snapshot_product_durations(
            products,
            ffprobe_cmd=ffprobe_cmd,
            stop_check=stop_check,
        )
        if duration_snapshot is None
        else duration_snapshot
    )
    total = 0
    for product in products:
        duration = _snapshot_duration(product, snapshot)
        total += len(build_forward_segment_ranges(duration, segment_duration))
    return total


def unrenderable_products(
    products: List[str],
    ffprobe_cmd: str = "ffprobe",
    *,
    duration_snapshot: Optional[Mapping[str, float]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> List[str]:
    """Return products that do not expose a positive media duration."""

    snapshot = (
        snapshot_product_durations(
            products,
            ffprobe_cmd=ffprobe_cmd,
            stop_check=stop_check,
        )
        if duration_snapshot is None
        else duration_snapshot
    )
    return [
        product
        for product in products
        if _snapshot_duration(product, snapshot) <= 0
    ]


def build_asset_pickers(
    backgrounds: List[str],
    audios: List[str],
    settings: BatchSettings,
):
    """Build the seeded Background/Audio pickers used by runtime and dry-run."""

    if not backgrounds:
        raise ValueError("backgrounds list is empty")
    rng = random.Random(settings.seed)

    def make_pool(items, mode):
        if not items:
            return lambda: None
        if mode == MatchMode.RANDOM:
            return lambda: rng.choice(items)
        if mode == MatchMode.SEQUENTIAL:
            return _CyclePool(items).next
        if mode == MatchMode.SHUFFLE_ONCE:
            return _ShuffleOncePool(items, rng).next
        if mode == MatchMode.NO_REPEAT:
            return _ShufflePool(items, rng).next
        return _CyclePool(items).next

    pick_bg = make_pool(backgrounds, settings.background_mode)
    pick_audio = make_pool(audios, settings.audio_mode) if audios else lambda: None
    return rng, pick_bg, pick_audio

def build_batch_matches(
    products: List[str],
    backgrounds: List[str],
    audios: List[str],
    settings: BatchSettings,
    covers: Optional[List[str]] = None,
    *,
    duration_snapshot: Optional[Mapping[str, float]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    ffprobe_cmd: str = "ffprobe",
) -> List[BatchMatch]:
    """
    สร้าง list ของ BatchMatch ตาม settings

    Pipeline:
      1. สำหรับแต่ละ product → สร้าง ping-pong sequence (ถ้า product_ping_pong)
      2. ทำซ้ำจนครบ num_outputs
      3. match cover/background/audio ตาม mode

    Returns:
        List[BatchMatch] — เรียงตาม output_index
    """
    if not products:
        raise ValueError("products list is empty")
    if not backgrounds:
        raise ValueError("backgrounds list is empty")
    if settings.num_outputs <= 0 and not settings.split_by_duration:
        raise ValueError("num_outputs must be > 0")
    if settings.segment_duration <= 0:
        raise ValueError("segment_duration must be > 0")

    matches: List[BatchMatch] = []
    rng, pick_bg, pick_audio = build_asset_pickers(backgrounds, audios, settings)
    first_cover = covers[0] if covers else None
    pick_cover = lambda: first_cover

    if settings.split_by_duration:
        snapshot = (
            snapshot_product_durations(
                products,
                ffprobe_cmd=ffprobe_cmd,
                stop_check=stop_check,
            )
            if duration_snapshot is None
            else duration_snapshot
        )
        output_index = 0
        seg_dur = settings.segment_duration
        # FIX (2026-07-02): use enumerate() instead of products.index()
        # to avoid O(n^2) cost when iterating a long product list.
        for product_index, product_path in enumerate(products):
            product_duration = _snapshot_duration(product_path, snapshot)
            ranges = build_forward_segment_ranges(product_duration, seg_dur)
            for segment_index, time_range in enumerate(ranges):
                cover = pick_cover()
                bg = pick_bg()
                audio = pick_audio() if (settings.use_uploaded_audio and audios) else None
                output_index += 1
                matches.append(BatchMatch(
                    product_index=product_index,
                    product_path=product_path,
                    segment=PingPongSegment(
                        time_range=time_range,
                        direction=1,
                        segment_index=segment_index,
                    ),
                    cover_path=cover,
                    background_path=bg,
                    audio_path=audio,
                    output_index=output_index,
                ))
        if not matches:
            raise ValueError("no duration-based segments could be created")
        return matches

    # Build the ping-pong template once and reuse it for every product run.
    seg_dur = settings.segment_duration
    # target = seg_dur * 2 (1 forward + 1 backward) — the base template.
    # Every product reuses the same template (no per-product randomization).
    base_template = create_ping_pong_segments(seg_dur, seg_dur * 2)

    output_index = 0
    # FIX (2026-07-02): pre-build path -> index map (O(n)) instead of
    # calling products.index(product_path) inside the render loop (was O(n^2)).
    product_index_map = {p: i for i, p in enumerate(products)}
    product_iter = _CyclePool(products).next  # cycle through products
    # Use a shuffle pool for the product too — avoid reusing the same
    # product in two consecutive outputs.
    product_pool = _ShufflePool(products, rng).next

    while output_index < settings.num_outputs:
        product_path = product_pool()
        product_index = product_index_map.get(product_path, 0)

        # Pick the ping-pong segment in cyclic order.
        pp_seg = base_template[output_index % len(base_template)]

        cover = pick_cover()
        bg = pick_bg()
        audio = pick_audio() if (settings.use_uploaded_audio and audios) else None

        output_index += 1
        matches.append(BatchMatch(
            product_index=product_index,
            product_path=product_path,
            segment=pp_seg,
            cover_path=cover,
            background_path=bg,
            audio_path=audio,
            output_index=output_index,
        ))

    return matches


# ==================== Render Batch ====================

@dataclass
class BatchResult:
    output_index: int
    output_path: str
    success: bool
    error: str = ""
    duration_sec: float = 0.0
    cancelled: bool = False
    # FIX (B-26, 2026-07-31): carry BatchMatch info so error messages can
    # identify which product / segment / cover / background failed.
    match: Optional["BatchMatch"] = None
    # FIX (V1.0.2.14, G3): marker when the output was already on disk and
    # validated -- render_batch did not invoke ffmpeg. This is a "skipped"
    # success, not a re-render. The downstream summary should report
    # validated_resumed=1 for these.
    skipped: bool = False


class _BatchCancelled(RuntimeError):
    """Internal control flow for a cancelled segment extraction."""



def render_batch(
    products: List[str],
    backgrounds: List[str],
    audios: List[str],
    out_dir: str,
    base_settings: GreenSettings,
    batch_settings: BatchSettings,
    covers: Optional[List[str]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, FfmpegProgress], None]] = None,  # (current, total, prog)
    on_match: Optional[Callable[[BatchMatch], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
    tc_label: str = "",  # V1.0.0.4: per-TC watchdog factor override
    chroma_max_parallel: int = 1,  # V1.0.0.7: divide CPU budget across concurrent chroma
    duration_snapshot: Optional[Mapping[str, float]] = None,
    run_stamp: str = "",
    pre_validated_outputs: Optional[set] = None,
) -> List[BatchResult]:
    """
    Render batch ตาม BatchSettings — สร้าง N outputs พร้อม ping-pong matching

    V1.0.0.6 note on the `products` parameter name:
        The parameter is named `products` for historical reasons (TC03
        uses original green-screen videos here), but the value is just
        "input clips to be split + chroma-keyed". TC04 calls this with
        reframe outputs from stage 1 (render_reframe_plan), not with
        the original user products. If you are reading the code and
        see `render_batch(products=reframe_sources, ...)`, the
        `products` here means "reframe clips", not "user input products".

    Args:
        products: list ของ clips ที่จะ split + chroma-key (อาจเป็น original
                 green-screen videos สำหรับ TC03, หรือ reframe outputs
                 จาก stage 1 สำหรับ TC04) — ต้องมีอย่างน้อย 1
        backgrounds: list ของ background — ต้องมีอย่างน้อย 1
        audios: list ของ audio (optional)
        out_dir: โฟลเดอร์ output
        base_settings: GreenSettings (chroma, encoder, resolution, ...)
        batch_settings: BatchSettings (matchers, ping-pong, num_outputs)
        on_log: callback log message
        on_progress: callback (current_output, total_outputs, FfmpegProgress)
        on_match: callback เมื่อสร้าง BatchMatch (ก่อน render)
        stop_check: callback user cancel
        ffmpeg_cmd / ffprobe_cmd: paths
        duration_snapshot: one-shot source durations shared by planning stages
        run_stamp: optional per-run filename token; generated with microseconds when empty

    Returns:
        List[BatchResult] เรียงตาม output_index
    """
    log = on_log or (lambda m: None)
    raw_run_stamp = run_stamp or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    effective_run_stamp = re.sub(r"[^0-9A-Za-z_-]+", "_", raw_run_stamp).strip("_")
    if not effective_run_stamp:
        effective_run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    log(f"[batch] products={len(products)} backgrounds={len(backgrounds)} audios={len(audios)} covers={len(covers or [])}")
    log(f"[batch] run_stamp={effective_run_stamp}")
    log(f"[batch] num_outputs={batch_settings.num_outputs} seg_dur={batch_settings.segment_duration}s")
    log(f"[batch] split_by_duration={batch_settings.split_by_duration}")
    log(
        "[batch] uploaded_audio_controls_duration="
        f"{batch_settings.uploaded_audio_controls_duration}"
    )
    log(f"[batch] ping_pong: product={batch_settings.product_ping_pong} bg={batch_settings.background_ping_pong}")
    log(f"[batch] modes: cover={batch_settings.cover_mode.value} bg={batch_settings.background_mode.value} audio={batch_settings.audio_mode.value}")

    matches = build_batch_matches(
        products,
        backgrounds,
        audios,
        batch_settings,
        covers=covers,
        duration_snapshot=duration_snapshot,
        stop_check=stop_check,
        ffprobe_cmd=ffprobe_cmd,
    )
    log(f"[batch] generated {len(matches)} matches")

    os.makedirs(out_dir, exist_ok=True)

    results: List[BatchResult] = []
    total = len(matches)
    audio_duration_cache: Dict[str, float] = {}

    # v3.PARALLEL (2026-08-18): actual parallel chroma execution via ThreadPoolExecutor.
    # chroma_max_parallel=1 → sequential (original behavior).
    # chroma_max_parallel>=2 → N ffmpegs run concurrently with thread budget = cpu_budget/N.
    if chroma_max_parallel <= 1:
        for i, match in enumerate(matches, 1):
            if stop_check and stop_check():
                log(f"[batch] ⏹️ cancelled at output {i}/{total}")
                break

            if on_match:
                try:
                    on_match(match)
                except Exception:
                    pass

            log(f"[batch] [{i}/{total}] {os.path.basename(match.product_path)} "
                f"segment {match.segment.segment_index} ({'F' if match.segment.is_forward else 'B'}, "
                f"{match.segment.duration:.1f}s)")

            out_name = (
                f"batch_{effective_run_stamp}_{match.output_index:03d}_"
                f"{os.path.splitext(os.path.basename(match.product_path))[0]}.mp4"
            )
            out_path = os.path.join(out_dir, out_name)

            # FIX (V1.0.2.14, G3): skip-existing-output. If the caller
            # confirmed this output is already valid on disk, return a
            # "skipped" success without invoking ffmpeg. Saves resume time
            # when only a few outputs are missing.
            if pre_validated_outputs and os.fspath(out_path) in pre_validated_outputs:
                log(f"[batch] [{i}/{total}] skipped (pre-validated): {os.path.basename(out_path)}")
                if on_match:
                    try: on_match(match)
                    except Exception: pass
                results.append(BatchResult(
                    output_index=match.output_index,
                    output_path=out_path,
                    success=True,
                    error="",
                    duration_sec=0.0,
                    match=match,
                    skipped=True,
                ))
                continue

            # Adjust GreenSettings for the current segment.
            seg_settings = GreenSettings(
                width=base_settings.width,
                height=base_settings.height,
                fps=base_settings.fps,
                bitrate=base_settings.bitrate,
                encoder_alias=base_settings.encoder_alias,
                key_color=base_settings.key_color,
                similarity=base_settings.similarity,
                blend=base_settings.blend,
                despill=base_settings.despill,
                despill_screen=base_settings.despill_screen,
                cover_enabled=base_settings.cover_enabled,
                cover_duration=base_settings.cover_duration,
                cover_scale=base_settings.cover_scale,
                audio_source=base_settings.audio_source,
                preset=base_settings.preset,
            )

            # Extract a sub-clip that matches the segment direction.
            try:
                extract_encoder = base_settings.encoder_alias
                log(
                    f"[batch] extract segment direction={'forward' if match.segment.is_forward else 'backward'} "
                    f"encoder_alias={extract_encoder}"
                )
                sub_clip = _extract_segment(
                    match.product_path,
                    match.segment,
                    out_dir,
                    ffmpeg_cmd=ffmpeg_cmd,
                    ffprobe_cmd=ffprobe_cmd,
                    encoder_alias=base_settings.encoder_alias,
                    stop_check=stop_check,
                    on_log=log,
                    tc_label=tc_label,
                    run_stamp=effective_run_stamp,
                )
            except _BatchCancelled as e:
                log(f"[batch] cancelled during segment extract: {e}")
                results.append(BatchResult(
                    output_index=match.output_index,
                    output_path=out_path,
                    success=False,
                    error=str(e),
                    cancelled=True,
                    match=match,
                ))
                break
            except Exception as e:
                log(f"[batch] ❌ extract segment failed: {e}")
                results.append(BatchResult(
                    output_index=match.output_index,
                    output_path=out_path,
                    success=False,
                    error=f"extract: {e}",
                    match=match,
                ))
                continue

            # Render
            def prog_cb(p: FfmpegProgress, _i=i, _total=total):
                if on_progress:
                    try:
                        on_progress(_i, _total, p)
                    except Exception:
                        pass

            target_duration_sec: Optional[float] = None
            ping_pong_product_to_target = False
            if batch_settings.uploaded_audio_controls_duration and match.audio_path:
                audio_key = os.fspath(match.audio_path)
                if audio_key not in audio_duration_cache:
                    try:
                        probed_duration = probe_audio_duration(audio_key, ffprobe_cmd)
                    except Exception:
                        probed_duration = None
                    if probed_duration is not None:
                        try:
                            numeric_duration = float(probed_duration)
                        except (TypeError, ValueError, OverflowError):
                            numeric_duration = 0.0
                    else:
                        numeric_duration = 0.0
                    audio_duration_cache[audio_key] = numeric_duration
                target_duration_sec = audio_duration_cache[audio_key]
                if not math.isfinite(target_duration_sec) or target_duration_sec <= 0:
                    message = f"audio duration probe failed: {audio_key}"
                    log(f"[batch] output {i}/{total} failed: {message}")
                    results.append(BatchResult(
                        output_index=match.output_index,
                        output_path=out_path,
                        success=False,
                        error=message,
                        match=match,
                    ))
                    _safe_remove(sub_clip)
                    continue
                ping_pong_product_to_target = True
                log(
                    f"[batch] Audio master: source_segment={match.segment.duration:.3f}s "
                    f"final={target_duration_sec:.3f}s"
                )

            try:
                result = render_green(
                    cover=match.cover_path,
                    product=sub_clip,
                    background=match.background_path,
                    audio=match.audio_path,
                    out_path=out_path,
                    settings=seg_settings,
                    on_log=log,
                    on_progress=prog_cb,
                    stop_check=stop_check,
                    ffmpeg_cmd=ffmpeg_cmd,
                    ffprobe_cmd=ffprobe_cmd,
                    tc_label=tc_label,
                    chroma_max_parallel=chroma_max_parallel,
                    target_duration_sec=target_duration_sec,
                    ping_pong_product_to_target=ping_pong_product_to_target,
                )
            except Exception as e:
                log(f"[batch] ❌ render failed: {e}")
                results.append(BatchResult(
                    output_index=match.output_index,
                    output_path=out_path,
                    success=False,
                    error=f"render: {e}",
                    match=match,
                ))
                # cleanup sub_clip
                _safe_remove(sub_clip)
                continue

            # cleanup sub_clip
            _safe_remove(sub_clip)

            results.append(BatchResult(
                output_index=match.output_index,
                output_path=out_path,
                success=result.success,
                error=result.error,
                duration_sec=result.duration_sec,
                cancelled=bool(getattr(result, "cancelled", False)),
            ))

            if result.success:
                log(f"[batch] ✅ {os.path.basename(out_path)}")
            elif bool(getattr(result, "cancelled", False)):
                log(f"[batch] cancelled during output {i}/{total}")
                break
            else:
                log(f"[batch] ❌ output {i}/{total} failed: {result.error[:200]}")

        success_count = sum(1 for r in results if r.success)
        cancelled_count = sum(1 for r in results if r.cancelled)
        fail_count = len(results) - success_count - cancelled_count
        log(
            f"[batch] done: {success_count} ok, {fail_count} fail, "
            f"{cancelled_count} cancelled (of {total})"
        )
        return results

    # Parallel path: N chromas concurrent (v3.PARALLEL 2026-08-18).
    # Each match → its own ffmpeg in ThreadPoolExecutor.
    log(f"[batch] parallel mode: {chroma_max_parallel} concurrent chromas over {total} matches")
    parallel_results: List[BatchResult] = []

    # v3.PIPELINE (2026-08-18): use functools.partial to pass context to workers
    import functools
    process_match = functools.partial(
        _process_one_match,
        matches=matches,
        out_dir=out_dir,
        base_settings=base_settings,
        batch_settings=batch_settings,
        audio_duration_cache=audio_duration_cache,
        effective_run_stamp=effective_run_stamp,
        on_match=on_match,
        on_progress=on_progress,
        stop_check=stop_check,
        ffmpeg_cmd=ffmpeg_cmd,
        ffprobe_cmd=ffprobe_cmd,
        tc_label=tc_label,
        chroma_max_parallel=chroma_max_parallel,
        pre_validated_outputs=pre_validated_outputs,
        log_fn=log,
    )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=chroma_max_parallel,
        thread_name_prefix="batch-chroma-",
    ) as ex:
        future_to_match = {ex.submit(process_match, m): m for m in matches}
        for fut in concurrent.futures.as_completed(future_to_match):
            try:
                result = fut.result()
            except Exception as exc:
                match = future_to_match[fut]
                parallel_results.append(BatchResult(
                    output_index=match.output_index,
                    output_path="",
                    success=False,
                    error=f"executor: {exc}",
                    match=match,
                ))
                continue
            parallel_results.append(result)

    results = sorted(parallel_results, key=lambda r: r.output_index)
    success_count = sum(1 for r in results if r.success)
    cancelled_count = sum(1 for r in results if r.cancelled)
    fail_count = len(results) - success_count - cancelled_count
    log(
        f"[batch] done: {success_count} ok, {fail_count} fail, "
        f"{cancelled_count} cancelled (of {total})"
    )
    return results


def _process_one_match(
    match: BatchMatch,
    matches: List[BatchMatch],
    out_dir: str,
    base_settings,
    batch_settings,
    audio_duration_cache: Dict[str, float],
    effective_run_stamp: str,
    on_match,
    on_progress,
    stop_check,
    ffmpeg_cmd: str,
    ffprobe_cmd: str,
    tc_label: str,
    chroma_max_parallel: int,
    pre_validated_outputs,
    log_fn,
) -> BatchResult:
    """Process one BatchMatch (segment extract + chroma). For parallel executor."""
    total = len(matches)
    log = log_fn
    out_name = (
        f"batch_{effective_run_stamp}_{match.output_index:03d}_"
        f"{os.path.splitext(os.path.basename(match.product_path))[0]}.mp4"
    )
    out_path = os.path.join(out_dir, out_name)

    if on_match:
        try:
            on_match(match)
        except Exception:
            pass

    log(f"[batch] [{match.output_index+1}/{total}] {os.path.basename(match.product_path)} "
        f"segment {match.segment.segment_index} ({'F' if match.segment.is_forward else 'B'}, "
        f"{match.segment.duration:.1f}s) parallel")

    if pre_validated_outputs and os.fspath(out_path) in pre_validated_outputs:
        log(f"[batch] [{match.output_index+1}/{total}] skipped (pre-validated)")
        return BatchResult(
            output_index=match.output_index,
            output_path=out_path,
            success=True, error="", duration_sec=0.0,
            match=match, skipped=True,
        )

    seg_settings = GreenSettings(
        width=base_settings.width,
        height=base_settings.height,
        fps=base_settings.fps,
        bitrate=base_settings.bitrate,
        encoder_alias=base_settings.encoder_alias,
        key_color=base_settings.key_color,
        similarity=base_settings.similarity,
        blend=base_settings.blend,
        despill=base_settings.despill,
        despill_screen=base_settings.despill_screen,
        cover_enabled=base_settings.cover_enabled,
        cover_duration=base_settings.cover_duration,
        cover_scale=base_settings.cover_scale,
        audio_source=base_settings.audio_source,
        preset=base_settings.preset,
    )

    try:
        sub_clip = _extract_segment(
            match.product_path,
            match.segment,
            out_dir,
            ffmpeg_cmd=ffmpeg_cmd,
            ffprobe_cmd=ffprobe_cmd,
            encoder_alias=base_settings.encoder_alias,
            stop_check=stop_check,
            on_log=log,
            tc_label=tc_label,
            run_stamp=effective_run_stamp,
        )
    except _BatchCancelled as e:
        return BatchResult(
            output_index=match.output_index, output_path=out_path,
            success=False, error=str(e), cancelled=True, match=match,
        )
    except Exception as e:
        return BatchResult(
            output_index=match.output_index, output_path=out_path,
            success=False, error=f"extract: {e}", match=match,
        )

    def prog_cb(p, _idx=match.output_index, _total=total):
        if on_progress:
            try:
                on_progress(_idx + 1, _total, p)
            except Exception:
                pass

    target_duration_sec = None
    ping_pong_product_to_target = False
    if batch_settings.uploaded_audio_controls_duration and match.audio_path:
        audio_key = os.fspath(match.audio_path)
        if audio_key not in audio_duration_cache:
            try:
                probed_duration = probe_audio_duration(audio_key, ffprobe_cmd)
            except Exception:
                probed_duration = None
            if probed_duration is not None:
                try:
                    numeric_duration = float(probed_duration)
                except (TypeError, ValueError, OverflowError):
                    numeric_duration = 0.0
            else:
                numeric_duration = 0.0
            audio_duration_cache[audio_key] = numeric_duration
        target_duration_sec = audio_duration_cache[audio_key]
        if not math.isfinite(target_duration_sec) or target_duration_sec <= 0:
            _safe_remove(sub_clip)
            return BatchResult(
                output_index=match.output_index, output_path=out_path,
                success=False, error="audio duration probe failed", match=match,
            )
        ping_pong_product_to_target = True

    try:
        result = render_green(
            cover=match.cover_path,
            product=sub_clip,
            background=match.background_path,
            audio=match.audio_path,
            out_path=out_path,
            settings=seg_settings,
            on_log=log,
            on_progress=prog_cb,
            stop_check=stop_check,
            ffmpeg_cmd=ffmpeg_cmd,
            ffprobe_cmd=ffprobe_cmd,
            tc_label=tc_label,
            chroma_max_parallel=chroma_max_parallel,
            target_duration_sec=target_duration_sec,
            ping_pong_product_to_target=ping_pong_product_to_target,
        )
    except Exception as e:
        _safe_remove(sub_clip)
        return BatchResult(
            output_index=match.output_index, output_path=out_path,
            success=False, error=f"render: {e}", match=match,
        )

    _safe_remove(sub_clip)
    if result.success:
        log(f"[batch] ✅ {os.path.basename(out_path)}")
    else:
        log(f"[batch] ❌ output {match.output_index+1}/{total} failed: {result.error[:200]}")
    return BatchResult(
        output_index=match.output_index,
        output_path=out_path,
        success=result.success,
        error=result.error,
        duration_sec=result.duration_sec,
        cancelled=bool(getattr(result, "cancelled", False)),
    )


# ==================== Helpers ====================

def _extract_ping_pong_segment(
    src_path: str,
    seg: PingPongSegment,
    target_duration: float,
    out_dir: str,
    *,
    fps: int = 30,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
    encoder_alias: str = "auto",
    stop_check: Optional[Callable[[], bool]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    tc_label: str = "",
    run_stamp: str = "",
) -> str:
    """Create one bounded F-B cycle, then loop it to the Audio duration.

    The number of filter nodes stays constant even when Audio is much longer
    than the source segment. The temporary cycle and filled clip contain video
    only; ``render_green`` maps the selected uploaded Audio afterward.
    """

    if not math.isfinite(seg.duration) or seg.duration <= 0:
        raise ValueError("ping-pong segment duration must be finite and positive")
    if not math.isfinite(target_duration) or target_duration <= seg.duration:
        raise ValueError(
            "ping-pong target duration must be finite and longer than the segment"
        )

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    stamp = re.sub(r"[^0-9A-Za-z_-]+", "_", str(run_stamp)).strip("_")
    stamp_part = f"_{stamp}" if stamp else ""
    stem = f"_seg{stamp_part}_{base}_{seg.segment_index:03d}"
    cycle_path = os.path.join(out_dir, f"{stem}_ppcycle.mp4")
    filled_path = os.path.join(out_dir, f"{stem}_ppfill.mp4")
    enc_name, enc_args = effective_video_encoder(
        preferred=resolve_encoder_alias(encoder_alias),
        ffmpeg_cmd=ffmpeg_cmd,
        disable_fallback=encoder_alias == "libx264",
    )
    if encoder_alias == "libx264" and enc_name != "libx264":
        raise RuntimeError("exact libx264 CPU fallback is unavailable")
    frame_rate = max(1, int(fps))
    cycle_duration = seg.duration * 2.0
    cycle_filter = (
        "[0:v:0]setpts=PTS-STARTPTS,split=2[ppf][ppr];"
        "[ppr]reverse,setpts=PTS-STARTPTS[ppb];"
        "[ppf][ppb]concat=n=2:v=1:a=0,format=yuv420p[ppv]"
    )
    cycle_cmd = [
        ffmpeg_cmd,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seg.time_range.start:.6f}",
        "-t",
        f"{seg.duration:.6f}",
        "-i",
        src_path,
        "-filter_complex",
        cycle_filter,
        "-map",
        "[ppv]",
        "-an",
        "-r",
        str(frame_rate),
        "-c:v",
        enc_name,
        *enc_args,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        cycle_path,
    ]
    fill_cmd = [
        ffmpeg_cmd,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stream_loop",
        "-1",
        "-i",
        cycle_path,
        "-map",
        "0:v:0",
        "-an",
        "-t",
        f"{target_duration:.6f}",
        "-r",
        str(frame_rate),
        "-c:v",
        enc_name,
        *enc_args,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        filled_path,
    ]

    try:
        cycle_result = FfmpegRunner(ffmpeg_cmd=ffmpeg_cmd).run(
            cycle_cmd,
            expected_duration_sec=cycle_duration,
            on_log=on_log,
            stop_check=stop_check,
            tc_label=tc_label,
        )
        if (
            encoder_alias != "libx264"
            and should_retry_with_cpu(cycle_cmd, cycle_result, stop_check=stop_check)
        ):
            gpu_error = (
                cycle_result.error
                or f"hardware encoder exited {cycle_result.returncode}"
            )
            _safe_remove(cycle_path)
            _safe_remove(filled_path)
            if on_log:
                on_log(
                    "[batch] hardware encoder failed during ping-pong cycle; "
                    "retrying the transaction with libx264"
                )
            try:
                return _extract_ping_pong_segment(
                    src_path,
                    seg,
                    target_duration,
                    out_dir,
                    fps=fps,
                    ffmpeg_cmd=ffmpeg_cmd,
                    ffprobe_cmd=ffprobe_cmd,
                    encoder_alias="libx264",
                    stop_check=stop_check,
                    on_log=on_log,
                    tc_label=tc_label,
                    run_stamp=run_stamp,
                )
            except _BatchCancelled:
                raise
            except Exception as cpu_exc:
                raise RuntimeError(
                    "hardware encoder failed during ping-pong cycle: "
                    f"{gpu_error}; CPU fallback failed: {cpu_exc}"
                ) from cpu_exc
        if cycle_result.cancelled:
            raise _BatchCancelled(
                cycle_result.error or "ping-pong cycle extraction cancelled"
            )
        if (
            not cycle_result.success
            or not os.path.isfile(cycle_path)
            or os.path.getsize(cycle_path) <= 0
        ):
            raise RuntimeError(
                "ping-pong cycle extraction failed: "
                f"{(cycle_result.error or 'unknown error')[:200]}"
            )

        fill_result = FfmpegRunner(ffmpeg_cmd=ffmpeg_cmd).run(
            fill_cmd,
            expected_duration_sec=target_duration,
            on_log=on_log,
            stop_check=stop_check,
            tc_label=tc_label,
        )
        if (
            encoder_alias != "libx264"
            and should_retry_with_cpu(fill_cmd, fill_result, stop_check=stop_check)
        ):
            gpu_error = (
                fill_result.error
                or f"hardware encoder exited {fill_result.returncode}"
            )
            _safe_remove(cycle_path)
            _safe_remove(filled_path)
            if on_log:
                on_log(
                    "[batch] hardware encoder failed during ping-pong fill; "
                    "retrying the transaction with libx264"
                )
            try:
                return _extract_ping_pong_segment(
                    src_path,
                    seg,
                    target_duration,
                    out_dir,
                    fps=fps,
                    ffmpeg_cmd=ffmpeg_cmd,
                    ffprobe_cmd=ffprobe_cmd,
                    encoder_alias="libx264",
                    stop_check=stop_check,
                    on_log=on_log,
                    tc_label=tc_label,
                    run_stamp=run_stamp,
                )
            except _BatchCancelled:
                raise
            except Exception as cpu_exc:
                raise RuntimeError(
                    "hardware encoder failed during ping-pong fill: "
                    f"{gpu_error}; CPU fallback failed: {cpu_exc}"
                ) from cpu_exc
        if fill_result.cancelled:
            raise _BatchCancelled(
                fill_result.error or "ping-pong Audio fill cancelled"
            )
        if (
            not fill_result.success
            or not os.path.isfile(filled_path)
            or os.path.getsize(filled_path) <= 0
        ):
            raise RuntimeError(
                "ping-pong Audio fill failed: "
                f"{(fill_result.error or 'unknown error')[:200]}"
            )

        if not has_video_stream(
            filled_path,
            ffprobe_cmd=ffprobe_cmd,
            ffmpeg_cmd=ffmpeg_cmd,
        ):
            raise RuntimeError(
                "ping-pong Audio fill validation failed: "
                "missing decodable video stream"
            )
        actual_duration = _probe_duration(filled_path, ffprobe_cmd)
        if not _duration_matches_expected(
            actual_duration,
            target_duration,
            frame_rate,
        ):
            tolerance = _duration_tolerance(target_duration, frame_rate)
            raise RuntimeError(
                "ping-pong Audio fill validation failed: "
                f"duration {actual_duration:.3f}s differs from expected "
                f"{target_duration:.3f}s (tolerance {tolerance:.3f}s)"
            )
        return filled_path
    except Exception:
        _safe_remove(filled_path)
        raise
    finally:
        _safe_remove(cycle_path)


def _extract_segment(
    src_path: str,
    seg: PingPongSegment,
    out_dir: str,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
    encoder_alias: str = "auto",
    stop_check: Optional[Callable[[], bool]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    tc_label: str = "",
    run_stamp: str = "",
) -> str:
    """
    Extract sub-clip ตาม TimeRange + direction (forward/backward)
    Returns path ของไฟล์ mp4 ชั่วคราว
    """
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    direction = "fwd" if seg.is_forward else "bwd"
    stamp = re.sub(r"[^0-9A-Za-z_-]+", "_", str(run_stamp)).strip("_")
    stamp_part = f"_{stamp}" if stamp else ""
    out_name = f"_seg{stamp_part}_{base}_{seg.segment_index:03d}_{direction}.mp4"
    out_path = os.path.join(out_dir, out_name)

    if seg.is_forward:
        enc_name, enc_args = effective_video_encoder(
            preferred=resolve_encoder_alias(encoder_alias),
            ffmpeg_cmd=ffmpeg_cmd,
            disable_fallback=encoder_alias == "libx264",
        )
        if encoder_alias == "libx264" and enc_name != "libx264":
            raise RuntimeError("exact libx264 CPU fallback is unavailable")
        cmd = [
            ffmpeg_cmd, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src_path,
            "-ss", f"{seg.time_range.start:.3f}",
            "-t", f"{seg.duration:.3f}",
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", enc_name,
            *enc_args,
            "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_path,
        ]
    else:
        enc_name, enc_args = effective_video_encoder(
            preferred=resolve_encoder_alias(encoder_alias),
            ffmpeg_cmd=ffmpeg_cmd,
            disable_fallback=encoder_alias == "libx264",
        )
        if encoder_alias == "libx264" and enc_name != "libx264":
            raise RuntimeError("exact libx264 CPU fallback is unavailable")
        # Backward direction: trim the source range and reverse the video.
        # (Audio is reversed as well so the loop stays in sync.)
        # Source range = [next_boundary - duration, next_boundary] which
        # corresponds to the forward range that the PingPongSegment has
        # already pre-computed; we just play it backwards here.
        cmd = [
            ffmpeg_cmd, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seg.time_range.start:.3f}",
            "-i", src_path,
            "-t", f"{seg.duration:.3f}",
            "-vf", "reverse",
            "-af", "areverse",  # reverse audio ด้วย
            "-c:v", enc_name,
            *enc_args,
            "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            out_path,
        ]

    result = FfmpegRunner(ffmpeg_cmd=ffmpeg_cmd).run(
        cmd,
        expected_duration_sec=seg.duration,
        on_log=on_log,
        stop_check=stop_check,
        tc_label=tc_label,
    )
    if (
        encoder_alias != "libx264"
        and should_retry_with_cpu(cmd, result, stop_check=stop_check)
    ):
        gpu_error = result.error or f"hardware encoder exited {result.returncode}"
        _safe_remove(out_path)
        if on_log:
            on_log(
                "[batch] hardware encoder failed during segment extraction; "
                "retrying once with libx264"
            )
        try:
            return _extract_segment(
                src_path,
                seg,
                out_dir,
                ffmpeg_cmd=ffmpeg_cmd,
                ffprobe_cmd=ffprobe_cmd,
                encoder_alias="libx264",
                stop_check=stop_check,
                on_log=on_log,
                tc_label=tc_label,
                run_stamp=run_stamp,
            )
        except _BatchCancelled:
            raise
        except Exception as cpu_exc:
            raise RuntimeError(
                "hardware encoder failed during segment extraction: "
                f"{gpu_error}; CPU fallback failed: {cpu_exc}"
            ) from cpu_exc
    if result.cancelled:
        _safe_remove(out_path)
        raise _BatchCancelled(result.error or "segment extraction cancelled")
    if not result.success or not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        _safe_remove(out_path)
        raise RuntimeError(
            f"segment extract failed: {(result.error or 'unknown error')[:200]}"
        )

    validation_error = ""
    if not has_video_stream(
        out_path,
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
    ):
        validation_error = "missing decodable video stream"
    else:
        actual_duration = _probe_duration(out_path, ffprobe_cmd)
        tolerance = _duration_tolerance(seg.duration)
        if not _duration_matches_expected(actual_duration, seg.duration):
            validation_error = (
                f"duration {actual_duration:.3f}s differs from expected "
                f"{seg.duration:.3f}s (tolerance {tolerance:.3f}s)"
            )
    if validation_error:
        _safe_remove(out_path)
        raise RuntimeError(f"segment extract validation failed: {validation_error}")
    return out_path


def _safe_remove(path: str):
    """ลบไฟล์แบบ silent (ไม่ raise)"""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass
