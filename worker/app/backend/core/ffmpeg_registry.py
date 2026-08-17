"""
core/ffmpeg_registry.py — Global registry of live ffmpeg handles.

Why this exists:
    On Windows the user closes the main window while a render is in
    progress. Tkinter calls WM_DELETE_WINDOW which quits the mainloop,
    but the daemon Worker thread is a busy ffmpeg subprocess — it does
    NOT get cancelled automatically. We need:

      1. A way to track every ffmpeg process that the app has spawned
         (so we can kill them all on shutdown).
      2. A way for a single FfmpegRunner (or a raw subprocess.Popen
         spawned by reframe stage) to register itself on creation and
         unregister when its subprocess exits (success, error, or
         cancel) so the list stays clean.

Both concerns are handled by a module-level "active runnings" set wrapped
behind a tiny public API:

    from core.ffmpeg_registry import register, unregister, cancel_all,
                                       active_count

    # Inside FfmpegRunner.__init__ → register(self)
    # Inside reframe._encode_one (Popen) → register(proc)
    # On subprocess exit (any path) → unregister
    # From main_window._on_close → cancel_all()

V1.0.0.10: registry now accepts either an FfmpegRunner-like object
(has .cancel()) OR a raw subprocess.Popen (has .terminate()). cancel_all()
dispatches by duck-typing so the reframe stage (which still uses
subprocess.Popen directly for parallelism) is also reachable from
window-close / SIGINT.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from threading import RLock
from typing import Set, Union

# Lazy-import FfmpegRunner to avoid circular import: this module is a
# registry; FfmpegRunner imports nothing back from us.  The annotation
# is fine even without the import.
if sys.version_info >= (3, 11):
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from .ffmpeg_runner import FfmpegRunner
else:
    FfmpegRunner = None  # type: ignore[assignment]


# Public type alias: anything with a stop method counts as a live handle.
Cancellable = Union["FfmpegRunner", subprocess.Popen]


_lock = RLock()
_active: Set[Cancellable] = set()


def register(handle: Cancellable) -> None:
    """Track a live ffmpeg handle. Call when subprocess is starting.

    Accepts either an FfmpegRunner (chromakey stage) or a raw
    subprocess.Popen (reframe stage).
    """
    with _lock:
        _active.add(handle)


def unregister(handle: Cancellable) -> None:
    """Stop tracking — call when subprocess has exited (success/error/cancel)."""
    with _lock:
        _active.discard(handle)


def _cancel_one(handle: Cancellable) -> None:
    """Request cancellation for an FfmpegRunner or raw Popen handle."""
    if hasattr(handle, "cancel") and callable(getattr(handle, "cancel")):
        handle.cancel()
        return
    if isinstance(handle, subprocess.Popen):
        try:
            if handle.poll() is None:
                handle.terminate()
        except Exception:
            try:
                handle.kill()
            except Exception:
                pass
        return
    raise TypeError(f"unsupported handle type for cancel: {type(handle).__name__}")


def cancel_all(grace_sec: float = 0.0) -> int:
    """Cancel every registered FfmpegRunner. Returns # cancellation requests sent.

    Called by the main window's WM_DELETE_WINDOW handler. Best-effort:
    cancel() is a no-op if the runner already exited, and we tolerate
    all subprocesses that may have already died.

    FIX (B-02, 2026-07-31): Do NOT remove the runner from the active set here.
    The runner's subprocess is still alive after cancel() returns (terminate()
    only signals; it does not wait). If we discard the runner now, the
    subsequent ``active_count()`` call returns 0 even though the OS still has
    the ffmpeg subprocess alive, which breaks ``_on_close_window``'s "any
    ffmpeg still running?" confirmation guard. The runner unregisters itself
    from ``FfmpegRunner.run`` when the subprocess actually exits (see
    ``core/ffmpeg_runner.py`` which calls ``_unreg(self)`` after
    ``self._process.wait()`` returns).  Callers that need the second-wave
    force-kill must pass a positive ``grace_sec`` explicitly; the zero default
    preserves the truthful active-runner state for ordinary cancellation.
    """
    with _lock:
        snapshot = list(_active)
    cancelled = 0
    for handle in snapshot:
        try:
            _cancel_one(handle)
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            try:
                logging.getLogger(__name__).warning(
                    "cancel_all: failed to cancel %r: %s", handle, exc,
                )
            except Exception:
                pass
    # Second wave: SIGKILL any survivors after grace_sec.
    if grace_sec > 0:
        import time as _time
        _time.sleep(grace_sec)
        for handle in snapshot:
            try:
                if _is_alive(handle):
                    _force_kill_one(handle)
            except Exception as exc:  # noqa: BLE001
                try:
                    logging.getLogger(__name__).warning(
                        "cancel_all: failed to force_kill %r: %s", handle, exc,
                    )
                except Exception:
                    pass
    return cancelled


def _is_alive(handle: Cancellable) -> bool:
    """Return True if the handle's subprocess is still running."""
    if hasattr(handle, "is_running") and callable(getattr(handle, "is_running")):
        try:
            return bool(handle.is_running())
        except Exception:
            return False
    if isinstance(handle, subprocess.Popen):
        try:
            return handle.poll() is None
        except Exception:
            return False
    return False


def _force_kill_one(handle: Cancellable) -> None:
    """Force-kill a single handle. Duck-types FfmpegRunner vs Popen."""
    if hasattr(handle, "force_kill") and callable(getattr(handle, "force_kill")):
        handle.force_kill()
        return
    if isinstance(handle, subprocess.Popen):
        try:
            handle.kill()
        except Exception:
            pass
        return
    raise TypeError(f"unsupported handle type for force_kill: {type(handle).__name__}")


def active_count() -> int:
    """Return the count of currently registered handles."""
    with _lock:
        return len(_active)
