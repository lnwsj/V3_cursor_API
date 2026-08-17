"""
FFmpegRunner — subprocess wrapper with progress parsing + watchdog.

Pattern ported from:
  - green.sj88ai.com/services/render_service.py::stream_command_events (line 78-141)
  - greenlnw.cutdee.com/core/media_ffmpeg.py::build_ffmpeg_cmd

ความสามารถ:
  - รัน ffmpeg subprocess + stream stderr
  - parse progress จาก `-progress pipe:1` หรือ `out_time_ms=` key=value
  - watchdog: kill ffmpeg ถ้า:
      * wall-clock duration > max_factor × video_duration (default 3.0)
      * ไม่มี output เกิน idle_timeout (default 120s)
  - cancel: หยุด subprocess ได้ทันที
  - callback: log, progress, done, error events
"""
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional


# FIX (2026-07-02): flag ปิดหน้าต่าง console ดำตอน spawn ffmpeg/ffprobe.
# สำคัญมากบน PyInstaller build ที่ console=False — ถ้าไม่ใส่ ทุกครั้งที่ render
# (โดยเฉพาะตอนรัน ffmpeg ขนาน max_parallel=3) จะมีหน้าต่าง cmd ดำขึ้นมาวาบๆ กระพริบ.
# ใช้กับทุก subprocess.run/check_output/Popen ที่เรียก ffmpeg/ffprobe.
NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# Defaults mirror green.sj88ai.com/services/render_service.py
DEFAULT_IDLE_TIMEOUT_SEC = 120
DEFAULT_MAX_FACTOR = 3.0  # wall-clock / video_duration

# V1.0.0.4: per-TC watchdog factor overrides. TC04 reframe+batch is the
# longest pipeline (21 reframe + 21 chroma per source). The global
# max_wall_factor=5.0 (set in V1.0.0.3) is enough for normal sources,
# but a 5min input under CPU throttling can still spike above it, so
# TC04 defaults to 10.0× and any other TC can be added by callers.
DEFAULT_TC_FACTOR_OVERRIDES: dict[str, float] = {
    "TC04": 10.0,
}


@dataclass
class FfmpegProgress:
    """Snapshot ของ progress ปัจจุบัน"""
    pct: float = 0.0         # 0..100
    out_time_us: int = 0     # microseconds ของ output video
    speed: str = ""          # e.g. "1.23x"
    fps: float = 0.0
    bitrate: str = ""
    total_size: int = 0
    elapsed_sec: float = 0.0


@dataclass
class FfmpegResult:
    success: bool = False
    returncode: int = -1
    output_path: str = ""
    error: str = ""
    duration_sec: float = 0.0
    cancelled: bool = False


