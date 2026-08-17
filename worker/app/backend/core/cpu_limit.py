"""
core/cpu_limit.py — user-facing CPU-usage limiter.

FIX (2026-07-02): ให้ผู้ใช้เลือกว่าจะให้ ffmpeg ใช้ CPU กี่ %
(ค่าเริ่มต้น 50%) → map เป็น `-threads N` (N = round(cpu_count × % / 100)).

ตัวอย่าง: เครื่อง 16 cores, ตั้ง 50% → threads = 8 ต่อ process ffmpeg.
ค่า persist ที่ ~/.green_pc/cpu_percent.txt (เหมือน theme.txt) ข้ามการรัน.
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT = 50
_MIN, _MAX = 5, 100

_cpu_percent: int = _DEFAULT


def _state_file() -> Path:
    return Path.home() / ".green_pc" / "cpu_percent.txt"


def set_cpu_percent(pct: int, persist: bool = True) -> int:
    """ตั้งค่า CPU% (clamp 5–100) — apply ทันทีและ persist ถ้า persist=True."""
    global _cpu_percent
    try:
        v = int(pct)
    except Exception:
        v = _DEFAULT
    v = max(_MIN, min(_MAX, v))
    _cpu_percent = v
    if persist:
        try:
            _state_file().parent.mkdir(parents=True, exist_ok=True)
            _state_file().write_text(str(v))
        except Exception:
            pass
    return v


def load_cpu_percent() -> int:
    """โหลดค่าจาก ~/.green_pc/cpu_percent.txt (เรียกตอน startup)."""
    global _cpu_percent
    try:
        v = int(_state_file().read_text(encoding="utf-8").strip())
        _cpu_percent = max(_MIN, min(_MAX, v))
    except Exception:
        _cpu_percent = _DEFAULT
    return _cpu_percent


def cpu_percent() -> int:
    return _cpu_percent


def cpu_count() -> int:
    try:
        return os.cpu_count() or 4
    except Exception:
        return 4


def effective_ffmpeg_threads(max_parallel: int = 1) -> int:
    """จำนวน thread ต่อ process ffmpeg = total_budget // max_parallel (≥1).

    total_budget = round(cpu_count × % / 100) คืองบ CPU ทั้งหมดที่ยอมให้ใช้
    แล้วหารให้ process ขนานตาม `max_parallel` (reframe TC02/05 รันขนานได้)
    → ผลรวม thread ทุก worker จะไม่เกิน budget (ไม่ oversubscribe).
    ตัวอย่าง: 12 cores, 50% → budget=6; max_parallel=3 → 2 thread/process (รวม 6).
    """
    budget = max(1, round(cpu_count() * cpu_percent() / 100.0))
    return max(1, budget // max(1, int(max_parallel)))
