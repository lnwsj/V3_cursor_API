"""core/path_utils.py — Cross-platform helpers for opening paths in the OS shell.

Centralizes the small but security-sensitive code paths that spawn the system
file explorer / default app. The previous call sites used a f-string to build
a `explorer "<path>"` command which is fragile (a path containing `"` could
break the shell quoting). All callers must go through `safe_open_in_explorer`
or `safe_open_with_default_app` instead.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


def portable_basename(path: str) -> str:
    """Return a basename for either POSIX or Windows-style input paths."""
    text = str(path or "").replace("\\", "/")
    return text.rsplit("/", 1)[-1]


def portable_stem(path: str) -> str:
    """Return a stem for either POSIX or Windows-style input paths."""
    return Path(portable_basename(path)).stem


def _validate_path(path: str) -> Optional[str]:
    """Return the absolute path if it exists on disk, otherwise None.

    Also rejects paths that contain a NUL byte (which can never legitimately
    appear in a Windows / POSIX path and is a classic injection marker).
    """
    if not path:
        return None
    if "\x00" in path:
        return None
    try:
        # ``abspath`` retains an 8.3 Windows spelling when ``%TEMP%`` is
        # exposed that way (for example ``C:\\Users\\SJ8888~1``).  Resolve the
        # existing path instead so every caller receives one canonical,
        # absolute spelling that is safe to pass as a single argv item.
        return str(Path(path).resolve(strict=True))
    except (ValueError, OSError, RuntimeError):
        return None


def safe_open_in_explorer(path: str) -> bool:
    """Open a file or folder in the platform's file explorer.

    - Windows: opens the folder and selects the file (if a file is given),
      or just opens the folder.
    - macOS:   `open <path>` (the Finder will show the folder).
    - Linux:   `xdg-open <path>`.

    Returns True if the helper accepted the request (the OS shell is async —
    we cannot guarantee the window opened).
    """
    abs_path = _validate_path(path)
    if abs_path is None:
        return False

    try:
        if sys.platform.startswith("win"):
            # os.startfile opens the file with its default app; for directories
            # it opens the folder. We want folder-view behaviour so we use
            # explorer.exe with a normalized path (no shell quoting).
            if os.path.isdir(abs_path):
                subprocess.Popen(
                    ["explorer.exe", os.path.normpath(abs_path)],
                    shell=False,
                    close_fds=True,
                )
            else:
                # /select, opens the parent folder with the file highlighted.
                subprocess.Popen(
                    ["explorer.exe", "/select,", os.path.normpath(abs_path)],
                    shell=False,
                    close_fds=True,
                )
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", abs_path],
                shell=False,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                ["xdg-open", abs_path],
                shell=False,
                close_fds=True,
            )
        return True
    except (OSError, FileNotFoundError):
        return False


def safe_open_with_default_app(path: str) -> bool:
    """Open a file with the OS-registered default application.

    Falls back to `safe_open_in_explorer` when no default app is available.
    """
    abs_path = _validate_path(path)
    if abs_path is None:
        return False

    try:
        if sys.platform.startswith("win"):
            os.startfile(abs_path)  # type: ignore[attr-defined]
            return True
        return safe_open_in_explorer(abs_path)
    except (OSError, FileNotFoundError, AttributeError):
        return False
