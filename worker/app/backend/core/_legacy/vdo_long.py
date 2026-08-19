"""
VdoLongProcessor - Long video segment processing for Auto MV

Supports 3 modes:
- Mode 1: Normal (random files from vdo_long/ root)
- Mode 2: Sequential (long_xxx/ → footage_xxx/ → split)
- Mode 3: Footage (existing footage_xxx/ with pre-split segments)

Flow:
1. Detect mode based on folder structure
2. Select/move videos to temp location
3. Split long videos into segments
4. Build output MV from segments + audio
5. Clean up used segments

Code by Mini Max 2.1, 2026-01-05
"""
import os
import random
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, List, Callable, Dict, NamedTuple
from datetime import datetime

from .runlog import SegmentUsage
from .models import load_title_rows, select_title_row


class SegmentInfo(NamedTuple):
    """Info about a video segment"""
    path: str
    duration: float
    base_id: str   # e.g., "MVI_5845"
    lens_id: str   # e.g., "lens1_16mm" or "16mm"


@dataclass
class VdoLongConfig:
    """Configuration for vdo_long processing"""
    # Mode selection
    mode: int = 1  # 1=Normal, 2=Sequential, 3=Footage

    # Mode 1 options
    footage_manual: int = 3  # Number of videos to select

    # Mode 2/3 options
    use_existing_footage: bool = False  # Mode 3: use existing footage_xxx

    # Common options
    delete_used_segments: bool = True
    exhaust_segments: bool = False
    exhaust_min_types: int = 3
    segment_duration: float = 5.0  # Target segment length in seconds
    segments_per_audio: int = 3    # Number of segments per audio file

    # Output options (inherited from AutoMV)
    encoder: str = "libx264"
    bitrate: str = "8000k"
    target_wh: Tuple[int, int] = (1920, 1080)
    text_enabled: bool = True
    text_pos: str = "Bottom Center"
    text_size: int = 64
    overlay_template: str = "{name}"
    fontfile: Optional[str] = None
    ai_enabled: bool = False

    # CSV overlay support
    overlay_config: Dict = field(default_factory=dict)


