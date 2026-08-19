from __future__ import annotations

import time
import uuid
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx

from worker.app.backend import main as worker


def _wait_for_terminal(job_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = worker._snapshot(job_id) or {}
        if state.get("status") in worker.TERMINAL_STATUSES:
            return state
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {worker._snapshot(job_id)}")


def test_submit_job_is_non_blocking_and_persists_terminal_state():
    job_id = f"queue-{uuid.uuid4().hex}"

    def factory(control):
        time.sleep(0.08)
        return {"job_id": job_id, "status": "succeeded", "output_files": ["output.mp4"], "progress": 100}

    started = time.monotonic()
    state = worker._submit_job(job_id, "tc01", factory)
    enqueue_elapsed = time.monotonic() - started
    terminal = _wait_for_terminal(job_id)

    assert enqueue_elapsed < 0.05
    assert state["status"] == "queued"
    assert terminal["status"] == "succeeded"
    assert terminal["output_files"] == ["output.mp4"]
    assert worker._load_persisted_state(job_id)["status"] == "succeeded"


def test_cancel_signal_is_observed_by_runner():
    job_id = f"cancel-{uuid.uuid4().hex}"

    def factory(control):
        while not control.cancel_event.is_set():
            time.sleep(0.01)
        return {"job_id": job_id, "status": "cancelled", "output_files": []}

    worker._submit_job(job_id, "tc01", factory)
    time.sleep(0.05)
    worker._control_job(job_id, "cancel")
    terminal = _wait_for_terminal(job_id)

    assert terminal["status"] == "cancelled"
    assert terminal["cancel_requested"] is True


def test_health_and_status_stay_responsive_while_job_runs(monkeypatch):
    job_id = f"health-{uuid.uuid4().hex}"

    def factory(control):
        while not control.cancel_event.is_set():
            time.sleep(0.01)
        return {"job_id": job_id, "status": "cancelled", "output_files": []}

    monkeypatch.setattr(
        worker,
        "_health_snapshot_sync",
        lambda: {"gpu": {}, "encoder": ["libx264", []], "supports_chromakey_cuda": False, "data_dir": "test"},
    )
    worker._submit_job(job_id, "tc01", factory)

    async def probe():
        transport = httpx.ASGITransport(app=worker.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            health = await client.get("/health")
            status = await client.get(
                f"/v1/jobs/{job_id}/status",
                headers={"X-Cutdee-Internal": worker.INTERNAL_TOKEN},
            )
            return health, status

    health, status = asyncio.run(probe())
    worker._control_job(job_id, "cancel")
    _wait_for_terminal(job_id)

    assert health.status_code == 200
    assert health.json()["active_jobs"] >= 1
    assert status.status_code == 200
    assert status.json()["job_id"] == job_id


def test_http_upload_queue_status_and_output_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "JOBS_DIR", tmp_path / "jobs")

    class FakeResult:
        status = SimpleNamespace(value="SUCCEEDED")
        expected = 1
        succeeded = 1
        failed = 0
        cancelled = 0

        def __init__(self, output):
            self.outputs = [str(output)]

        def to_dict(self):
            return {
                "status": "SUCCEEDED",
                "expected": 1,
                "succeeded": 1,
                "failed": 0,
                "cancelled": 0,
                "outputs": self.outputs,
                "all_errors": [],
            }

    def fake_pipeline(inputs, callbacks):
        output = Path(inputs.output_dir) / "source__lens16mm__center.mp4"
        output.write_bytes(b"valid-output")
        callbacks.progress_fn(100, "done")
        return FakeResult(output)

    monkeypatch.setitem(worker.PIPELINES, "tc05", fake_pipeline)
    job_id = f"http-{uuid.uuid4().hex}"

    async def exercise():
        transport = httpx.ASGITransport(app=worker.app)
        headers = {"X-Cutdee-Internal": worker.INTERNAL_TOKEN}
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            upload = await client.post(
                f"/v1/jobs/{job_id}/upload/source",
                content=b"source-input",
                headers={**headers, "Content-Disposition": "attachment; filename=source.mp4"},
            )
            render = await client.post(
                f"/v1/tc05/render/{job_id}",
                json={"source_ids": ["source"]},
                headers=headers,
            )
            for _ in range(50):
                status = await client.get(f"/v1/jobs/{job_id}/status", headers=headers)
                if status.json().get("status") == "succeeded":
                    output = await client.get(
                        f"/v1/jobs/{job_id}/output",
                        params={"filename": "source__lens16mm__center.mp4"},
                        headers=headers,
                    )
                    return upload, render, status, output
                await asyncio.sleep(0.01)
            raise AssertionError(status.text)

    upload, render, status, output = asyncio.run(exercise())
    assert upload.status_code == 200
    assert render.status_code == 202
    assert render.json()["status"] == "queued"
    assert status.json()["output_files"] == ["source__lens16mm__center.mp4"]
    assert output.status_code == 200
    assert output.content == b"valid-output"
