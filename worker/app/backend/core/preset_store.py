"""
core/preset_store.py — Save/load SettingsPanel presets.

FIX (2026-06-22): Preset save/load
- เก็บ preset ใน ~/.green_pc/presets/*.json
- แต่ละ preset = ค่า settings ทั้งหมดของ tab (จาก SettingsPanel.get_values())
- ชื่อ preset ใช้ได้ทั้ง tab (e.g. "TC01 green-screen HD")
- ใช้เป็น "render config" ที่ใช้ซ้ำได้

API:
    save_preset(tab_key, name, values) -> str (path)
    load_preset(name) -> dict | None
    list_presets(tab_key=None) -> List[PresetInfo]
    delete_preset(name) -> bool
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


PRESETS_MAX = 50


def presets_max() -> int:
    """Return the effective preset cap, overridden by config.json.

    Module-level `PRESETS_MAX` is the hard-coded default.
    """
    try:
        from core.app_config import load_app_config
        return load_app_config().presets_max_count
    except Exception:
        return PRESETS_MAX


def _presets_dir() -> Path:
    """ที่เก็บ preset (user home, ไม่ใช่ project dir)"""
    home = Path.home()
    green_pc_dir = home / ".green_pc" / "presets"
    green_pc_dir.mkdir(parents=True, exist_ok=True)
    return green_pc_dir


def _preset_path(name: str) -> Optional[Path]:
    """แปลง name → file path (sanitize filename)"""
    if not name or not isinstance(name, str):
        return None
    # sanitize: only allow alphanumeric + underscore + dash + space
    safe = re.sub(r"[^\w\- ]", "_", name.strip())
    if not safe:
        return None
    safe = safe[:80]  # max 80 chars
    return _presets_dir() / f"{safe}.json"


@dataclass
class PresetInfo:
    """Metadata ของ preset สำหรับแสดงใน dropdown"""
    name: str
    tab: str
    created_at: str
    path: Path

    @property
    def display(self) -> str:
        return f"{self.name}  [{self.tab}]"


def _read_preset(path: Path) -> Optional[Dict[str, Any]]:
    """อ่าน preset file (silent on error)"""
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "values" in data:
            return data
    except Exception:
        pass
    return None


def save_preset(
    tab_key: str,
    name: str,
    values: Dict[str, Dict[str, Any]],
) -> Optional[Path]:
    """
    Save preset ลง disk

    Args:
        tab_key: "TC01" / "TC02" / etc.
        name: ชื่อ preset (sanitize ให้ปลอดภัย)
        values: dict ของ {panel_key: {field: value}}

    Returns:
        path ของ preset file (หรือ None ถ้า fail)
    """
    path = _preset_path(name)
    if path is None:
        return None
    data = {
        "name": name.strip(),
        "tab": tab_key,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "values": values,
    }
    try:
        # atomic write
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        _trim_old_presets()
        return path
    except Exception:
        return None


def load_preset(name: str) -> Optional[Dict[str, Any]]:
    """โหลด preset (dict of values) — return None ถ้าไม่เจอ"""
    path = _preset_path(name)
    if path is None:
        return None
    return _read_preset(path)


def list_presets(tab_key: Optional[str] = None) -> List[PresetInfo]:
    """
    list preset files, เรียงตาม created_at desc
    filter by tab_key ถ้าระบุ
    """
    out: List[PresetInfo] = []
    d = _presets_dir()
    if not d.is_dir():
        return out
    for p in d.glob("*.json"):
        data = _read_preset(p)
        if not data:
            continue
        info = PresetInfo(
            name=data.get("name", p.stem),
            tab=data.get("tab", "?"),
            created_at=data.get("created_at", ""),
            path=p,
        )
        if tab_key is None or info.tab == tab_key:
            out.append(info)
    # sort by created_at desc (newest first)
    out.sort(key=lambda x: x.created_at, reverse=True)
    return out


def delete_preset(name: str) -> bool:
    """ลบ preset file — return True ถ้าลบสำเร็จ"""
    path = _preset_path(name)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except Exception:
        return False


def _trim_old_presets() -> None:
    """Trim oldest presets beyond the effective cap (config-aware)."""
    d = _presets_dir()
    if not d.is_dir():
        return
    cap = presets_max()
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[cap:]:
        try:
            old.unlink()
        except Exception:
            pass