def _format_seconds(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{int(m)}m{s:.0f}s"
    h, m = divmod(m, 60)
    return f"{int(h)}h{int(m)}m"


class FfmpegRunner:
    """
    รัน ffmpeg command พร้อม progress tracking และ watchdog

    Usage:
        runner = FfmpegRunner(ffmpeg_cmd="ffmpeg", idle_timeout_sec=120, max_factor=3.0)
        result = runner.run(
            cmd=["ffmpeg", "-y", "-i", "input.mp4", "output.mp4"],
            expected_duration_sec=30.0,
            on_log=lambda msg: print(msg),
            on_progress=lambda p: print(f"{p.pct:.1f}%"),
            stop_check=lambda: False,  # return True เพื่อ cancel
        )
    """

    def __init__(
        self,
        ffmpeg_cmd: str = "ffmpeg",
        idle_timeout_sec: float = DEFAULT_IDLE_TIMEOUT_SEC,
        max_factor: float = DEFAULT_MAX_FACTOR,
        tc_factor_overrides: Optional[dict[str, float]] = None,
    ):
        self.ffmpeg_cmd = ffmpeg_cmd
        self.idle_timeout_sec = idle_timeout_sec
        self.max_factor = max_factor
        # V1.0.0.4: merge caller-provided per-TC overrides on top of the
        # built-in defaults. Caller wins.
        merged = dict(DEFAULT_TC_FACTOR_OVERRIDES)
        if tc_factor_overrides:
            merged.update(tc_factor_overrides)
        self.tc_factor_overrides: dict[str, float] = merged
        self._process: Optional[subprocess.Popen] = None
        self._process_lock = threading.RLock()
        self._cancel_event = threading.Event()

    def _terminate_current(self, *, force: bool = False) -> bool:
        """Terminate the current subprocess without changing cancellation intent."""
        with self._process_lock:
            proc = self._process
        if proc is None or proc.poll() is not None:
            return False
        try:
            if force:
                proc.kill()
            elif os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            return True
        except Exception:
            return False

    def cancel(self) -> bool:
        """Request cancellation and terminate an in-flight process (thread-safe)."""
        self._cancel_event.set()
        return self._terminate_current(force=False)

    def force_kill(self) -> None:
        """Hard-kill the subprocess (SIGKILL equivalent). Used on app shutdown."""
        self._cancel_event.set()
        self._terminate_current(force=True)

    def is_running(self) -> bool:
        with self._process_lock:
            proc = self._process
        return proc is not None and proc.poll() is None

    def _compute_max_wall_sec(
        self,
        expected_duration_sec: float,
        tc_label: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> float:
        """
        V1.0.0.4: pick the effective wall-clock budget for the watchdog.

        - per-TC factor override wins over the runner default when
          `tc_label` is set and present in `self.tc_factor_overrides`
        - always at least `self.idle_timeout_sec`
        - when `expected_duration_sec == 0` the budget is 3600s
          (legacy behaviour: no per-render time limit, only idle)
        """
        effective_factor = self.tc_factor_overrides.get(tc_label, self.max_factor)
        if expected_duration_sec > 0:
            return max(
                self.idle_timeout_sec,
                expected_duration_sec * effective_factor,
            )
        return 3600.0

    def run(
        self,
        cmd: List[str],
        expected_duration_sec: float = 0.0,
        on_log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[FfmpegProgress], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        extra_progress_args: bool = True,
        tc_label: str = "",
    ) -> FfmpegResult:
        """
        รัน ffmpeg command

        Args:
            cmd: ffmpeg command (เป็น list)
            expected_duration_sec: ความยาววิดีโอ output ที่คาดไว้ (ใช้คำนวณ progress %)
            on_log: callback รับ log message
            on_progress: callback รับ FfmpegProgress
            stop_check: callback ตรวจว่า user ขอหยุด (return True เพื่อ cancel)
            extra_progress_args: ถ้า True จะแทรก `-progress pipe:1 -nostats` เข้า cmd อัตโนมัติ
            tc_label: V1.0.0.4 — TC tag (e.g. "TC04") used to look up a
                per-TC max_factor override in `self.tc_factor_overrides`.
                When set and present in the override map, that factor
                replaces `self.max_factor` for the wall-clock budget.
                When empty, `self.max_factor` is used as-is.

        Returns:
            FfmpegResult
        """
        # Preserve a cancellation request that raced ahead of process creation.
        if self._cancel_event.is_set():
            self._cancel_event.clear()
            return FfmpegResult(
                success=False,
                error="cancelled before ffmpeg spawn",
                cancelled=True,
            )

        if not cmd or cmd[0] != self.ffmpeg_cmd:
            cmd = [self.ffmpeg_cmd] + list(cmd)

        if extra_progress_args and "-progress" not in cmd:
            # Insert after -y / -hide_banner / -loglevel.
            insert_idx = 0
            for i, c in enumerate(cmd):
                if c in ("-y", "-hide_banner", "-loglevel"):
                    insert_idx = i + 2 if i + 1 < len(cmd) and cmd[i + 1] in (
                        "warning", "error", "info", "quiet", "panic", "fatal", "verbose", "debug"
                    ) else i + 1
            cmd = cmd[:insert_idx] + ["-progress", "pipe:1", "-nostats"] + cmd[insert_idx:]

        log = on_log or (lambda m: None)
        progress_cb = on_progress or (lambda p: None)

        uses_progress_pipe = "-progress" in cmd

        log(f"[ffmpeg] {self._summarize_cmd(cmd)}")

        # V1.0.0.4: pick the effective wall-clock factor. Per-TC override
        # wins over the runner default when tc_label is set.
        max_wall_sec = self._compute_max_wall_sec(expected_duration_sec, tc_label, log)
        if tc_label and self.tc_factor_overrides.get(tc_label, self.max_factor) != self.max_factor:
            log(f"[ffmpeg] watchdog factor override: {tc_label} "
                f"{self.max_factor} -> {self.tc_factor_overrides[tc_label]}")
        start_time = time.time()
        last_output_time = start_time

        with self._process_lock:
            self._process = None
        cancelled = False
        error_msg = ""

        # Check again immediately before Popen. cancel() may race with run().
        if self._cancel_event.is_set():
            self._cancel_event.clear()
            return FfmpegResult(
                success=False,
                error="cancelled before ffmpeg spawn",
                cancelled=True,
            )

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            with self._process_lock:
                self._process = process
        except FileNotFoundError:
            self._cancel_event.clear()
            return FfmpegResult(success=False, error=f"ffmpeg not found: {self.ffmpeg_cmd}")
        except Exception as e:
            self._cancel_event.clear()
            return FfmpegResult(success=False, error=f"Popen failed: {e}")

        # Close the race where cancel() arrived between the final check and
        # publishing the new process under the lock.
        if self._cancel_event.is_set():
            self._terminate_current(force=False)

        # V1.0.0.9: register this runner so shutdown can cancel any in-flight
        # ffmpeg alongside the Worker thread that wraps it. Unregister when
        # the process exits below (success / cancel / error).
        try:
            from .ffmpeg_registry import register as _reg, unregister as _unreg
            _reg(self)
        except Exception:
            _reg = _unreg = None  # type: ignore[assignment]  # noqa

        # Drain stderr on a background thread so we keep error lines for the result tail.
        stderr_lines: List[str] = []
        stderr_lock = threading.Lock()

        def drain_stderr():
            for line in iter(self._process.stderr.readline, ""):
                with stderr_lock:
                    stderr_lines.append(line)
                line_clean = line.rstrip()
                if line_clean:
                    # In progress mode, stdout carries key=value progress and
                    # normal ffmpeg stderr can be very noisy. Keep real error
                    # lines, but suppress routine Input/Stream/frame chatter.
                    lower = line_clean.lower()
                    is_error_line = "error" in lower or "failed" in lower
                    if (not uses_progress_pipe) or is_error_line:
                        log(f"[ffmpeg] {line_clean}")

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        # Poll cancellation and watchdogs independently from stdout. A silent
        # subprocess can otherwise block the stdout iterator indefinitely.
        monitor_done = threading.Event()
        monitor_errors: List[str] = []

        def monitor_control() -> None:
            while not monitor_done.wait(0.05):
                if self._cancel_event.is_set():
                    self._terminate_current(force=False)
                    return

                try:
                    stop_requested = bool(stop_check and stop_check())
                except Exception:
                    stop_requested = False
                if stop_requested:
                    log("[ffmpeg] cancellation requested by caller")
                    self.cancel()
                    return

                now = time.time()
                if now - last_output_time > self.idle_timeout_sec:
                    # FIX (B-23, 2026-07-31): clarify the message — this is
                    # an idle-timeout, NOT the wall-clock cap. The wall-clock
                    # cap is checked below. Operators sometimes assume the
                    # two are the same and mis-interpret the logs.
                    message = (
                        f"ffmpeg idle timeout reached "
                        f"(no stdout for {self.idle_timeout_sec:.0f}s; "
                        f"wall-clock cap would have been {max_wall_sec:.0f}s) - killed"
                    )
                    monitor_errors.append(message)
                    log(f"[ffmpeg] watchdog (idle): {message}")
                    self._terminate_current(force=False)
                    return
                if now - start_time > max_wall_sec:
                    message = f"ffmpeg wall-clock cap exceeded {max_wall_sec:.0f}s - killed"
                    monitor_errors.append(message)
                    log(f"[ffmpeg] watchdog (wall-clock): {message}")
                    self._terminate_current(force=False)
                    return

        monitor_thread = threading.Thread(target=monitor_control, daemon=True)
        monitor_thread.start()

        # Read the progress stream (stdout) line-by-line.
        prog = FfmpegProgress()
        last_progress_time = start_time
        last_progress_cb_time = start_time  # FIX (2026-06-22): throttle progress callback (กัน UI ค้าง)

        def _update_pct_from_out_time() -> None:
            if expected_duration_sec > 0:
                prog.pct = min(
                    100.0,
                    (prog.out_time_us / 1_000_000.0) / expected_duration_sec * 100.0,
                )

        try:
            for raw in self._process.stdout:
                line = raw.strip()
                if not line:
                    continue

                # parse key=value
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if key == "out_time_us":
                        try:
                            prog.out_time_us = int(val) if val.lstrip("-").isdigit() else 0
                        except Exception:
                            prog.out_time_us = 0
                        _update_pct_from_out_time()
                    elif key == "out_time_ms":
                        try:
                            # FFmpeg's -progress field name is historical:
                            # current builds emit microseconds here (same unit
                            # as out_time_us), not milliseconds.
                            prog.out_time_us = int(val) if val.lstrip("-").isdigit() else 0
                        except Exception:
                            prog.out_time_us = 0
                        _update_pct_from_out_time()
                    elif key == "speed":
                        prog.speed = val
                    elif key == "fps":
                        try:
                            prog.fps = float(val) if val.replace(".", "").replace("-", "").isdigit() else 0.0
                        except Exception:
                            prog.fps = 0.0
                    elif key == "bitrate":
                        prog.bitrate = val
                    elif key == "total_size":
                        try:
                            prog.total_size = int(val) if val.lstrip("-").isdigit() else 0
                        except Exception:
                            prog.total_size = 0
                    elif key == "progress":
                        # progress=continue / progress=end
                        if val == "end":
                            prog.pct = 100.0
                        last_progress_time = time.time()
                        last_output_time = time.time()
                        prog.elapsed_sec = time.time() - start_time
                        # FIX (2026-06-22): throttle the progress callback to once per 0.25s.
                        # Problem: ffmpeg emits `progress=continue` 50-100 times/sec,
                        # and each progress_cb posts root.after(0, ...) which floods
                        # the Tk event queue — the UI then appears to freeze right
                        # after the user clicks Render.
                        now = time.time()
                        if val == "end" or (now - last_progress_cb_time) >= 0.25:
                            last_progress_cb_time = now
                            try:
                                progress_cb(prog)
                            except Exception:
                                pass

        except Exception as e:
            error_msg = f"stdout read error: {e}"
            log(f"[ffmpeg] {error_msg}")

        monitor_done.set()
        monitor_thread.join(timeout=1)
        if monitor_errors:
            error_msg = monitor_errors[0]
        cancelled = cancelled or self._cancel_event.is_set()
        if cancelled and not error_msg:
            error_msg = "cancelled"

        # wait process
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        except Exception:
            pass

        stderr_thread.join(timeout=2)

        for stream_name in ("stdout", "stderr"):
            try:
                stream = getattr(self._process, stream_name, None)
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:
                pass

        returncode = self._process.returncode
        with stderr_lock:
            tail_err = "".join(stderr_lines[-10:]).strip() if stderr_lines else ""

        success = returncode == 0 and not cancelled and not error_msg
        result = FfmpegResult(
            success=success,
            returncode=returncode if returncode is not None else -1,
            error="" if success else (error_msg or tail_err or ""),
            duration_sec=time.time() - start_time,
            cancelled=cancelled,
        )

        if not result.success and not error_msg and returncode != 0:
            log(f"[ffmpeg] ❌ exit {returncode}: {tail_err[:300]}")

        with self._process_lock:
            self._process = None
        self._cancel_event.clear()
        # V1.0.0.9: unregister when this ffmpeg subprocess exits, so the
        # shutdown handler doesn't try to cancel an already-finished runner.
        if _unreg is not None:
            try:
                _unreg(self)
            except Exception:
                pass
        try:
            from .encoder_recovery import record_encoder_attempt

            record_encoder_attempt(cmd, result)
        except Exception:
            # Evidence telemetry must never change the render verdict.
            pass
        return result

    def _summarize_cmd(self, cmd: List[str]) -> str:
        """สรุป command ยาวๆ ให้อ่านง่าย"""
        # Convert None → "<none>" for safe join
        safe = [str(c) if c is not None else "<none>" for c in cmd]
        s = " ".join(safe)
        if len(s) > 240:
            s = s[:240] + "…"
        return s
