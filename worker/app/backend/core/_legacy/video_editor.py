import os
import subprocess
from datetime import datetime
from typing import Optional, Tuple, List, Callable, Dict

from .utils import escape_drawtext_text, escape_ff_path, pick_h264_encoder, ffmpeg_supports_encoder

class VideoEditor:
    """Process video files using their original audio (no MP3 needed)"""
    def __init__(self, encoder: str, bitrate: str,
                 text_enabled: bool, text_pos: str, text_size: int,
                 overlay_template: str, fontfile: Optional[str], target_wh: Tuple[int, int],
                 audio_config: Dict, video_config: Dict,
                 logger: Callable[[str], None], ffmpeg_cmd: str = "ffmpeg", ffprobe_cmd: str = "ffprobe"):
        self.encoder = encoder
        self.bitrate = bitrate
        self.text_enabled = text_enabled
        self.text_pos = text_pos
        self.text_size = text_size
        self.overlay_template = overlay_template or "{name}"
        self.fontfile = fontfile
        self.target_wh = target_wh
        self.audio_config = audio_config
        self.video_config = video_config
        self.log = logger
        self.ffmpeg_cmd = ffmpeg_cmd
        self.ffprobe_cmd = ffprobe_cmd

    def _build_audio_filter_chain(self) -> str:
        """Build FFmpeg audio filter chain from config"""
        cfg = self.audio_config
        filters = []

        # High Pass Filter (remove low rumble)
        if cfg.get('audio_highpass', 0) > 0:
            filters.append(f"highpass=f={cfg['audio_highpass']}")

        # Low Pass Filter (remove hiss)
        if cfg.get('audio_lowpass', 0) > 0:
            filters.append(f"lowpass=f={cfg['audio_lowpass']}")

        # Noise Reduction (afftdn)
        if cfg.get('audio_noise_reduction', False):
            filters.append("afftdn=nf=-25")

        # De-esser (deesser)
        if cfg.get('audio_deesser', False):
            filters.append("deesser")

        # Compressor (acompressor)
        if cfg.get('audio_compressor', False):
            filters.append("acompressor=threshold=-20:ratio=4")

        # Echo Removal (aecho)
        if cfg.get('audio_echo_remove', False):
            filters.append("aecho=0.8:0.9:40:0.3")

        # Silence Removal (silenceremove)
        if cfg.get('audio_silence_remove', False):
            filters.append("silenceremove=stop_periods=-1:stop_duration=1:stop_threshold=-50dB")

        # Volume Boost
        if cfg.get('audio_volume', 0) > 0:
            filters.append(f"volume={cfg['audio_volume']}dB")

        # Audio Normalize (loudnorm) - always last before fade
        if cfg.get('audio_normalize', True):
            filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")

        # Fade In/Out
        if cfg.get('audio_fade', False):
            fade_in = cfg.get('audio_fade_in', 0)
            fade_out = cfg.get('audio_fade_out', 0)
            if fade_in > 0:
                filters.append(f"afade=t=in:st=0:d={fade_in}")
            if fade_out > 0:
                # Need duration for fade out, will be handled per-file
                pass  # Handled dynamically

        return ",".join(filters) if filters else ""

    def _build_video_filter_chain(self) -> str:
        """Build FFmpeg video filter chain from config"""
        cfg = self.video_config
        filters = []

        # Brightness (eq=brightness)
        if cfg.get('video_brightness', 0) != 50:
            brightness_val = (cfg['video_brightness'] - 50) / 50.0  # Convert 0-100 to -1 to 1
            filters.append(f"eq=brightness={brightness_val:.2f}")

        # Contrast (eq=contrast)
        if cfg.get('video_contrast', 0) != 50:
            contrast_val = cfg['video_contrast'] / 50.0  # Convert 0-100 to 0-2
            filters.append(f"eq=contrast={contrast_val:.2f}")

        # Saturation (eq=saturation)
        if cfg.get('video_saturation', 0) != 50:
            saturation_val = cfg['video_saturation'] / 50.0  # Convert 0-100 to 0-2
            filters.append(f"eq=saturation={saturation_val:.2f}")

        # Grayscale
        if cfg.get('video_grayscale', False):
            filters.append("format=gray")

        # Deinterlace (yadif)
        if cfg.get('video_deinterlace', False):
            filters.append("yadif")

        # Rotate 90° clockwise (transpose=1)
        if cfg.get('video_rotate', False):
            filters.append("transpose=1")

        # Flip Horizontal (hflip)
        if cfg.get('video_flip_h', False):
            filters.append("hflip")

        # Flip Vertical (vflip)
        if cfg.get('video_flip_v', False):
            filters.append("vflip")

        return ",".join(filters) if filters else ""

    def _choose_encoder(self) -> Tuple[str, List[str]]:
        pref = self.encoder if self.encoder in ("h264_nvenc", "h264_qsv", "libx264", "av1_nvenc") else None
        if pref and ffmpeg_supports_encoder(pref, self.ffmpeg_cmd):
            if pref in ("h264_nvenc", "av1_nvenc"):
                return pref, ["-preset", "p6"]
            if pref == "h264_qsv":
                return pref, ["-preset", "slow"]
            return pref, ["-preset", "slow"]
        return pick_h264_encoder(None, self.ffmpeg_cmd)

    def _drawtext_expr(self, label: str) -> Optional[str]:
        if not self.text_enabled:
            return None
        pos = self.text_pos
        x, y = {
            "Top Center": ("(w-text_w)/2", "30"),
            "Bottom Center": ("(w-text_w)/2", "h-text_h-30"),
            "Top Right": ("w-text_w-30", "30"),
            "Top Left": ("30", "30"),
            "Bottom Right": ("w-text_w-30", "h-text_h-30"),
            "Bottom Left": ("30", "h-text_h-30"),
        }.get(pos, ("(w-text_w)/2", "30"))
        safe_label = escape_drawtext_text(label)
        if self.fontfile:
            ff = escape_ff_path(self.fontfile)
            return f"drawtext=fontfile='{ff}':text='{safe_label}':fontcolor=white:fontsize={self.text_size}:x={x}:y={y}"
        else:
            return f"drawtext=font='Arial':text='{safe_label}':fontcolor=white:fontsize={self.text_size}:x={x}:y={y}"

    def _vf_chain(self, label: str) -> str:
        w, h = self.target_wh
        parts = [f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"]
        dt = self._drawtext_expr(label)
        if dt:
            parts.append(dt)
        parts += ["fps=30", "format=yuv420p"]
        return ",".join(parts)

    def process_video(self, video_path: str, output_dir: str,
                      stop_check: Optional[Callable[[], bool]] = None,
                      track_index: int = 1, track_total: int = 1) -> str:
        base = os.path.splitext(os.path.basename(video_path))[0]
        dt = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmpl = self.overlay_template or "{name}"
        label_text = tmpl.replace("{name}", base)
        label_text = label_text.replace("{index}", str(track_index))
        label_text = label_text.replace("{total}", str(track_total))
        label_text = label_text.strip() or base
        if stop_check and stop_check():
            raise InterruptedError("Stopped by user")

        # Get video duration for fade out calculation
        video_duration = self.ffprobe_duration(video_path)

        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{base}_{dt}_edit.mp4")
        vcodec, vflags = self._choose_encoder()
        vf = self._vf_chain(label_text)

        # Build video filter chain (prepend to existing)
        vf_custom = self._build_video_filter_chain()
        if vf_custom:
            vf = f"{vf_custom},{vf}"

        # Build audio filter chain
        af = self._build_audio_filter_chain()
        # Add fade out if enabled
        cfg = self.audio_config
        if cfg.get('audio_fade', False) and cfg.get('audio_fade_out', 0) > 0:
            fade_out = cfg['audio_fade_out']
            start_time = max(0, video_duration - fade_out)
            if af:
                af += f",afade=t=out:st={start_time}:d={fade_out}"
            else:
                af = f"afade=t=out:st={start_time}:d={fade_out}"

        cmd = [self.ffmpeg_cmd, "-hide_banner", "-loglevel", "warning", "-y",
               "-i", video_path,
               "-map", "0:v", "-map", "0:a?",
               "-filter:v", vf,
               "-c:v", vcodec, "-b:v", self.bitrate, *vflags, "-g", "60", "-bf", "2", "-pix_fmt", "yuv420p",
               "-max_muxing_queue_size", "4096", "-movflags", "+faststart", out_path]

        # Add audio filter if any
        if af:
            cmd.extend(["-filter:a", af])

        # Audio encoding
        if af:
            cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
        else:
            cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])

        self.log(f"[ffmpeg] {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            err_msg = result.stderr.strip() if result.stderr else f"Exit code: {result.returncode}"
            self.log(f"[ffmpeg stderr] {err_msg}")
            raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
        return out_path