class VdoLongProcessor:
    """
    Process long videos from vdo_long/ folder.

    Usage:
        config = VdoLongConfig(mode=1, footage_manual=3)
        processor = VdoLongProcessor(config, logger=log_func)
        processor.process_product(product_path, audio_files, output_dir)
    """

    VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".m4v", ".avi")

    def __init__(self, config: VdoLongConfig,
                 logger: Callable[[str], None],
                 ffmpeg_cmd: str = "ffmpeg",
                 ffprobe_cmd: str = "ffprobe",
                 run_logger: 'RunLogger' = None):
        self.config = config
        self.log = logger
        self.ffmpeg_cmd = ffmpeg_cmd
        self.ffprobe_cmd = ffprobe_cmd
        self.durations: Dict[str, float] = {}
        self.run_logger = run_logger  # Optional RunLogger for history

    def _normalize_hex(self, color: str) -> str:
        """Normalize hex color to 6 digits without #."""
        c = (color or "").strip()
        if c.startswith("#"):
            c = c[1:]
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return c if len(c) == 6 else "000000"

    def _get_pos_coords(self, pos: str) -> Tuple[str, str]:
        positions = {
            "Top Center": ("(w-text_w)/2", "30"),
            "Center": ("(w-text_w)/2", "(h-text_h)/2"),
            "Bottom Center": ("(w-text_w)/2", "h-text_h-30"),
            "Top Right": ("w-text_w-30", "30"),
            "Top Left": ("30", "30"),
            "Bottom Right": ("w-text_w-30", "h-text_h-30"),
            "Bottom Left": ("30", "h-text_h-30"),
        }
        return positions.get(pos, ("(w-text_w)/2", "30"))

    def _collect_ai_candidates(self, ai_root: Path) -> List[Path]:
        """Collect AI clips grouped by top-level folder and pick one per group (folder-ordered, file-random)."""
        if not ai_root.is_dir():
            return []
        video_exts = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
        all_files: List[Path] = []
        for p in ai_root.rglob("*"):
            if p.is_file() and p.suffix.lower() in video_exts:
                all_files.append(p)
        if not all_files:
            return []
        groups: Dict[str, List[Path]] = {}
        for p in all_files:
            rel = p.relative_to(ai_root)
            key = rel.parts[0] if len(rel.parts) > 1 else ""
            groups.setdefault(key, []).append(p)
        selected: List[Path] = []
        for key in sorted(groups.keys()):
            bucket = groups[key][:]
            random.shuffle(bucket)
            selected.append(bucket[0])
        return selected

    def _normalize_ai_clip(self, src: Path, dst: Path) -> bool:
        """Normalize AI intro to target size/fps."""
        w, h = self.config.target_wh
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            "fps=30,format=yuv420p"
        )
        cmd = [
            self.ffmpeg_cmd, "-y",
            "-i", str(src),
            "-vf", vf,
            "-c:v", self.config.encoder,
            "-preset", "slow",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            str(dst),
        ]
        self.log(f"[AI] normalize {src.name} -> {dst.name}")
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            self.log(f"[AI] normalize failed: {result.stderr.strip()}")
            return False
        return True

    def _build_ai_intro(self, product_dir: Path, idx: int) -> Optional[Path]:
        """Collect, move, and normalize one AI intro clip (like V20)."""
        ai_root = product_dir / "vdo_ai"
        if not ai_root.is_dir():
            return None
        candidates = self._collect_ai_candidates(ai_root)
        if not candidates:
            return None
        # pick first after grouping (groups already shuffled internally)
        src = sorted(candidates)[0]
        tmp_ai_dir = product_dir / "tmp_ai"
        tmp_ai_dir.mkdir(parents=True, exist_ok=True)
        moved = tmp_ai_dir / src.name
        try:
            shutil.move(str(src), str(moved))
        except Exception as e:
            self.log(f"[AI] move failed: {src} -> {moved}: {e}")
            return None

        intro = tmp_ai_dir / f"ai_intro_{idx:03d}.mp4"
        if not self._normalize_ai_clip(moved, intro):
            return None
        return intro

    def _collect_ai_soundless_files(self, ai_soundless_dir: Path) -> List[Path]:
        """Collect AI soundless clips (no grouping, keep list for random pick)."""
        if not ai_soundless_dir.is_dir():
            return []
        video_exts = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
        files = [p for p in ai_soundless_dir.iterdir() if p.is_file() and p.suffix.lower() in video_exts]
        return sorted(files)

    # ==================== Mode Detection ====================

    def detect_mode(self, vdo_long_dir: str) -> int:
        """
        Auto-detect processing mode based on folder structure.

        Returns:
            1: Normal Mode (files at root)
            2: Sequential Mode (long_xxx folders exist)
            3: Footage Mode (footage_xxx folders exist)
        """
        if not os.path.isdir(vdo_long_dir):
            return 1

        has_long_folders = False
        has_footage_folders = False
        has_root_files = False

        for item in os.listdir(vdo_long_dir):
            item_path = os.path.join(vdo_long_dir, item)
            if os.path.isdir(item_path):
                if item.startswith("long_") and self._is_number_suffix(item):
                    has_long_folders = True
                elif item.startswith("footage_") and self._is_number_suffix(item):
                    has_footage_folders = True
            elif item.lower().endswith(self.VIDEO_EXTS):
                has_root_files = True

        # Priority: Mode 2 > Mode 3 > Mode 1
        if has_long_folders:
            return 2
        elif has_footage_folders:
            return 3
        else:
            return 1

    def _is_number_suffix(self, name: str) -> bool:
        """Check if name ends with _xxx where xxx is a number"""
        parts = name.rsplit("_", 1)
        return len(parts) == 2 and parts[1].isdigit()

    # ==================== File Selection ====================

    def get_long_folders(self, vdo_long_dir: str) -> List[Tuple[str, int]]:
        """
        Get list of long_xxx folders sorted by number.

        Returns:
            List of (folder_path, number) tuples
        """
        folders = []
        for item in os.listdir(vdo_long_dir):
            item_path = os.path.join(vdo_long_dir, item)
            if os.path.isdir(item_path) and item.startswith("long_"):
                suffix = item[5:]  # Remove "long_"
                if suffix.isdigit():
                    folders.append((item_path, int(suffix)))
        return sorted(folders, key=lambda x: x[1])

    def get_footage_folders(self, vdo_long_dir: str) -> List[Tuple[str, int]]:
        """
        Get list of footage_xxx folders sorted by number.

        Returns:
            List of (folder_path, number) tuples
        """
        folders = []
        for item in os.listdir(vdo_long_dir):
            item_path = os.path.join(vdo_long_dir, item)
            if os.path.isdir(item_path) and item.startswith("footage_"):
                suffix = item[8:]  # Remove "footage_"
                if suffix.isdigit():
                    folders.append((item_path, int(suffix)))
        return sorted(folders, key=lambda x: x[1])

    def get_root_video_files(self, vdo_long_dir: str) -> List[str]:
        """Get video files at root of vdo_long/"""
        files = []
        for item in os.listdir(vdo_long_dir):
            if item.lower().endswith(self.VIDEO_EXTS):
                files.append(os.path.join(vdo_long_dir, item))
        return sorted(files)

    # ==================== Lens Classification ====================

    def parse_lens_from_filename(self, filename: str) -> Optional[str]:
        """
        Extract lens info from filename.

        Examples:
            "MVI_5845_lens1_16mm.mp4" -> "lens1_16mm" or "16mm"
            "A_24mm.mp4" -> "24mm"
            "video.mp4" -> None

        Returns:
            Lens identifier or None if not parseable
        """
        name = os.path.splitext(filename)[0]

        # Pattern 1: *_16mm.mp4 or *_lens1_16mm.mp4
        import re
        patterns = [
            r'_(\d+)mm$',           # A_16mm, video_85mm
            r'_lens\d*_(\d+)mm$',   # lens1_16mm, lens_24mm
        ]

        for pattern in patterns:
            match = re.search(pattern, name, re.IGNORECASE)
            if match:
                return f"{match.group(1)}mm"

        return None

    def get_lens_group(self, lens_id: str) -> str:
        """
        Classify lens into wide/mid/tele group.

        Returns:
            "wide" (<35mm), "mid" (35-50mm), "tele" (>=51mm)
        """
        if not lens_id:
            return "unknown"

        # Extract number
        import re
        match = re.search(r'(\d+)', lens_id)
        if not match:
            return "unknown"

        mm = int(match.group(1))
        if mm < 35:
            return "wide"
        elif mm < 51:
            return "mid"
        else:
            return "tele"

    def select_balanced_videos(self, files: List[str], count: int) -> List[str]:
        """
        Select videos balanced across wide/mid/tele groups.
        """
        if not files:
            return []

        # Group by lens type
        groups: Dict[str, List[str]] = {"wide": [], "mid": [], "tele": [], "unknown": []}
        for f in files:
            lens = self.parse_lens_from_filename(os.path.basename(f))
            group = self.get_lens_group(lens)
            groups[group].append(f)

        selected = []
        remaining = count

        # Take one from each group first (if available)
        for group in ["wide", "mid", "tele"]:
            if remaining <= 0:
                break
            if groups[group]:
                selected.append(groups[group].pop(0))
                remaining -= 1

        # Fill remaining with random from any group
        all_remaining = []
        for g in groups.values():
            all_remaining.extend(g)

        while remaining > 0 and all_remaining:
            selected.append(all_remaining.pop(random.randint(0, len(all_remaining) - 1)))
            remaining -= 1

        return selected

    # ==================== Video Splitting ====================

    def ffprobe_duration(self, path: str) -> float:
        """Get video duration using ffprobe"""
        if path in self.durations:
            return self.durations[path]

        cmd = [
            self.ffprobe_cmd, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path
        ]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                          text=True, encoding="utf-8", errors="replace").strip()
            d = float(out)
            self.durations[path] = d
            return d
        except Exception as e:
            raise RuntimeError(f"ffprobe error for {path}: {e}")

    def split_video_to_segments(self, video_path: str, output_dir: str,
                                 segment_duration: float = 5.0) -> List[SegmentInfo]:
        """
        Split a long video into segments of approximately segment_duration seconds.

        Returns:
            List of SegmentInfo for each segment created
        """
        os.makedirs(output_dir, exist_ok=True)

        base_name = os.path.splitext(os.path.basename(video_path))[0]
        video_duration = self.ffprobe_duration(video_path)

        segments = []
        start_time = 0.0
        seg_idx = 1

        while start_time < video_duration:
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user")

            end_time = min(start_time + segment_duration, video_duration)
            seg_duration = end_time - start_time

            if seg_duration < 1.0:  # Skip segments shorter than 1 second
                break

            seg_name = f"{base_name}_seg{seg_idx:03d}.mp4"
            seg_path = os.path.join(output_dir, seg_name)

            # FFmpeg command to extract segment
            cmd = [
                self.ffmpeg_cmd, "-hide_banner", "-loglevel", "warning",
                "-i", video_path,
                "-ss", str(start_time),
                "-t", str(seg_duration),
                "-c", "copy",  # Copy streams (fast, no re-encode)
                "-avoid_negative_ts", "make_zero",
                seg_path
            ]

            self.log(f"[Split] Creating segment {seg_idx}: {start_time:.1f}s - {end_time:.1f}s")
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")

            if result.returncode == 0 and os.path.exists(seg_path):
                # Parse lens info from original filename
                lens = self.parse_lens_from_filename(os.path.basename(video_path))
                base_id = base_name

                segments.append(SegmentInfo(
                    path=seg_path,
                    duration=seg_duration,
                    base_id=base_id,
                    lens_id=lens or "unknown"
                ))
            else:
                self.log(f"[Split] Failed: {result.stderr}")

            start_time = end_time
            seg_idx += 1

        self.log(f"[Split] Created {len(segments)} segments from {os.path.basename(video_path)}")
        return segments

    # ==================== Segment Management ====================

    def build_segment_map(self, segments: List[SegmentInfo]) -> Dict[str, List[SegmentInfo]]:
        """
        Build a map from lens_id to list of segments.

        Used for balanced selection across lens types.
        """
        seg_map: Dict[str, List[SegmentInfo]] = {}
        for seg in segments:
            if seg.lens_id not in seg_map:
                seg_map[seg.lens_id] = []
            seg_map[seg.lens_id].append(seg)

        # Shuffle each list for random selection
        for lens_id in seg_map:
            random.shuffle(seg_map[lens_id])

        return seg_map

    def choose_segments_for_audio(self, seg_map: Dict[str, List[SegmentInfo]],
                                   audio_duration: float,
                                   segments_per_audio: int = 3) -> List[SegmentInfo]:
        """
        Choose exactly N segments for one audio file.

        Tries to pick from different lens types for variety.
        If segment is shorter than segment_duration, concatenate multiple segments.

        Args:
            seg_map: Map of lens_id to list of segments
            audio_duration: Duration of audio (for reference, not strictly followed)
            segments_per_audio: Number of segments to select per audio

        Returns:
            List of segments to use
        """
        selected = []
        used_lens = set()

        for _ in range(segments_per_audio):
            # Find available lens types
            available = [lid for lid, segs in seg_map.items() if segs and lid not in used_lens]

            if not available:
                # Allow reuse of lens types if we're running out
                available = [lid for lid, segs in seg_map.items() if segs]

            if not available:
                break  # No more segments available

            # Pick a lens type (prefer unused ones)
            lens_id = random.choice(available)
            if seg_map[lens_id]:
                seg = seg_map[lens_id].pop(0)
                selected.append(seg)
                used_lens.add(lens_id)

        return selected

    def consume_used_segments(self, segments: List[SegmentInfo],
                              seg_map: Dict[str, List[SegmentInfo]],
                              delete: bool = True):
        """
        Mark segments as used and optionally delete them.
        """
        if not delete:
            return

        for seg in segments:
            # Remove from seg_map
            if seg.lens_id in seg_map:
                # Already removed by pop, just clean up file
                pass

            # Delete the file
            if os.path.exists(seg.path):
                try:
                    os.remove(seg.path)
                    self.log(f"[Delete] Removed {os.path.basename(seg.path)}")
                except Exception as e:
                    self.log(f"[Delete] Failed to remove {seg.path}: {e}")

    # ==================== Main Processing ====================

    def process_product(self, product_path: str,
                        audio_files: List[str],
                        stop_check: Optional[Callable[[], bool]] = None) -> List[str]:
        """
        Process vdo_long folder to create MVs.

        Args:
            product_path: Path to product folder (contains vdo_long/, audio/)
            audio_files: List of audio files to create MVs for
            stop_check: Optional callback to check for stop signal

        Returns:
            List of output file paths created
        """
        vdo_long_dir = os.path.join(product_path, "vdo_long")
        audio_dir = os.path.join(product_path, "audio")
        product_dir_path = Path(product_path)

        if not os.path.isdir(vdo_long_dir):
            raise ValueError(f"vdo_long folder not found: {vdo_long_dir}")

        # Auto-detect mode if not specified
        mode = self.config.mode
        if mode == 0:
            mode = self.detect_mode(vdo_long_dir)
            self.log(f"[Mode] Auto-detected Mode {mode}")

        # Setup temp directories
        tmp_dir = os.path.join(product_path, "tmp")
        seg_dir = os.path.join(tmp_dir, "seg")
        tmp_seg_dir = os.path.join(tmp_dir, "tmp_seg")

        os.makedirs(tmp_dir, exist_ok=True)

        # Start RunLogger if available
        if self.run_logger:
            config_dict = {
                "mode": mode,
                "footage_manual": self.config.footage_manual,
                "segment_duration": self.config.segment_duration,
                "segments_per_audio": self.config.segments_per_audio,
                "exhaust_segments": self.config.exhaust_segments,
                "encoder": self.config.encoder,
                "bitrate": self.config.bitrate,
                "text_enabled": self.config.text_enabled,
            }
            self.run_logger.start_run(config_dict, mode)

        # Process based on mode
        if mode == 1:
            return self._process_mode1(vdo_long_dir, audio_files, tmp_dir, seg_dir, stop_check, product_dir_path)
        elif mode == 2:
            return self._process_mode2(vdo_long_dir, audio_files, tmp_dir, tmp_seg_dir, stop_check, product_dir_path)
        elif mode == 3:
            return self._process_mode3(vdo_long_dir, audio_files, tmp_dir, tmp_seg_dir, stop_check, product_dir_path)
        else:
            raise ValueError(f"Invalid mode: {mode}")

    def _process_mode1(self, vdo_long_dir: str, audio_files: List[str],
                       tmp_dir: str, seg_dir: str,
                       stop_check: Optional[Callable[[], bool]],
                       product_dir: Path) -> List[str]:
        """
        Mode 1: Normal Mode
        - Files at vdo_long/*.mp4
        - Select balanced, move to tmp/, split to seg/
        """
        self.log("[Mode1] Normal Mode processing")

        # Get all video files
        all_files = self.get_root_video_files(vdo_long_dir)
        if not all_files:
            raise ValueError("No video files in vdo_long/")

        # Select balanced videos
        use_count = min(self.config.footage_manual, len(all_files))
        selected = self.select_balanced_videos(all_files, use_count)

        self.log(f"[Mode1] Selected {len(selected)} videos")

        # Move to tmp and split
        all_segments = []
        for video in selected:
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user")

            # Move to tmp
            tmp_video = os.path.join(tmp_dir, os.path.basename(video))
            if os.path.exists(video):
                shutil.move(video, tmp_video)

            # Split
            segs = self.split_video_to_segments(tmp_video, seg_dir, self.config.segment_duration)
            all_segments.extend(segs)

        if not all_segments:
            raise ValueError("No segments created")

        # Build segment map
        seg_map = self.build_segment_map(all_segments)

        # Process each audio file
        ai_mode, ai_soundless_files = self._detect_ai_mode(product_dir)
        return self._process_outputs(audio_files, seg_map, tmp_dir, stop_check, product_dir, ai_mode, ai_soundless_files)

    def _process_mode2(self, vdo_long_dir: str, audio_files: List[str],
                       tmp_dir: str, tmp_seg_dir: str,
                       stop_check: Optional[Callable[[], bool]],
                       product_dir: Path) -> List[str]:
        """
        Mode 2: Sequential Mode - long to footage
        - long_xxx/ folders with long videos
        - Split each video in place to footage_xxx/
        - Move segments to tmp_seg/xxx/
        """
        self.log("[Mode2] Sequential Mode (long_xxx -> footage_xxx)")

        long_folders = self.get_long_folders(vdo_long_dir)
        if not long_folders:
            raise ValueError("No long_xxx folders found")

        # Create footage folders and split
        seq_segments: Dict[int, List[SegmentInfo]] = {}  # folder_num -> segments

        for folder_path, folder_num in long_folders:
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user")

            footage_folder = os.path.join(vdo_long_dir, f"footage_{folder_num:03d}")
            os.makedirs(footage_folder, exist_ok=True)

            # Get videos in long folder
            videos = [os.path.join(folder_path, f) for f in os.listdir(folder_path)
                     if f.lower().endswith(self.VIDEO_EXTS)]

            for video in videos:
                # Move to footage as _tmp_xxx
                tmp_name = f"_tmp_{os.path.basename(video)}"
                tmp_path = os.path.join(footage_folder, tmp_name)
                shutil.move(video, tmp_path)

                # Split in footage folder
                segs = self.split_video_to_segments(tmp_path, footage_folder, self.config.segment_duration)

                # Remove _tmp file
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

                if segs:
                    seq_segments[folder_num] = segs
                    self.log(f"[Mode2] Footage {folder_num}: {len(segs)} segments")

        # Move all segments to tmp_seg
        for folder_num, segs in seq_segments.items():
            dest_folder = os.path.join(tmp_seg_dir, f"{folder_num:03d}")
            os.makedirs(dest_folder, exist_ok=True)
            for seg in segs:
                dest = os.path.join(dest_folder, os.path.basename(seg.path))
                shutil.move(seg.path, dest)
                # Update path in seg
                seg = SegmentInfo(dest, seg.duration, seg.base_id, seg.lens_id)
                seq_segments[folder_num][seq_segments[folder_num].index(seg)] = seg

        # Process each audio file
        ai_mode, ai_soundless_files = self._detect_ai_mode(product_dir)
        return self._process_outputs_sequential(audio_files, seq_segments, tmp_dir, stop_check, product_dir, ai_mode, ai_soundless_files)

    def _process_mode3(self, vdo_long_dir: str, audio_files: List[str],
                       tmp_dir: str, tmp_seg_dir: str,
                       stop_check: Optional[Callable[[], bool]],
                       product_dir: Path) -> List[str]:
        """
        Mode 3: Footage Mode
        - Existing footage_xxx/ folders with segments
        - Move segments to tmp_seg/xxx/ and use directly
        """
        self.log("[Mode3] Footage Mode (existing footage_xxx)")

        footage_folders = self.get_footage_folders(vdo_long_dir)
        if not footage_folders:
            raise ValueError("No footage_xxx folders found")

        seq_segments: Dict[int, List[SegmentInfo]] = {}

        for folder_path, folder_num in footage_folders:
            segs = []
            for f in os.listdir(folder_path):
                if f.lower().endswith(self.VIDEO_EXTS) and not f.startswith("_tmp_"):
                    seg_path = os.path.join(folder_path, f)
                    segs.append(SegmentInfo(
                        path=seg_path,
                        duration=self.ffprobe_duration(seg_path),
                        base_id=os.path.splitext(f)[0],
                        lens_id=self.parse_lens_from_filename(f) or "unknown"
                    ))

            if segs:
                seq_segments[folder_num] = segs
                self.log(f"[Mode3] Footage {folder_num}: {len(segs)} segments")

            # Move to tmp_seg
            dest_folder = os.path.join(tmp_seg_dir, f"{folder_num:03d}")
            os.makedirs(dest_folder, exist_ok=True)
            for seg in segs:
                dest = os.path.join(dest_folder, os.path.basename(seg.path))
                shutil.move(seg.path, dest)
                seg = SegmentInfo(dest, seg.duration, seg.base_id, seg.lens_id)
                seq_segments[folder_num][seq_segments[folder_num].index(seg)] = seg

        ai_mode, ai_soundless_files = self._detect_ai_mode(product_dir)
        return self._process_outputs_sequential(audio_files, seq_segments, tmp_dir, stop_check, product_dir, ai_mode, ai_soundless_files)

    def _process_outputs(self, audio_files: List[str],
                         seg_map: Dict[str, List[SegmentInfo]],
                         tmp_dir: str,
                         stop_check: Optional[Callable[[], bool]],
                         product_dir: Path,
                         ai_mode: str,
                         ai_soundless_files: List[Path]) -> List[str]:
        """Process audio files with segments (Mode 1)"""
        outputs = []

        for i, audio in enumerate(audio_files, 1):
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user")

            self.log(f"[{i}/{len(audio_files)}] Processing: {os.path.basename(audio)}")

            # Get audio duration
            audio_duration = self.ffprobe_duration(audio)

            # Choose segments
            chosen = self.choose_segments_for_audio(seg_map, audio_duration, self.config.segments_per_audio)

            if not chosen:
                self.log(f"[{i}] No segments available, skipping")
                # Log to RunLogger
                if self.run_logger:
                    self.run_logger.log_result(audio, None, False, "No segments available")
                continue

            # Create output
            try:
                out_path = self._create_output(audio, chosen, tmp_dir, i, len(audio_files), audio_duration, product_dir, ai_mode, ai_soundless_files)
                outputs.append(out_path)

                # Log success to RunLogger
                if self.run_logger:
                    segments_for_log = [
                        SegmentUsage(
                            path=seg.path,
                            lens_id=seg.lens_id,
                            duration=seg.duration,
                            audio_file=audio
                        )
                        for seg in chosen
                    ]
                    self.run_logger.log_result(audio, out_path, True, segments_used=segments_for_log)

            except Exception as e:
                # Log failure to RunLogger
                if self.run_logger:
                    self.run_logger.log_result(audio, None, False, str(e))
                raise

            # Consume segments
            self.consume_used_segments(chosen, seg_map, self.config.delete_used_segments)

        # End run
        if self.run_logger:
            self.run_logger.end_run()

        # Cleanup
        self._cleanup_tmp(tmp_dir)

        return outputs

    def _process_outputs_sequential(self, audio_files: List[str],
                                    seq_segments: Dict[int, List[SegmentInfo]],
                                    tmp_dir: str,
                                    stop_check: Optional[Callable[[], bool]],
                                    product_dir: Path,
                                    ai_mode: str,
                                    ai_soundless_files: List[Path]) -> List[str]:
        """Process audio files with sequential segments (Mode 2/3)"""
        outputs = []

        for i, audio in enumerate(audio_files, 1):
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user")

            self.log(f"[{i}/{len(audio_files)}] Processing: {os.path.basename(audio)}")

            audio_duration = self.ffprobe_duration(audio)

            # Choose one segment from each available footage folder
            chosen = self._choose_sequential_segments(seq_segments, audio_duration)

            if not chosen:
                self.log(f"[{i}] No segments available, skipping")
                if self.run_logger:
                    self.run_logger.log_result(audio, None, False, "No segments available")
                continue

            # Create output
            try:
                out_path = self._create_output(audio, chosen, tmp_dir, i, len(audio_files), audio_duration, product_dir, ai_mode, ai_soundless_files)
                outputs.append(out_path)

                # Log success to RunLogger
                if self.run_logger:
                    segments_for_log = [
                        SegmentUsage(
                            path=seg.path,
                            lens_id=seg.lens_id,
                            duration=seg.duration,
                            audio_file=audio
                        )
                        for seg in chosen
                    ]
                    self.run_logger.log_result(audio, out_path, True, segments_used=segments_for_log)

            except Exception as e:
                if self.run_logger:
                    self.run_logger.log_result(audio, None, False, str(e))
                raise

            # Delete used segments
            for seg in chosen:
                if os.path.exists(seg.path):
                    os.remove(seg.path)

        # End run
        if self.run_logger:
            self.run_logger.end_run()

        # Cleanup
        self._cleanup_tmp(tmp_dir)

        return outputs

    def _choose_sequential_segments(self, seq_segments: Dict[int, List[SegmentInfo]],
                                    audio_duration: float) -> List[SegmentInfo]:
        """Choose segments from sequential folders"""
        chosen = []
        need = audio_duration

        # Sort folder numbers
        folder_nums = sorted(seq_segments.keys())

        while need > 0 and folder_nums:
            for fn in list(folder_nums):
                if need <= 0:
                    break

                segs = seq_segments[fn]
                if segs:
                    seg = segs.pop(0)
                    chosen.append(seg)
                    need -= seg.duration
                else:
                    # No more segments in this folder
                    folder_nums.remove(fn)

        return chosen

    def _create_output(self, audio_path: str, segments: List[SegmentInfo],
                       tmp_dir: str, idx: int, total: int, audio_duration: float,
                       product_dir: Path, ai_mode: str, ai_soundless_files: List[Path]) -> str:
        """Create final output MV from segments + audio"""
        # Build output path
        base = os.path.splitext(os.path.basename(audio_path))[0]
        dt = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(os.path.dirname(os.path.dirname(audio_path)), "final", "mp4ok")
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{base}_vdo_{dt}.mp4")

        # If AI soundless mode: concat AI clip + segments with external audio
        if ai_mode == "vdo_ai_soundless" and ai_soundless_files:
            # pick random AI clip, move to tmp_ai, normalize
            ai_clip_src = random.choice(ai_soundless_files)
            ai_soundless_files.remove(ai_clip_src)
            tmp_ai_dir = product_dir / "tmp_ai"
            tmp_ai_dir.mkdir(parents=True, exist_ok=True)
            moved = tmp_ai_dir / ai_clip_src.name
            try:
                shutil.move(str(ai_clip_src), str(moved))
            except Exception as e:
                self.log(f"[AI_SOUNDLESS] move failed: {e}")
                ai_clip_src = None

            if ai_clip_src:
                ai_norm = tmp_ai_dir / f"ai_soundless_{idx:03d}.mp4"
                if not self._normalize_ai_clip(moved, ai_norm):
                    ai_norm = None
            else:
                ai_norm = None

            if ai_norm and ai_norm.exists():
                # Build sequence [AI][seg0][seg1][AI]...
                video_inputs = []
                seg_since_ai = 0
                inserted_first_ai = False
                for seg in segments:
                    if not inserted_first_ai:
                        video_inputs.append(str(ai_norm))
                        inserted_first_ai = True
                        seg_since_ai = 0
                    video_inputs.append(seg.path)
                    seg_since_ai += 1
                    if seg_since_ai >= 2:
                        video_inputs.append(str(ai_norm))
                        seg_since_ai = 0
                n = len(video_inputs)
                cmd = [self.ffmpeg_cmd, "-hide_banner", "-loglevel", "warning", "-y"]
                for vp in video_inputs:
                    cmd += ["-i", vp]
                cmd += ["-i", audio_path]
                w, h = self.config.target_wh
                fc_parts = []
                for i in range(n):
                    fc_parts.append(
                        f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]"
                    )
                concat_inputs = "".join(f"[v{i}]" for i in range(n))
                fc_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[v]")
                filter_str = ";".join(fc_parts)
                cmd += [
                    "-filter_complex", filter_str,
                    "-map", "[v]",
                    "-map", f"{n}:a",
                    "-c:v", self.config.encoder,
                    "-b:v", self.config.bitrate,
                    "-pix_fmt", "yuv420p",
                    "-af", "aresample=async=1:48000",
                    "-shortest",
                    "-movflags", "+faststart",
                    out_path
                ]
                self.log(f"[AI_SOUNDLESS] concat ({n} clips) -> {os.path.basename(out_path)}")
                result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if result.returncode != 0:
                    self.log(f"[AI_SOUNDLESS] ffmpeg error: {result.stderr}")
                    raise RuntimeError(f"FFmpeg failed: {result.stderr}")
                return out_path

        # Normal / vdo_ai mode: use ffconcat (AI intro prepend once if enabled)
        list_txt = os.path.join(tmp_dir, f"list_{idx}.txt")
        concat_items: List[SegmentInfo] = []

        if ai_mode == "vdo_ai":
            ai_intro = self._build_ai_intro(product_dir, idx)
            if ai_intro and ai_intro.exists():
                try:
                    ai_dur = self.ffprobe_duration(str(ai_intro))
                except Exception:
                    ai_dur = 0
                if ai_dur > 0:
                    concat_items.append(SegmentInfo(path=str(ai_intro), duration=ai_dur, base_id="ai", lens_id="ai"))
                    self.log(f"[AI] prepend intro: {ai_intro.name} ({ai_dur:.1f}s)")
                else:
                    self.log(f"[AI] skip invalid AI clip: {ai_intro}")

        concat_items.extend(segments)

        with open(list_txt, "w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            for seg in concat_items:
                safe_path = seg.path.replace("\\", "/").replace(":", "\\:")
                f.write(f"file '{safe_path}'\n")
                f.write(f"inpoint 0\n")
                f.write(f"outpoint {seg.duration}\n")

        # Resolve overlay label/template + CSV rows (shared with AutoMV style)
        cfg = self.config.overlay_config or {}
        overlay_mode = cfg.get("overlay_mode", "single")
        title_rows = load_title_rows(os.path.dirname(audio_path))
        effective_mode = overlay_mode
        if len(title_rows) > 1 and overlay_mode == "single":
            effective_mode = "round_robin"
        selected_title = select_title_row(title_rows, idx, effective_mode) if title_rows else None

        tmpl = self.config.overlay_template or "{name}"
        label_text = tmpl.replace("{name}", base)
        label_text = label_text.replace("{index}", str(idx)).replace("{total}", str(total)).strip() or base
        if selected_title:
            lines = [ln for ln in [selected_title.line1, selected_title.line2, selected_title.line3] if ln and ln.strip()]
            if lines:
                label_text = "\\n".join(lines)

        # Build FFmpeg command
        vf = self._build_overlay_filter(label_text, audio_duration)

        cmd = [
            self.ffmpeg_cmd, "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", list_txt, "-i", audio_path,
            "-map", "0:v", "-map", "1:a",
            "-filter:v", vf,
            "-c:v", self.config.encoder,
            "-b:v", self.config.bitrate,
            "-g", "60", "-bf", "2",
            "-pix_fmt", "yuv420p",
            "-af", "aresample=async=1:48000",
            "-shortest",
            "-max_muxing_queue_size", "4096",
            "-movflags", "+faststart",
            out_path
        ]

        self.log(f"[ffmpeg] Creating {os.path.basename(out_path)}")
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")

        if result.returncode != 0:
            self.log(f"[ffmpeg] Error: {result.stderr}")
            raise RuntimeError(f"FFmpeg failed: {result.stderr}")

        # Cleanup list
        if os.path.exists(list_txt):
            os.remove(list_txt)

        self.log(f"[{idx}/{total}] Saved: {os.path.basename(out_path)}")
        return out_path

    def _build_overlay_filter(self, label: str, video_duration: float) -> str:
        """Build overlay filter for output (background, stroke, fade, CSV-aware)."""
        w, h = self.config.target_wh
        cfg = self.config.overlay_config or {}
        pos = self.config.text_pos
        x, y = self._get_pos_coords(pos)

        filters = [f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"]

        # Background box behind text
        if cfg.get("overlay_bg", False):
            alpha = max(0, min(cfg.get("overlay_bg_alpha", 70), 100)) / 100.0
            bg_hex = self._normalize_hex(cfg.get("overlay_bg_color", "#000000"))
            bg_expr = f"0x{bg_hex}{int(alpha * 255):02x}"
            box_h_ratio = 0.15
            if pos in ("Top Center", "Top Left", "Top Right"):
                box_y = "0"
            elif pos == "Center":
                box_y = f"(ih-{box_h_ratio}*ih)/2"
            else:
                box_y = f"ih-{box_h_ratio}*ih"
            filters.append(f"drawbox=x=0:y={box_y}:w=iw:h={box_h_ratio}*ih:color={bg_expr}:t=fill")

        # Enable/fade expressions
        dur = cfg.get("overlay_duration", 5)
        fade_dur = cfg.get("overlay_fade_dur", 0.5) if cfg.get("overlay_fade", False) else 0
        show_end = cfg.get("overlay_show_end", False)
        end_start = cfg.get("overlay_end_start", 3.0)
        if show_end and video_duration > end_start:
            enable_expr = f"between(t,0,{dur})+gte(t,{video_duration - end_start:.2f})"
        else:
            enable_expr = f"between(t,0,{dur})"
        alpha_expr = f"if(lt(t,{fade_dur}),t/{fade_dur},1)" if fade_dur > 0 else None

        # Stroke
        stroke_part = ""
        if cfg.get("overlay_stroke", False):
            stroke_width = cfg.get("overlay_stroke_width", 2)
            stroke_hex = self._normalize_hex(cfg.get("overlay_stroke_color", "#000000"))
            stroke_part = f":borderw={stroke_width}:bordercolor=0x{stroke_hex}"

        # Colors and font
        font_color = cfg.get("line_color1", "#FFFFFF")
        font_path = f":fontfile='{self.config.fontfile}'" if self.config.fontfile else ""
        alpha_part = f":alpha='{alpha_expr}'" if alpha_expr else ""

        # Escape label (support for \\n to break lines)
        safe_label = label.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("\n", "\\n")

        filters.append(
            f"drawtext=text='{safe_label}':x={x}:y={y}:fontsize={self.config.text_size}"
            f":fontcolor={font_color}{font_path}{stroke_part}{alpha_part}"
            f":enable='{enable_expr}'"
        )

        filters += ["fps=30", "format=yuv420p"]
        return ",".join(filters)

    def _cleanup_tmp(self, tmp_dir: str):
        """Clean up temporary directory"""
        if os.path.isdir(tmp_dir):
            try:
                shutil.rmtree(tmp_dir)
                self.log("[Cleanup] Removed tmp/ directory")
            except Exception as e:
                self.log(f"[Cleanup] Warning: {e}")
        # Also clean tmp_ai (AI intros)
        try:
            tmp_ai_dir = Path(tmp_dir).parent / "tmp_ai"
            if tmp_ai_dir.exists():
                shutil.rmtree(tmp_ai_dir)
                self.log("[Cleanup] Removed tmp_ai/ directory")
        except Exception as e:
            self.log(f"[Cleanup] Warning (tmp_ai): {e}")

    def _detect_ai_mode(self, product_dir: Path) -> Tuple[str, List[Path]]:
        """Detect AI mode and collect soundless files."""
        if not self.config.ai_enabled:
            return "none", []
        ai_dir = product_dir / "vdo_ai"
        ai_soundless_dir = product_dir / "vdo_ai_soundless"
        ai_files = list(ai_dir.glob("**/*.*")) if ai_dir.exists() else []
        soundless_files = self._collect_ai_soundless_files(ai_soundless_dir)
        if ai_files:
            return "vdo_ai", soundless_files
        elif soundless_files:
            return "vdo_ai_soundless", soundless_files
        return "none", []

    # ==================== Utility Functions ====================

    def create_missing_folders(self, product_path: str):
        """
        Create standard folder structure for vdo_long processing.

        Creates:
            product/vdo_long/
                long_001/ to long_005/
                footage_001/ to footage_005/
            product/audio/
            product/vdo_ai/
            product/vdo_ai_soundless/
        """
        vdo_long_dir = os.path.join(product_path, "vdo_long")
        os.makedirs(vdo_long_dir, exist_ok=True)

        created = []

        # Create long_001 to long_005
        for i in range(1, 6):
            folder = os.path.join(vdo_long_dir, f"long_{i:03d}")
            if not os.path.exists(folder):
                os.makedirs(folder)
                created.append(folder)

        # Create footage_001 to footage_005
        for i in range(1, 6):
            folder = os.path.join(vdo_long_dir, f"footage_{i:03d}")
            if not os.path.exists(folder):
                os.makedirs(folder)
                created.append(folder)

        # Create audio folder
        audio_dir = os.path.join(product_path, "audio")
        if not os.path.exists(audio_dir):
            os.makedirs(audio_dir)
            created.append(audio_dir)

        # Create vdo_ai folders
        vdo_ai_dir = os.path.join(product_path, "vdo_ai")
        if not os.path.exists(vdo_ai_dir):
            os.makedirs(vdo_ai_dir)
            created.append(vdo_ai_dir)

        vdo_ai_sl_dir = os.path.join(product_path, "vdo_ai_soundless")
        if not os.path.exists(vdo_ai_sl_dir):
            os.makedirs(vdo_ai_sl_dir)
            created.append(vdo_ai_sl_dir)

        self.log(f"[Setup] Created {len(created)} folders")
        return created

