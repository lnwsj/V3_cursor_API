"""Unit tests for core/media_probe.py - tri-state truth."""
import os
import sys
import pytest

WORKER = os.path.join(os.path.dirname(__file__), "..", "..", "worker", "app", "backend")
sys.path.insert(0, WORKER)

def test_media_stream_state_present():
    from core.media_probe import MediaStreamState
    assert MediaStreamState.PRESENT.value == "present"

def test_media_stream_state_absent():
    from core.media_probe import MediaStreamState
    assert MediaStreamState.ABSENT.value == "absent"

def test_media_stream_state_error():
    from core.media_probe import MediaStreamState
    assert MediaStreamState.ERROR.value == "error"

def test_despill_parameters_green():
    """#00FF00 (green) should give type=0 with mix=0.32."""
    from core.green_render import _despill_parameters
    type_int, mix = _despill_parameters("#00FF00", 0.32)
    assert type_int == 0
    assert abs(mix - 0.32) < 1e-6

def test_despill_parameters_blue():
    from core.green_render import _despill_parameters
    type_int, _ = _despill_parameters("#0000FF", 0.5)
    assert type_int == 1

def test_despill_parameters_red_falls_back_to_green():
    """#FF0000 (red) is not in blue support - falls back to green."""
    from core.green_render import _despill_parameters
    type_int, _ = _despill_parameters("#FF0000", 0.5)
    assert type_int == 0

def test_despill_parameters_invalid_color():
    from core.green_render import _despill_parameters
    type_int, _ = _despill_parameters("not_a_color", 0.5)
    assert type_int == 0

def test_canonical_status_normalization():
    # _canonical_status is in worker/app/backend/main.py
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "worker", "app", "backend"))
    from main import _canonical_status
    assert _canonical_status("success") == "succeeded"
    assert _canonical_status("succeeded") == "succeeded"
    assert _canonical_status("completed") == "succeeded"
    assert _canonical_status("done") == "succeeded"
    assert _canonical_status("failed") == "failed"
    assert _canonical_status("FAILED") == "failed"
    assert _canonical_status("cancelled") == "cancelled"
    assert _canonical_status("canceled") == "cancelled"
    assert _canonical_status("invalid_input") == "invalid_input"
    assert _canonical_status("invalid-input") == "invalid_input"

def test_canonical_status_passthrough():
    """Unknown statuses pass through unchanged (lowercase)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "worker", "app", "backend"))
    from main import _canonical_status
    assert _canonical_status("unknown") == "unknown"
    assert _canonical_status("queued") == "queued"
    assert _canonical_status("running") == "running"

def test_probe_video_codec_missing_file():
    from core.media_probe import probe_video_codec
    assert probe_video_codec("ffprobe", "/nonexistent/path.mp4") == ""

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
