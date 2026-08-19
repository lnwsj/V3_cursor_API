"""Global V3 app config loader."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List


DEFAULT_CONFIG: dict[str, Any] = {
    # ===== Debug / logging =====
    "debug_mode_log": True,
    "debug_log_detail_level": "max",
    "debug_log_retention_days": 30,
    "debug_log_include": {
        "environment": True,
        "input_media": True,
        "settings": True,
        "gpu": True,
        "progress": True,
        "ffmpeg": True,
        "output_validation": True,
    },
    # ===== UI =====
    "ui": {
        "theme": "dark",                # "dark" | "light"
        "window": {
            "width": 1440,
            "height": 920,
            "min_width": 1040,
            "min_height": 720,
        },
        "config_hot_reload_ms": 5000,   # poll config.json mtime every N ms
        "recent_files_max": 10,
    },
    # ===== FFmpeg / encoder =====
    "ffmpeg": {
        "auto_probe": True,             # run 1-frame smoke test at startup
        "preferred_order": [
            "h264_nvenc", "av1_nvenc", "hevc_nvenc",
            "h264_qsv", "h264_amf", "libx264",
        ],
        "watchdog": {
            "idle_timeout_sec": 120,    # kill ffmpeg if no output > N sec
            "max_wall_factor": 5.0,     # kill if wall_clock > N * video_duration (V1.0.0.3: bumped 3.0->5.0 for CPU-throttled batch reframe)
        },
    },
    # ===== Render defaults =====
    "render": {
        "tc01": {"width": 1080, "height": 1920, "fps": 30, "bitrate": "6000k"},
        "tc02": {"width": 1080, "height": 1920, "bitrate": "8000k", "max_parallel": 3},
        "tc03": {"segment_duration_sec": 10.0, "max_parallel": 3},
        "tc04": {"segment_duration_sec": 10.0, "max_parallel": 3},
        "tc05": {"width": 1080, "height": 1920, "max_parallel": 3},
        "tc06": {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "bitrate": "6000k",
            "allow_clip_reuse": True,
        },
    },
    # ===== Presets =====
    "presets": {
        "max_count": 50,
        "directory": "~/.green_pc/presets",
    },
}


def root_dir() -> Path:
    # FIX (2026-07-02): frozen-aware root. When running as a PyInstaller exe,
    # resolve to the EXE's folder (e.g. dist/) so config.json is read next to
    # the app and stays user-editable on a portable machine. In dev, use the
    # project root (parent of this core/ module).
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _user_config_dir() -> Path:
    """Per-user writable config directory (always writable, even when
    the EXE lives in a read-only / admin-locked location like Program Files)."""
    home = Path.home()
    d = home / ".green_pc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    """Resolve the active config.json in this priority:

    1. `GREEN_PC_CONFIG_PATH` env var (explicit override for power users).
    2. Next-to-EXE `config.json` when running as a frozen app, so a portable
       folder (USB / Desktop / Documents) can carry its own config.
       Falls back to user-home if the EXE folder is not writable (e.g.
       `Program Files` on Windows, read-only mount on Unix).
    3. Project root `config.json` in dev.
    """
    override = os.environ.get("GREEN_PC_CONFIG_PATH", "").strip()
    if override:
        return Path(override)

    is_frozen = getattr(sys, "frozen", False)
    if is_frozen:
        # Two-tier: try next-to-EXE first (portable), but only if writable.
        exe_cfg = root_dir() / "config.json"
        if exe_cfg.is_file():
            return exe_cfg
        # No file next to the exe yet — try writing a default there to see
        # if the folder is writable. If yes, ship the bundled config there.
        try:
            exe_cfg.parent.mkdir(parents=True, exist_ok=True)
            test = exe_cfg.parent / ".green_pc_writable_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink()
            return exe_cfg
        except OSError:
            # Folder not writable (Program Files etc.) — fall back to user home.
            return _user_config_dir() / "config.json"

    # Dev mode
    return root_dir() / "config.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class AppConfig:
    path: Path
    values: dict[str, Any] = field(default_factory=dict)

    # ===== Debug / logging =====
    @property
    def debug_mode_log(self) -> bool:
        return bool(self.values.get("debug_mode_log", True))

    @property
    def debug_log_detail_level(self) -> str:
        return str(self.values.get("debug_log_detail_level", "max"))

    @property
    def debug_log_retention_days(self) -> int:
        try:
            return int(self.values.get("debug_log_retention_days", 30))
        except (TypeError, ValueError):
            return 30

    def debug_include(self, key: str) -> bool:
        include = self.values.get("debug_log_include", {})
        if not isinstance(include, dict):
            return True
        return bool(include.get(key, True))

    # ===== UI =====
    def _ui(self) -> dict[str, Any]:
        ui = self.values.get("ui", {})
        return ui if isinstance(ui, dict) else {}

    @property
    def theme(self) -> str:
        mode = str(self._ui().get("theme", "dark")).lower().strip()
        return mode if mode in ("dark", "light") else "dark"

    def window_size(self) -> tuple[int, int]:
        win = self._ui().get("window", {})
        if not isinstance(win, dict):
            return (1440, 920)
        try:
            return (int(win.get("width", 1440)), int(win.get("height", 920)))
        except (TypeError, ValueError):
            return (1440, 920)

    def window_min_size(self) -> tuple[int, int]:
        win = self._ui().get("window", {})
        if not isinstance(win, dict):
            return (1040, 720)
        try:
            return (int(win.get("min_width", 1040)), int(win.get("min_height", 720)))
        except (TypeError, ValueError):
            return (1040, 720)

    @property
    def config_hot_reload_ms(self) -> int:
        try:
            return max(500, int(self._ui().get("config_hot_reload_ms", 5000)))
        except (TypeError, ValueError):
            return 5000

    @property
    def recent_files_max(self) -> int:
        try:
            return max(1, int(self._ui().get("recent_files_max", RECENT_FILES_MAX)))
        except (TypeError, ValueError):
            return RECENT_FILES_MAX

    # ===== FFmpeg / encoder =====
    def _ffmpeg_cfg(self) -> dict[str, Any]:
        cfg = self.values.get("ffmpeg", {})
        return cfg if isinstance(cfg, dict) else {}

    @property
    def ffmpeg_auto_probe(self) -> bool:
        return bool(self._ffmpeg_cfg().get("auto_probe", True))

    def preferred_encoder_order(self) -> list[str]:
        order = self._ffmpeg_cfg().get("preferred_order")
        if not isinstance(order, list) or not order:
            return ["h264_nvenc", "av1_nvenc", "hevc_nvenc",
                    "h264_qsv", "h264_amf", "libx264"]
        return [str(e) for e in order if isinstance(e, str)]

    def watchdog_idle_timeout_sec(self) -> float:
        wd = self._ffmpeg_cfg().get("watchdog", {})
        if not isinstance(wd, dict):
            return 120.0
        try:
            return max(1.0, float(wd.get("idle_timeout_sec", 120.0)))
        except (TypeError, ValueError):
            return 120.0

    def watchdog_max_wall_factor(self) -> float:
        wd = self._ffmpeg_cfg().get("watchdog", {})
        if not isinstance(wd, dict):
            return 5.0
        try:
            return max(1.0, float(wd.get("max_wall_factor", 5.0)))
        except (TypeError, ValueError):
            return 5.0

    # ===== Render defaults =====
    def _render_cfg(self, tc: str) -> dict[str, Any]:
        rd = self.values.get("render", {})
        if not isinstance(rd, dict):
            return {}
        tc_cfg = rd.get(tc, {})
        return tc_cfg if isinstance(tc_cfg, dict) else {}

    def render_default(self, tc: str, key: str, fallback: Any) -> Any:
        """Look up a render-default key for a given TC with safe fallback."""
        return self._render_cfg(tc).get(key, fallback)

    # ===== Presets =====
    def _presets_cfg(self) -> dict[str, Any]:
        ps = self.values.get("presets", {})
        return ps if isinstance(ps, dict) else {}

    @property
    def presets_max_count(self) -> int:
        try:
            return max(1, int(self._presets_cfg().get("max_count", 50)))
        except (TypeError, ValueError):
            return 50


def _ensure_default_config_file(path: Path) -> None:
    """Seed `path` with DEFAULT_CONFIG if it does not yet exist.

    Used by onefile builds where the EXE may live in a read-only folder
    (Program Files, read-only USB mount). In that case `path` already points
    at `~/.green_pc/config.json` (see config_path) which is always writable.

    The exclusive-create mode is intentional: if another process or the user
    creates the file after the existence check, that file must win unchanged.
    """
    if path.is_file():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n"
            )
    except FileExistsError:
        # A concurrent creator/user file always wins; never rewrite it.
        pass
    except OSError:
        pass


def load_app_config() -> AppConfig:
    path = config_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    else:
        # Onefile/fresh-install: seed the user-home copy with defaults so
        # the user has a starting config to edit. No-op if path is read-only.
        _ensure_default_config_file(path)
    return AppConfig(path=path, values=_deep_merge(DEFAULT_CONFIG, data))


def debug_mode_log_enabled() -> bool:
    return load_app_config().debug_mode_log


# ====================================================================
# Recent Files (FIX 2026-06-22) — track files the user opened recently.
# ====================================================================
# Stored in ~/.green_pc/recent_files.json (user home, kept separate from project config).
# API:
#   add_recent_file(path)        — append + dedupe + trim to 10 + persist
#   load_recent_files()          — return List[str] (latest first)
#   clear_recent_files()         — empty + persist
# ====================================================================

RECENT_FILES_MAX = 10


def recent_files_max() -> int:
    """Return the effective recent-files cap, overridden by config.json.

    Module-level `RECENT_FILES_MAX` remains the hard-coded default for code
    paths that need it before the config loader is available.
    """
    try:
        cfg = load_app_config()
        return cfg.recent_files_max
    except Exception:
        return RECENT_FILES_MAX


def _recent_files_dir() -> Path:
    """ที่เก็บ recent files (user home, ไม่ใช่ project dir)"""
    home = Path.home()
    green_pc_dir = home / ".green_pc"
    green_pc_dir.mkdir(parents=True, exist_ok=True)
    return green_pc_dir


def _recent_files_path() -> Path:
    return _recent_files_dir() / "recent_files.json"


def load_recent_files() -> List[str]:
    """คืน list ของ recent file paths (ล่าสุดก่อน), กรอง path ที่ไม่มีอยู่แล้ว"""
    path = _recent_files_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        # filter entries that still exist on disk + dedupe (preserve order)
        seen = set()
        out: List[str] = []
        for item in data:
            if not isinstance(item, str):
                continue
            if item in seen:
                continue
            seen.add(item)
            if os.path.isfile(item):
                out.append(item)
        return out
    except Exception:
        return []


def save_recent_files(files: List[str]) -> None:
    """Save list ลง disk (atomic write ผ่าน tmp file)"""
    path = _recent_files_path()
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(files, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        pass


def add_recent_file(file_path: str) -> List[str]:
    """
    เพิ่ม path ลง recent list:
    - ถ้ามีอยู่แล้ว → ย้ายไปอันดับแรก
    - ถ้าใหม่ → prepend
    - trim ให้เหลือแค่ RECENT_FILES_MAX รายการ (or config override)
    - persist ทันที
    - คืน list ใหม่
    """
    if not file_path or not os.path.isfile(file_path):
        return load_recent_files()
    file_path = os.path.abspath(file_path)
    files = load_recent_files()
    # remove the old entry (if any) and prepend the new one
    files = [f for f in files if f != file_path]
    files.insert(0, file_path)
    # trim (config-aware)
    files = files[:recent_files_max()]
    save_recent_files(files)
    return files


def clear_recent_files() -> None:
    """ล้าง recent files (เซฟไฟล์ว่าง)"""
    save_recent_files([])
