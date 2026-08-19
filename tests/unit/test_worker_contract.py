from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from worker.app.backend import main as worker
from worker.app.backend.core.contract import pick_compositions, validate_reframe_parallel
from worker.app.backend.core.path_utils import portable_stem
from worker.app.backend.core.pipelines._common import StageResult, finalize_stage_result
from worker.app.backend.core.pipelines.tc01_chroma import _extract_progress_pct
from worker.app.backend.core.pipelines.tc04_rebatch import _skip_batch_result


def test_status_is_canonical():
    assert worker._canonical_status("SUCCEEDED") == "succeeded"
    assert worker._canonical_status("completed") == "succeeded"
    assert worker._canonical_status("CANCELED") == "cancelled"
    assert worker._canonical_status("INVALID-INPUT") == "invalid_input"


def test_reference_contract_limits_reframe_parallelism_and_handles_null_toggles():
    assert validate_reframe_parallel(3) == 3
    with pytest.raises(ValueError):
        validate_reframe_parallel(4)
    assert pick_compositions({"use_center": None}) == ["center", "left", "right"]


def test_windows_paths_have_portable_stems():
    assert portable_stem(r"C:\Users\foo\Bar-1.mp4") == "Bar-1"


def test_tc01_progress_accepts_mapping_payloads():
    assert _extract_progress_pct({"pct": 42}) == 42
    assert _extract_progress_pct({"percent": 55}) == 55


def test_tc04_skip_surfaces_valid_reframe_outputs(tmp_path):
    output = tmp_path / "reframe.mp4"
    output.write_bytes(b"valid")
    stage = finalize_stage_result(StageResult(name="reframe", expected=1, succeeded=1, outputs=[str(output)]))

    result = _skip_batch_result(
        reframe_stage=stage,
        expected_final=1,
        message="batch plan unavailable",
        paused=False,
        cancel_requested=False,
        metadata={},
    )

    assert result.outputs == [str(output)]
    assert result.succeeded == 1


def test_safe_filename_rejects_paths():
    assert worker._safe_filename("output_001.mp4") == "output_001.mp4"
    with pytest.raises(Exception):
        worker._safe_filename("../output_001.mp4")
    with pytest.raises(Exception):
        worker._safe_filename("nested/output_001.mp4")


def test_input_bridge_includes_sources_and_product_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "JOBS_DIR", tmp_path / "jobs")
    job_id = "job-input-1"
    job_dir = worker._job_dir(job_id)
    (job_dir / "product_1.mp4").write_bytes(b"product")
    (job_dir / "background_1.mp4").write_bytes(b"background")
    (job_dir / "source_1.mp4").write_bytes(b"source")
    root = job_dir / "root"
    (root / "product").mkdir(parents=True)
    (root / "bg").mkdir()
    (root / "audio").mkdir()

    request = worker.TCRenderRequest(
        product_ids=["product_1"],
        background_ids=["background_1"],
        source_ids=["source_1"],
        product_roots=[str(root)],
    )
    inputs = worker._build_tc_inputs(job_id, request)

    assert inputs.products == [str(job_dir / "product_1.mp4")]
    assert inputs.backgrounds == [str(job_dir / "background_1.mp4")]
    assert inputs.sources == [str(job_dir / "source_1.mp4")]
    assert inputs.product_roots == [str(root)]


def test_output_manifest_keeps_product_prefixed_final(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "JOBS_DIR", tmp_path / "jobs")
    job_id = "job-output-1"
    job_dir = worker._job_dir(job_id)
    input_path = job_dir / "product_1.mp4"
    output_path = job_dir / "product_1_single_001.mp4"
    input_path.write_bytes(b"input")
    output_path.write_bytes(b"output")
    result = SimpleNamespace(outputs=[str(output_path)])

    names = worker._collect_outputs(job_id, result, {str(input_path.resolve())})

    assert names == ["product_1_single_001.mp4"]


def test_status_response_does_not_duplicate_job_id(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "JOBS_DIR", tmp_path / "jobs")
    job_id = "job-status-1"
    worker._update_job(job_id, status="running", persist=True)

    response = __import__("asyncio").run(worker.get_status(job_id, True))

    assert response.job_id == job_id
    assert response.status == "running"
