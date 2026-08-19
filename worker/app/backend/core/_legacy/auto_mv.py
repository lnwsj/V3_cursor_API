import os
import random
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Callable, Dict

from .models import TitleRow, load_title_rows, select_title_row
from .utils import escape_drawtext_text, escape_ff_path, pick_h264_encoder, ffmpeg_supports_encoder, write_ffconcat

class AutoMV:
    """Core video generation with MP3 + video clips"""
    def __init__(self, encoder: str, bitrate: str, loops: int,
                 text_enabled: bool, text_pos: str, text_size: int,
                 overlay_template: str, fontfile: Optional[str], target_wh: Tuple[int, int],
                 overlay_config: Dict,
                 logger: Callable[[str], None], ffmpeg_cmd: str = "ffmpeg", ffprobe_cmd: str = "ffprobe"):
        self.encoder = encoder
        self.bitrate = bitrate
        self.loops = loops
        self.text_enabled = text_enabled
        self.text_pos = text_pos
        self.text_size = text_size
        self.overlay_template = overlay_template or "{name}"
        self.fontfile = fontfile
        self.target_wh = target_wh
        self.overlay_config = overlay_config
        self.durations: Dict[str, float] = {}
        self.log = logger
        self.ffmpeg_cmd = ffmpeg_cmd
        self.ffprobe_cmd = ffprobe_cmd

    def clear_cache(self):
        self.durations.clear()

    def ffprobe_duration(self, p: str) -> float:
        if p in self.durations:
            return self.durations[p]
        cmd = [self.ffprobe_cmd, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", p]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                          text=True, encoding="utf-8", errors="replace").strip()
        except Exception as e:
            raise RuntimeError(f"ffprobe error: {e}") from e
        d = float(out)
        self.durations[p] = d
        return d

    def _choose_encoder(self) -> Tuple[str, List[str]]:
        pref = self.encoder if self.encoder in ("h264_nvenc", "h264_qsv", "libx264", "av1_nvenc") else None
        if pref and ffmpeg_supports_encoder(pref, self.ffmpeg_cmd):
            if pref in ("h264_nvenc", "av1_nvenc"):
                return pref, ["-preset", "p6"]
            if pref == "h264_qsv":
                return pref, ["-preset", "slow"]
            return pref, ["-preset", "slow"]
        return pick_h264_encoder(None, self.ffmpeg_cmd)

    def _get_pos_coords(self, pos: str, box_h_ratio: float = 0.15) -> tuple:
        """Get x,y coordinates based on position"""
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

    def _normalize_hex(self, color: str) -> str:
        """Normalize hex color to 6 digits"""
        c = color.strip()
        if c.startswith("#"):
            c = c[1:]
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return c if len(c) == 6 else "000000"

    def _build_overlay_filters(self, label: str, video_duration: float = 30.0) -> str:
        """Build V20-style overlay filter chain with bg, stroke, fade, show_at_end"""
        if not self.text_enabled:
            return ""

        cfg = self.overlay_config
        pos = self.text_pos

        filters = []
        font_path_opt = ""
        if self.fontfile:
            ff = escape_ff_path(self.fontfile)
            font_path_opt = f":fontfile='{ff}'"

        # Background box
        if cfg.get('overlay_bg', False):
            alpha = max(0, min(cfg.get('overlay_bg_alpha', 70), 100)) / 100.0
            bg_hex = self._normalize_hex(cfg.get('overlay_bg_color', '#000000'))
            bg_expr = f"0x{bg_hex}{int(alpha * 255):02x}"
            box_h_ratio = 0.15

            # Position for box
            if pos in ("Top Center", "Top Left", "Top Right"):
                box_y = "0"
            elif pos == "Center":
                box_y = f"(ih-{box_h_ratio}*ih)/2"
            else:  # bottom
                box_y = f"ih-{box_h_ratio}*ih"

            filters.append(
                f"drawbox=x=0:y={box_y}:w=iw:h={box_h_ratio}*ih:color={bg_expr}:t=fill"
            )

        # Enable expression for fade and show_at_end
        dur = cfg.get('overlay_duration', 5)
        fade_dur = cfg.get('overlay_fade_dur', 0.5) if cfg.get('overlay_fade', False) else 0
        show_end = cfg.get('overlay_show_end', False)
        end_start = cfg.get('overlay_end_start', 3.0)

        if show_end and video_duration > end_start:
            enable_expr = f"between(t,0,{dur})+gte(t,{video_duration - end_start:.2f})"
        else:
            enable_expr = f"between(t,0,{dur})"

        # Alpha expression for fade
        if fade_dur > 0:
            alpha_expr = f"if(lt(t,{fade_dur}),t/{fade_dur},1)"
        else:
            alpha_expr = None

        # Stroke
        stroke_part = ""
        if cfg.get('overlay_stroke', False):
            stroke_width = cfg.get('overlay_stroke_width', 2)
            stroke_hex = self._normalize_hex(cfg.get('overlay_stroke_color', '#000000'))
            stroke_part = f":borderw={stroke_width}:bordercolor=0x{stroke_hex}"

        # Alpha part
        alpha_part = f":alpha='{alpha_expr}'" if alpha_expr else ""

        # Build drawtext
        safe_label = escape_drawtext_text(label)
        x, y = self._get_pos_coords(pos)

        filters.append(
            f"drawtext=text='{safe_label}':x={x}:y={y}:fontsize={self.text_size}"
            f":fontcolor=white{font_path_opt}{stroke_part}{alpha_part}"
            f":enable='{enable_expr}'"
        )

        return ",".join(filters) if filters else ""

    def build_multi_line_overlay_filters(self, title_row: TitleRow, video_duration: float = 30.0) -> str:
        """Build V20-style multi-line overlay filter (3 lines from CSV)"""
        cfg = self.overlay_config
        pos = self.text_pos

        # If no text at all, return empty
        if not title_row.line1.strip() and not title_row.line2.strip() and not title_row.line3.strip():
            return ""

        filters = []
        font_path_opt = ""
        if self.fontfile:
            ff = escape_ff_path(self.fontfile)
            font_path_opt = f":fontfile='{ff}'"

        # Get font sizes for 3 lines from overlay_config (passed from UI)
        font_sizes = [
            cfg.get('font_size1', 72),
            cfg.get('font_size2', 60),
            cfg.get('font_size3', 48),
        ]

        # Background box
        if cfg.get('overlay_bg', False):
            alpha = max(0, min(cfg.get('overlay_bg_alpha', 70), 100)) / 100.0
            bg_hex = self._normalize_hex(cfg.get('overlay_bg_color', '#000000'))
            bg_expr = f"0x{bg_hex}{int(alpha * 255):02x}"
            box_h_ratio = 0.24  # Slightly taller for 3 lines

            # Position for box
            if pos in ("Top Center", "Top Left", "Top Right"):
                box_y = "0"
            elif pos == "Center":
                box_y = f"(ih-{box_h_ratio}*ih)/2"
            else:  # bottom
                box_y = f"ih-{box_h_ratio}*ih"

            filters.append(
                f"drawbox=x=0:y={box_y}:w=iw:h={box_h_ratio}*ih:color={bg_expr}:t=fill"
            )

        # Enable expression for fade and show_at_end
        dur = cfg.get('overlay_duration', 5)
        show_end = cfg.get('overlay_show_end', False)
        end_start = cfg.get('overlay_end_start', 3.0)

        # Animation settings
        anim_type = cfg.get('anim_type', 'fade')
        anim_dur = cfg.get('anim_dur', 0.5)

        if show_end and video_duration > end_start:
            enable_expr = f"between(t,0,{dur})+gte(t,{video_duration - end_start:.2f})"
        else:
            enable_expr = f"between(t,0,{dur})"

        # Animation expressions based on type
        # Animation fades in over anim_dur seconds
        if anim_type == 'none':
            alpha_anim_expr = None
            y_anim_expr = None
            scale_anim_expr = None
        elif anim_type == 'fade' or anim_type == 'fade_scale':
            # Fade in animation
            alpha_anim_expr = f"if(lt(t,{anim_dur}),t/{anim_dur},1)"
            y_anim_expr = None
            scale_anim_expr = f"if(lt(t,{anim_dur}),0.5+0.5*(t/{anim_dur}),1)" if anim_type == 'fade_scale' else None
        elif anim_type == 'slide_up':
            # Slide up from bottom - y position animates from below
            alpha_anim_expr = f"if(lt(t,{anim_dur}),t/{anim_dur},1)"
            y_anim_expr = anim_dur
            scale_anim_expr = None
        else:
            alpha_anim_expr = None
            y_anim_expr = None
            scale_anim_expr = None

        # Stroke
        stroke_part = ""
        if cfg.get('overlay_stroke', False):
            stroke_width = cfg.get('overlay_stroke_width', 2)
            stroke_hex = self._normalize_hex(cfg.get('overlay_stroke_color', '#000000'))
            stroke_part = f":borderw={stroke_width}:bordercolor=0x{stroke_hex}"

        # Alpha part with animation
        alpha_part = f":alpha='{alpha_anim_expr}'" if alpha_anim_expr else ""

        # Build drawtext for each line
        lines = [title_row.line1, title_row.line2, title_row.line3]

        # Get line colors from config (default to white if not set)
        colors = [
            self._normalize_hex(cfg.get('line_color1', '#FFFFFF')),
            self._normalize_hex(cfg.get('line_color2', '#FFFFFF')),
            self._normalize_hex(cfg.get('line_color3', '#FFFFFF')),
        ]

        # Calculate y offsets based on position
        # For top: box_y + 20%, 50%, 80%
        # For center: center + offsets
        # For bottom: box_y + 80%, 50%, 20% (bottom up)
        if pos in ("Top Center", "Top Left", "Top Right"):
            y_offsets = ["h*0.1", "h*0.2", "h*0.3"]
            x_pos = "w/2-tw/2"
        elif pos == "Center":
            y_offsets = ["(h-80)/2-40", "(h-80)/2", "(h-80)/2+40"]
            x_pos = "w/2-tw/2"
        else:  # bottom positions
            y_offsets = ["h-h*0.24-20", "h-h*0.24-80", "h-h*0.24-140"]
            x_pos = "w/2-tw/2"

        for i, (line, size, y_off, color) in enumerate(zip(lines, font_sizes, y_offsets, colors)):
            if not line.strip():
                continue

            safe_label = escape_drawtext_text(line)

            # Handle slide_up animation - y position animates from below
            if y_anim_expr:
                # y starts below and slides up
                y_with_anim = f"if(lt(t,{y_anim_expr}),{y_off}+({y_anim_expr}*ih)*(1-t/{y_anim_expr}),{y_off})"
            else:
                y_with_anim = y_off

            filters.append(
                f"drawtext=text='{safe_label}':x={x_pos}:y={y_with_anim}:fontsize={size}"
                f":fontcolor=0x{color}{font_path_opt}{stroke_part}{alpha_part}"
                f":enable='{enable_expr}'"
            )

        return ",".join(filters) if filters else ""

    def build_logo_overlay_filter(self, logo_path: str, logo_config: Dict) -> str:
        """
        Build FFmpeg overlay filter for logo/watermark image.
        Uses img2filter for PNG logos with alpha channel support.
        """
        if not logo_path or not os.path.exists(logo_path):
            return ""

        if not logo_config.get('logo_enabled', False):
            return ""

        logo_pos = logo_config.get('logo_position', 'Top Right')
        logo_size_pct = logo_config.get('logo_size', 15)  # percentage of video width
        logo_alpha = logo_config.get('logo_alpha', 100) / 100.0

        # Get file extension for format detection
        ext = os.path.splitext(logo_path)[1].lower()

        # Calculate logo size relative to video
        # logo_size_pct = percentage of video width
        scale_expr = f"iw*{logo_size_pct}/100"

        # Calculate position based on selection
        pos_map = {
            'Top Right': "W-w-10:10",
            'Top Left': "10:10",
            'Bottom Right': "W-w-10:H-h-10",
            'Bottom Left': "10:H-h-10",
            'Center': "(W-w)/2:(H-h)/2",
        }
        x_y = pos_map.get(logo_pos, "W-w-10:10")

        # Build filter
        if ext == '.png':
            # PNG with alpha channel
            if logo_alpha >= 1.0:
                return f"overlay={x_y}"
            else:
                return f"overlay={x_y}:alpha={logo_alpha}"
        else:
            # Other formats (JPEG, BMP) - no alpha
            return f"scale={scale_expr}:-1:flags=lanczos,overlay={x_y}"

    def _drawtext_expr(self, label: str) -> Optional[str]:
        """Legacy single-line drawtext (backward compatible)"""
        if not self.text_enabled:
            return None
        pos = self.text_pos
        x, y = self._get_pos_coords(pos)
        safe_label = escape_drawtext_text(label)
        if self.fontfile:
            ff = escape_ff_path(self.fontfile)
            return f"drawtext=fontfile='{ff}':text='{safe_label}':fontcolor=white:fontsize={self.text_size}:x={x}:y={y}"
        else:
            return f"drawtext=font='Arial':text='{safe_label}':fontcolor=white:fontsize={self.text_size}:x={x}:y={y}"

    def _vf_chain(self, label: str, video_duration: float = 30.0, title_row: Optional[TitleRow] = None) -> str:
        w, h = self.target_wh
        parts = [f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"]

        # === Priority 1: Use CSV multi-line overlay if available ===
        if title_row and (title_row.line1.strip() or title_row.line2.strip() or title_row.line3.strip()):
            multi_overlay = self.build_multi_line_overlay_filters(title_row, video_duration)
            if multi_overlay:
                parts.append(multi_overlay)
        else:
            # === Priority 2: Use V20-style overlay filters if configured ===
            overlay = self._build_overlay_filters(label, video_duration)
            if overlay:
                parts.append(overlay)
            else:
                # Fallback to simple drawtext
                dt = self._drawtext_expr(label)
                if dt:
                    parts.append(dt)

        # === Priority 3: Add Logo/Watermark Overlay (if configured) ===
        logo_path = self.overlay_config.get('logo_path', '') if self.overlay_config else ''
        if logo_path:
            logo_filter = self.build_logo_overlay_filter(logo_path, self.overlay_config)
            if logo_filter:
                parts.append(logo_filter)

        parts += ["fps=30", "format=yuv420p"]
        return ",".join(parts)

    def make_one(self, mp3_path: str, video_files: List[str],
                 stop_check: Optional[Callable[[], bool]] = None,
                 track_index: int = 1, track_total: int = 1) -> Tuple[str, List[str]]:
        """
        Create one Auto MV from MP3 + video clips.
        Returns: (output_path, list_of_video_files_used)
        """
        base = os.path.splitext(os.path.basename(mp3_path))[0]
        dt = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmpl = self.overlay_template or "{name}"
        label_text = tmpl.replace("{name}", base)
        label_text = label_text.replace("{index}", str(track_index))
        label_text = label_text.replace("{total}", str(track_total))
        label_text = label_text.strip() or base

        # === V20 CSV Overlay Support ===
        mp3_dir = os.path.dirname(mp3_path)
        title_rows = load_title_rows(mp3_dir)

        # Get overlay mode from config (default: single)
        overlay_mode = self.overlay_config.get('overlay_mode', 'single') if self.overlay_config else 'single'

        # Smart auto-switch: if multiple rows but mode is single -> round_robin
        effective_mode = overlay_mode
        if len(title_rows) > 1 and overlay_mode == 'single':
            effective_mode = 'round_robin'

        # Select title row
        selected_title = select_title_row(title_rows, track_index, effective_mode)

        # Log info
        if title_rows:
            self.log(f"[OVERLAY] title.csv: {len(title_rows)} rows | Mode: {effective_mode} | Idx: {track_index}")
            if selected_title.line1:
                self.log(f"[OVERLAY] Selected: line1='{selected_title.line1}' line2='{selected_title.line2}' line3='{selected_title.line3}'")
        # === End CSV Overlay ===

        audio_len = self.ffprobe_duration(mp3_path)
        need = audio_len * max(1, self.loops)
        if not video_files:
            raise RuntimeError("No video files found.")
        acc = 0.0
        clips = []
        used_videos = []  # Track which videos were used
        pool = list(video_files)
        max_attempts = len(video_files) * 2
        attempts = 0
        while acc < need:
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user")
            if not pool:
                pool = list(video_files)
            if not pool:
                raise RuntimeError("No valid video files in pool.")
            clip = random.choice(pool)
            pool.remove(clip)
            used_videos.append(clip)  # Track this video for deletion later
            try:
                d = self.ffprobe_duration(clip)
                if d <= 0:
                    raise ValueError("Zero duration")
            except Exception as e:
                self.log(f"[AutoMV] Skip {os.path.basename(clip)}: {e}")
                attempts += 1
                if attempts > max_attempts and acc == 0:
                    raise RuntimeError("All video files are invalid or unreadable.")
                continue
            dur = min(d, need - acc)
            clips.append((clip, dur))
            acc += dur
            attempts = 0
        song_dir = os.path.dirname(mp3_path)
        final_dir = os.path.join(song_dir, "final")
        mp4ok_dir = os.path.join(final_dir, "mp4ok")
        os.makedirs(mp4ok_dir, exist_ok=True)
        list_txt = os.path.join(final_dir, f"{base}_list.txt")
        write_ffconcat(Path(list_txt), clips)
        vcodec, vflags = self._choose_encoder()
        vf = self._vf_chain(label_text, video_duration=need, title_row=selected_title)
        out_path = os.path.join(mp4ok_dir, f"{base}_{dt}_mv.mp4")
        cmd = [self.ffmpeg_cmd, "-hide_banner", "-loglevel", "warning", "-y",
               "-f", "concat", "-safe", "0", "-i", list_txt, "-i", mp3_path,
               "-map", "0:v", "-map", "1:a", "-filter:v", vf,
               "-c:v", vcodec, "-b:v", self.bitrate, *vflags, "-g", "60", "-bf", "2", "-pix_fmt", "yuv420p",
               "-af", "aresample=async=1", "-ar", "48000", "-ac", "2",
               "-shortest", "-max_muxing_queue_size", "4096", "-movflags", "+faststart", out_path]
        self.log(f"[ffmpeg] {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
            if result.returncode != 0:
                err_msg = result.stderr.strip() if result.stderr else f"Exit code: {result.returncode}"
                self.log(f"[ffmpeg stderr] {err_msg}")
                raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
        finally:
            if os.path.exists(list_txt):
                try:
                    os.remove(list_txt)
                except:
                    pass
        # Return output path AND list of videos used
        return out_path, used_videos


