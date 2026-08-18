from __future__ import annotations

import pytest

from gateway.app.backend import main as gateway


def test_gateway_status_normalization_matches_worker():
    assert gateway._normalize_status("SUCCEEDED") == "succeeded"
    assert gateway._normalize_status("done") == "succeeded"
    assert gateway._normalize_status("CANCELED") == "cancelled"


def test_gateway_output_manifest_falls_back_to_primary_file():
    assert gateway._output_names({"output_files": [], "output_file": "product_single.mp4"}) == ["product_single.mp4"]


def test_gateway_output_manifest_rejects_paths():
    row = {"output_files": ["../secret.mp4", "safe_output.mp4"]}
    assert gateway._output_names(row) == ["safe_output.mp4"]
    with pytest.raises(Exception):
        gateway._safe_output_name("../secret.mp4")


def test_gateway_upload_lookup_supports_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "UPLOADS_DIR", tmp_path)
    path = tmp_path / "product_1.mp4"
    path.write_bytes(b"data")
    assert gateway._find_upload_path("product_1") == path
