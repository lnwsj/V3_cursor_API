"""
core/_legacy/ — Archived V2-era modules.

These modules were ported from the original AutoMv_A-main Flet project (now renamed SJ88 Green Screen, V3+) and
are NOT used by the V3 Tk UI (which talks to `core.green_render`,
`core.ai_reframe`, `core.batch_pingpong`, `core.gpu_detector`,
`core.ffmpeg_runner`, `core.app_config`, `core.preset_store`,
`core.render_checkpoint`).

They are kept here for two reasons:
  1. V2 PyInstaller bundle (`V2/.venv`) still imports some of them
     (`portable_tc_runner` is the CLI runner for the V2 portable EXE).
  2. As reference for porting any missing V2 logic into V3 in the future.

DO NOT add new imports from `_legacy/*` in V3 UI or pipeline code.
If you need a feature that lives here, port it into a proper
`core/<new_module>.py` and delete the legacy file.

Relative imports between sibling legacy modules still work because they
all live inside this `core._legacy` sub-package.
"""
from __future__ import annotations

# Re-export public symbols so `from core._legacy import X` still resolves
# the same way the old `from core import X` did.
from .auto_mv import AutoMV  # noqa: F401
from .video_editor import VideoEditor  # noqa: F401
from .vdo_long import VdoLongProcessor, VdoLongConfig, SegmentInfo  # noqa: F401
from .runlog import RunLogger, RunLog, RunResult, SegmentUsage  # noqa: F401
from .models import TitleRow, load_title_rows, select_title_row  # noqa: F401
from .utils import (  # noqa: F401
    has_ffmpeg,
    ffmpeg_supports_encoder,
    pick_h264_encoder,
    list_files_recursive,
    normalize_bitrate,
    escape_drawtext_text,
    escape_ff_path,
    get_app_data_path,
)
from .podcast_engine import run_podcast_edit  # noqa: F401