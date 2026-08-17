"""
core/app_exit.py — Clean shutdown handler for the GUI.

Bridges tkinter's WM_DELETE_WINDOW + Python's atexit + Windows SIGINT into
a single "kill all running ffmpeg + Worker threads + close root" routine.

Why this exists:
    - Tk's mainloop exits when the user closes the window, but the daemon
      Worker threads holding ffmpeg subprocesses are out of Tk's control.
    - If we just call `root.destroy()`, the OS-level ffmpeg.exe processes
      keep running (visible in Task Manager as leftover `%CPU`).
    - This module gives the MainWindow a one-liner hook to ensure that
      nothing leaks past shutdown.
"""
from __future__ import annotations

import atexit
import logging
import signal
import sys
import threading
from typing import Any, Iterable, List, Optional

from .ffmpeg_registry import cancel_all
from .usage_stats import stop_default_dispatcher


logger = logging.getLogger(__name__)


class AppShutdown:
    """Co-ordinates clean shutdown of all live Worker threads + ffmpeg procs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._extra_workers: List[Any] = []
        self._atexit_registered = False

    def register_worker(self, worker_thread: Any) -> None:
        """Track a Worker thread so shutdown can join it (best-effort)."""
        with self._lock:
            self._extra_workers.append(worker_thread)

    def unregister_worker(self, worker_thread: Any) -> None:
        with self._lock:
            try:
                self._extra_workers.remove(worker_thread)
            except ValueError:
                pass

    def install_signal_handlers(self) -> None:
        """Wire SIGINT/SIGTERM (Windows console Ctrl-C / Task Manager close)
        into the same shutdown path."""
        for sig in (getattr(signal, "SIGINT", None),
                    getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # signal handler may already be installed by another module,
                # or unavailable on Windows for SIGTERM under python.exe
                pass

    def install_atexit(self) -> None:
        """Final safety net: guarantee cancel_all() runs even on hard exit."""
        if self._atexit_registered:
            return
        atexit.register(self._on_atexit)
        self._atexit_registered = True

    def shutdown(self, workers: Optional[Iterable[Any]] = None,
                 *, force: bool = True, reason: str = "ui_close") -> int:
        """Cancel Worker owners and every registered ffmpeg process once.

        The force parameter remains for compatibility. Shutdown itself is
        idempotent: later WM, signal, or atexit calls return immediately.
        """
        with self._lock:
            if self._cancelled:
                return 0
            self._cancelled = True
            registered_workers = list(self._extra_workers)

        all_workers = registered_workers
        if workers:
            all_workers.extend(workers)

        # Preserve order while avoiding duplicate cancellation.
        unique_workers: List[Any] = []
        seen_ids = set()
        for worker in all_workers:
            if worker is None or id(worker) in seen_ids:
                continue
            seen_ids.add(id(worker))
            unique_workers.append(worker)

        for worker in unique_workers:
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    logger.exception(
                        "AppShutdown[%s]: worker cancel failed", reason
                    )

        try:
            # Window/application shutdown is the explicit hard-stop boundary:
            # request termination first, then force-kill survivors after the
            # bounded grace period.  Ordinary cancel_all() callers keep the
            # non-blocking default so active_count() stays truthful until exit.
            cancelled = cancel_all(grace_sec=2.0)
        except Exception:
            cancelled = 0
            logger.exception("AppShutdown[%s]: ffmpeg cancel_all failed", reason)

        # Give Worker wrappers a short, bounded chance to observe stop flags.
        for worker in unique_workers:
            try:
                wait = getattr(worker, "wait", None)
                if callable(wait):
                    wait(timeout=0.25)
                    continue
                join = getattr(worker, "join", None)
                is_alive = getattr(worker, "is_alive", None)
                if callable(join) and (
                    not callable(is_alive) or is_alive()
                ):
                    join(timeout=0.25)
            except Exception:
                pass

        # Tk may be destroyed before a queued normal on_done callback runs.
        # Give each accepted job one exactly-once chance to close its
        # telemetry lifecycle after the bounded Worker wait.
        for worker in unique_workers:
            try:
                finalize = getattr(worker, "finalize_for_shutdown", None)
                if callable(finalize):
                    finalize()
            except Exception:
                pass

        # Persist queued lifecycle events for a bounded interval. Remote
        # delivery is independent and must never delay application exit.
        try:
            stop_default_dispatcher(timeout=0.5)
        except Exception:
            pass

        logger.info(
            "AppShutdown[%s]: workers=%d cancelled_ffmpeg=%d",
            reason,
            len(unique_workers),
            cancelled,
        )
        return cancelled
    def _on_signal(self, signum, frame) -> None:
        try:
            sys.stderr.write(
                f"\n[app_exit] received signal {signum}, killing ffmpeg\n"
            )
            sys.stderr.flush()
        except Exception:
            pass
        self.shutdown(reason=f"signal_{signum}")
        # Python runs signal handlers on the main thread. Raising SystemExit
        # unwinds Tk.mainloop after cleanup, so the latched shutdown owner can
        # never coexist with a still-running UI that accepts new jobs.
        raise SystemExit(128 + int(signum))

    def _on_atexit(self) -> None:
        # Last-chance full shutdown; ignore failures (interpreter may be dying).
        try:
            self.shutdown(reason="atexit")
        except Exception:
            pass


# Module-level singleton — most apps only need one of these.
_default = AppShutdown()


def install_default(workers: Optional[Iterable[Any]] = None) -> AppShutdown:
    """Install the default shutdown handler (signal + atexit + cancel_all)."""
    _default.install_signal_handlers()
    _default.install_atexit()
    if workers:
        for w in workers:
            _default.register_worker(w)
    return _default


def get_default() -> AppShutdown:
    return _default


def shutdown(*args, **kwargs) -> int:
    return _default.shutdown(*args, **kwargs)
