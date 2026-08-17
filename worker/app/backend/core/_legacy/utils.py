import os
import sys
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List


def fwd(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")


def escape_ff_path(p: str) -> str:
    return p.replace("\\", "/").replace(":", "\\:")


def get_resource_path(relative_path: str) -> str:
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def get_app_data_path(relative_path: str = "") -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative_path if relative_path else base


def has_ffmpeg(cmd: str = "ffmpeg") -> bool:
    try:
        subprocess.run([cmd, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def ffmpeg_supports_encoder(enc: str, cmd: str = "ffmpeg") -> bool:
    try:
        subprocess.run(
            [cmd, "-hide_banner", "-loglevel", "error", "-h", f"encoder={enc}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except Exception:
        return False


def pick_h264_encoder(preferred: Optional[str] = None, ffmpeg_cmd: str = "ffmpeg") -> Tuple[str, List[str]]:
    order = ([preferred] if preferred else []) + ["h264_nvenc", "h264_qsv", "libx264"]
    seen = set()
    for enc in order:
        if enc in seen:
            continue
        seen.add(enc)
        if ffmpeg_supports_encoder(enc, ffmpeg_cmd):
            if enc in ("h264_nvenc", "av1_nvenc"):
                return enc, ["-preset", "p6"]
            if enc == "h264_qsv":
                return enc, ["-preset", "slow"]
            if enc == preferred:
                return enc, ["-preset", "slow"]
            return "libx264", ["-preset", "slow"]
    return "libx264", ["-preset", "slow"]


def list_files_recursive(root: Path, exts: Tuple[str, ...]) -> List[str]:
    return [str(p) for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts] if root and root.exists() else []


def escape_ffconcat_path(p: str) -> str:
    return fwd(p).replace("\\", "/").replace("'", "'\\''")


def escape_drawtext_text(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    text = text.replace("%", "\\%")
    return text


def normalize_bitrate(val: str, default: str = "8000k") -> str:
    s = (val or "").strip().lower().replace(" ", "")
    if not s:
        return default
    suffix = s[-1] if s[-1].isalpha() else "k"
    num_part = s[:-1] if suffix in ("k", "m") else s
    try:
        n = int(float(num_part))
        if n <= 0:
            raise ValueError
    except Exception:
        return default
    return f"{n}{suffix}"


def write_ffconcat(list_path: Path, clips: List[Tuple[str, float]]):
    list_path.parent.mkdir(parents=True, exist_ok=True)
    with list_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("ffconcat version 1.0\n")
        for p, d in clips:
            safe = escape_ffconcat_path(p)
            f.write(f"file '{safe}'\n")
            f.write("inpoint 0\n")
            f.write(f"outpoint {d}\n")
