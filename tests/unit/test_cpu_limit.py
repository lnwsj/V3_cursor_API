"""Unit tests for core/cpu_limit.py - thread budget calculation."""
import os
import sys
import pytest
from unittest.mock import patch

WORKER = os.path.join(os.path.dirname(__file__), "..", "..", "worker", "app", "backend")
sys.path.insert(0, WORKER)


def test_cpu_percent_default_50():
    import core.cpu_limit as cl
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cl, "_cpu_percent", 50)
        assert cl.cpu_percent() == 50


def test_effective_ffmpeg_threads_single_worker():
    import core.cpu_limit as cl
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cl, "_cpu_percent", 100)
        mp.setattr(cl, "cpu_count", lambda: 12)
        assert cl.effective_ffmpeg_threads(1) == 12


def test_effective_ffmpeg_threads_3_parallel():
    import core.cpu_limit as cl
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cl, "_cpu_percent", 100)
        mp.setattr(cl, "cpu_count", lambda: 12)
        assert cl.effective_ffmpeg_threads(3) == 4


def test_effective_ffmpeg_threads_floor_1():
    """Even 0 budget returns at least 1 thread per worker."""
    import core.cpu_limit as cl
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cl, "_cpu_percent", 0)
        mp.setattr(cl, "cpu_count", lambda: 0)
        assert cl.effective_ffmpeg_threads(1) == 1
        assert cl.effective_ffmpeg_threads(10) == 1


def test_effective_ffmpeg_threads_clamps_low_budget():
    """Very small budget rounds up to 1 thread per worker."""
    import core.cpu_limit as cl
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cl, "_cpu_percent", 5)
        mp.setattr(cl, "cpu_count", lambda: 4)
        assert cl.effective_ffmpeg_threads(1) == 1
        assert cl.effective_ffmpeg_threads(5) == 1


def test_total_budget_never_exceeds_cpu_limit():
    """Sum of threads * parallel should never exceed cpu_count * cpu_percent."""
    import core.cpu_limit as cl
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cl, "_cpu_percent", 50)
        mp.setattr(cl, "cpu_count", lambda: 8)
        budget = 8 * 50 // 100  # = 4
        for parallel in (1, 2, 4, 8):
            threads = cl.effective_ffmpeg_threads(parallel)
            # Either total stays <= budget, or threads is 1 (minimum)
            assert threads * parallel <= budget or threads == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
