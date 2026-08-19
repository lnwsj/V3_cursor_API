"""
core/runlog.py — Stub (Run history for vdo_long).

สร้าง stub นี้เพื่อแก้ pre-existing import error ใน vdo_long.py
ที่ import SegmentUsage แต่ไฟล์นี้หายไป — ทำให้ core/__init__.py import ล้มเหลว
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class SegmentUsage:
    """บันทึกการใช้ segment ในการ render"""
    path: str
    lens_id: str = "unknown"
    duration: float = 0.0
    audio_file: Optional[str] = None


@dataclass
class RunResult:
    """ผลลัพธ์ของการ render 1 audio"""
    audio: str
    output: Optional[str] = None
    success: bool = False
    error: str = ""
    segments_used: List[SegmentUsage] = field(default_factory=list)


@dataclass
class RunLog:
    """log รวมของ 1 run"""
    config: Dict[str, Any] = field(default_factory=dict)
    mode: int = 1
    results: List[RunResult] = field(default_factory=list)
    started_at: float = 0.0
    ended_at: float = 0.0


class RunLogger:
    """Stub RunLogger — no-op (no actual logging)"""
    def __init__(self, *args, **kwargs):
        self.log: List[RunLog] = []

    def start_run(self, config: Dict[str, Any], mode: int) -> None:
        self.log.append(RunLog(config=config, mode=mode, started_at=__import__('time').time()))

    def log_result(self, audio: str, output: Optional[str], success: bool, error: str = "",
                   segments_used: Optional[List[SegmentUsage]] = None) -> None:
        if not self.log:
            return
        self.log[-1].results.append(RunResult(
            audio=audio, output=output, success=success, error=error,
            segments_used=segments_used or [],
        ))

    def end_run(self) -> None:
        if self.log:
            self.log[-1].ended_at = __import__('time').time()
