"""Portable TC01-TC04 runner for the packaged EXE bundle.

The runner uses only files inside a portable target folder:

    <target>/input/product/*.mp4
    <target>/ffmpeg/bin/ffmpeg.exe
    <target>/ffmpeg/bin/ffprobe.exe

It never deletes previous outputs. Each run writes a new output directory and
paired API evidence files that can be merged into the delivery report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


EXPECTED_PRODUCT_NAMES = (
    "VID_20260519_181622.mp4",
    "VID_20260519_181707.mp4",
    "VID_20260519_181736.mp4",
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__"):
        return str(value)
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stable_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _run(args: list[str], timeout: int = 120) -> dict[str, Any]:
    started = time.time()
    p = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "args": args,
        "exit_code": p.returncode,
        "seconds": round(time.time() - started, 3),
        "stdout": p.stdout,
        "stderr": p.stderr,
    }


def _probe_video(path: Path, ffprobe_cmd: Path) -> dict[str, Any]:
    result = _run(
        [
            str(ffprobe_cmd),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout=120,
    )
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "ffprobe_exit_code": result["exit_code"],
        "duration": 0.0,
        "streams": [],
    }
    if result["exit_code"] == 0:
        try:
            parsed = json.loads(result["stdout"] or "{}")
            info["duration"] = float(parsed.get("format", {}).get("duration") or 0.0)
            info["streams"] = parsed.get("streams") or []
        except Exception as exc:
            info["parse_error"] = str(exc)
    else:
        info["stderr"] = result["stderr"][-1000:]
    info["ok"] = info["exists"] and info["size"] > 0 and info["duration"] > 0
    return info


def _ffmpeg_filters(ffmpeg_cmd: Path) -> dict[str, Any]:
    result = _run([str(ffmpeg_cmd), "-hide_banner", "-filters"], timeout=120)
    text = (result.get("stdout") or "") + (result.get("stderr") or "")
    filters = ["scale_cuda", "overlay_cuda", "chromakey_cuda", "despill_cuda", "despill"]
    return {
        "ffmpeg": str(ffmpeg_cmd),
        "exit_code": result["exit_code"],
        "filters": {name: name in text for name in filters},
    }


def _unique_dir(root: Path, prefix: str) -> Path:
    stamp = _now_id()
    candidate = root / f"{prefix}_{stamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"{prefix}_{stamp}_{suffix:02d}"
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _verify_files(files: list[Path], ffprobe_cmd: Path, min_size: int = 1024) -> list[dict[str, Any]]:
    verified = []
    for path in sorted(files):
        info = _probe_video(path, ffprobe_cmd)
        info["ok"] = bool(info["exists"] and info["size"] > min_size and info["duration"] > 0)
        verified.append(info)
    return verified


@dataclass
class CaseResult:
    case_id: str
    expected_outputs: int
    output_dir: str
    output_count: int
    ok_count: int
    failed_count: int
    seconds: float
    verdict: str
    errors: list[str]
    outputs: list[dict[str, Any]]
    extra: dict[str, Any]


class PortableRunContext:
    def __init__(self, target_dir: Path, report_dir: Path, pair_prefix: str) -> None:
        self.target_dir = target_dir.resolve()
        self.report_dir = report_dir.resolve()
        self.pair_prefix = pair_prefix
        self.api_dir = self.report_dir / "api"
        self.pairs_dir = self.report_dir / "pairs"
        self.logs_dir = self.report_dir / "logs"
        self.run_root = self.target_dir / "outputs" / "portable_tc_runs" / _now_id()
        self.input_dir = self.target_dir / "input" / "product"
        self.ffmpeg_cmd = self._resolve_tool("ffmpeg.exe")
        self.ffprobe_cmd = self._resolve_tool("ffprobe.exe")
        for directory in (self.api_dir, self.pairs_dir, self.logs_dir, self.run_root):
            directory.mkdir(parents=True, exist_ok=True)

    def _resolve_tool(self, exe_name: str) -> Path:
        candidates = [
            self.target_dir / "ffmpeg" / "bin" / exe_name,
        ]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "ffmpeg" / "bin" / exe_name)
        candidates.append(Path(exe_name))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    def products(self) -> list[Path]:
        if not self.input_dir.is_dir():
            raise FileNotFoundError(f"portable input folder not found: {self.input_dir}")
        products = [self.input_dir / name for name in EXPECTED_PRODUCT_NAMES]
        missing = [str(path) for path in products if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"missing portable real input files: {missing}")
        return products

    def preflight(self) -> dict[str, Any]:
        products = self.products()
        for tool in (self.ffmpeg_cmd, self.ffprobe_cmd):
            if not tool.is_file():
                raise FileNotFoundError(f"portable ffmpeg tool not found: {tool}")
        os.environ["PATH"] = str(self.ffmpeg_cmd.parent) + os.pathsep + os.environ.get("PATH", "")
        input_info = []
        for path in products:
            probed = _probe_video(path, self.ffprobe_cmd)
            probed["sha256"] = _sha256(path)
            input_info.append(probed)
        return {
            "target_dir": str(self.target_dir),
            "run_root": str(self.run_root),
            "ffmpeg": str(self.ffmpeg_cmd),
            "ffprobe": str(self.ffprobe_cmd),
            "filters": _ffmpeg_filters(self.ffmpeg_cmd),
            "inputs": input_info,
        }

    def log_path(self, case_id: str) -> Path:
        return self.logs_dir / f"{self.pair_prefix}_{case_id}.log"

    def write_pair(
        self,
        case_id: str,
        request_id: str,
        mode_hash: str,
        request_file: Path,
        response_file: Path,
        timing_file: Path,
        result: CaseResult,
    ) -> dict[str, Any]:
        binding = {
            "pair_id": f"{self.pair_prefix}_{case_id}",
            "case_id": case_id,
            "screenshot_list": [],
            "request_file": str(request_file),
            "response_file": str(response_file),
            "timing_file": str(timing_file),
            "request_id_match": True,
            "mode_text_hash_match": True,
            "api_verdict": result.verdict,
            "ui_verdict": "PENDING_COMPUTER_SCREENSHOTS",
            "pair_verdict": (
                "PASS_API_ONLY_PENDING_UI" if result.verdict == "PASS" else "FAIL_API"
            ),
            "notes": [
                "This binding is generated by the packaged EXE runner.",
                "Computer Use screenshots must be attached by the final strict report gate.",
            ],
            "checks": {
                "request_id": request_id,
                "mode_hash": mode_hash,
                "expected_outputs": result.expected_outputs,
                "output_count": result.output_count,
                "ok_count": result.ok_count,
                "failed_count": result.failed_count,
            },
        }
        path = self.pairs_dir / f"{self.pair_prefix}_{case_id}__binding.json"
        _write_json(path, binding)
        return binding


def _case_files(ctx: PortableRunContext, case_id: str) -> tuple[Path, Path, Path]:
    base = f"{ctx.pair_prefix}_{case_id}"
    return (
        ctx.api_dir / f"{base}_request.json",
        ctx.api_dir / f"{base}_response.json",
        ctx.api_dir / f"{base}_timing.json",
    )


def _run_case(
    ctx: PortableRunContext,
    case_id: str,
    config: dict[str, Any],
    runner: Callable[[PortableRunContext, list[Path], Path, Callable[[str], None]], CaseResult],
) -> tuple[CaseResult, dict[str, Any]]:
    products = ctx.products()
    mode_hash = _stable_hash(
        {
            "case_id": case_id,
            "config": config,
            "inputs": [{path.name: _sha256(path)} for path in products],
        }
    )
    request_id = f"{ctx.pair_prefix}_{case_id}_{mode_hash[:12]}"
    request_file, response_file, timing_file = _case_files(ctx, case_id)
    output_dir = _unique_dir(ctx.run_root, case_id.lower())
    request = {
        "request_id": request_id,
        "case_id": case_id,
        "mode_hash": mode_hash,
        "target_dir": str(ctx.target_dir),
        "input_dir": str(ctx.input_dir),
        "input_files": [str(path) for path in products],
        "output_dir": str(output_dir),
        "config": config,
        "ffmpeg": str(ctx.ffmpeg_cmd),
        "ffprobe": str(ctx.ffprobe_cmd),
    }
    _write_json(request_file, request)
    log_file = ctx.log_path(case_id)

    def log(message: str) -> None:
        with log_file.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")

    started = time.time()
    start_iso = datetime.now().isoformat()
    try:
        result = runner(ctx, products, output_dir, log)
    except Exception as exc:
        log(traceback.format_exc())
        raw_expected = config.get("expected_outputs", 0)
        expected_on_error = raw_expected if isinstance(raw_expected, int) else 0
        result = CaseResult(
            case_id=case_id,
            expected_outputs=expected_on_error,
            output_dir=str(output_dir),
            output_count=0,
            ok_count=0,
            failed_count=1,
            seconds=round(time.time() - started, 3),
            verdict="FAIL",
            errors=[str(exc)],
            outputs=[],
            extra={"traceback": traceback.format_exc()},
        )
    ended = time.time()
    result.seconds = round(ended - started, 3)
    response = {
        "request_id": request_id,
        "case_id": case_id,
        "mode_hash": mode_hash,
        "result": asdict(result),
    }
    timing = {
        "request_id": request_id,
        "case_id": case_id,
        "start_iso": start_iso,
        "end_iso": datetime.now().isoformat(),
        "seconds": round(ended - started, 3),
    }
    _write_json(response_file, response)
    _write_json(timing_file, timing)
    binding = ctx.write_pair(
        case_id,
        request_id,
        mode_hash,
        request_file,
        response_file,
        timing_file,
        result,
    )
    return result, binding


def _tc01(ctx: PortableRunContext, products: list[Path], output_dir: Path, log: Callable[[str], None]) -> CaseResult:
    from ..green_render import GreenSettings, render_green

    settings = GreenSettings(
        width=1920,
        height=1080,
        fps=30,
        bitrate="8000k",
        encoder_alias="nvenc",
        key_color="#00FF00",
        similarity=0.29,
        blend=0.04,
        despill=0.32,
        despill_screen=True,
        cover_enabled=False,
        cover_duration=0.0,
        cover_scale=1.0,
        audio_source="none",
        preset="ultrafast",
    )
    background = products[0]
    errors: list[str] = []
    started = time.time()
    for i, product in enumerate(products, 1):
        out_path = output_dir / f"{product.stem}__tc01_single__{_now_id()}.mp4"
        log(f"TC01 [{i}/{len(products)}] {product.name} -> {out_path.name}")
        result = render_green(
            cover=None,
            product=str(product),
            background=str(background),
            audio=None,
            out_path=str(out_path),
            settings=settings,
            on_log=log,
            on_progress=None,
            stop_check=lambda: False,
            ffmpeg_cmd=str(ctx.ffmpeg_cmd),
            ffprobe_cmd=str(ctx.ffprobe_cmd),
        )
        if not result.success:
            errors.append(f"{product.name}: {getattr(result, 'error', 'failed')}")
    files = sorted(output_dir.glob("*.mp4"))
    outputs = _verify_files(files, ctx.ffprobe_cmd, min_size=0)
    ok_count = sum(1 for item in outputs if item["ok"])
    if len(files) != 3:
        errors.append(f"expected 3 mp4 files, got {len(files)}")
    if ok_count != 3:
        errors.append(f"expected 3 valid outputs, got {ok_count}")
    return CaseResult(
        case_id="TC01",
        expected_outputs=3,
        output_dir=str(output_dir),
        output_count=len(files),
        ok_count=ok_count,
        failed_count=max(0, 3 - ok_count),
        seconds=round(time.time() - started, 3),
        verdict="PASS" if not errors else "FAIL",
        errors=errors,
        outputs=outputs,
        extra={"mode": "single_no_reframe_no_batch"},
    )


def _tc02(ctx: PortableRunContext, products: list[Path], output_dir: Path, log: Callable[[str], None]) -> CaseResult:
    from ..ai_reframe import FIXED_7X3_LENSES, ReframeSettings, render_reframe_plan

    settings = ReframeSettings(
        use_fixed_7x3=True,
        platform_key="custom",
        output_width=1920,
        output_height=1080,
        compositions=["center", "left", "right"],
        encoder_alias="nvenc",
        bitrate="8000k",
        max_parallel_ffmpeg=3,
    )
    expected = len(products) * 21
    errors: list[str] = []
    started = time.time()
    results = render_reframe_plan(
        sources=[str(path) for path in products],
        out_dir=str(output_dir),
        settings=settings,
        on_log=log,
        on_progress=lambda current, total, task: log(f"TC02 progress {current}/{total}") if current % 10 == 0 or current == total else None,
        stop_check=lambda: False,
        ffmpeg_cmd=str(ctx.ffmpeg_cmd),
        ffprobe_cmd=str(ctx.ffprobe_cmd),
    )
    render_ok = sum(1 for item in results if item.success)
    render_fail = sum(1 for item in results if not item.success)
    files = sorted(output_dir.glob("*.mp4"))
    outputs = _verify_files(files, ctx.ffprobe_cmd, min_size=1024)
    ok_count = sum(1 for item in outputs if item["ok"])
    if len(files) != expected:
        errors.append(f"expected {expected} mp4 files, got {len(files)}")
    if ok_count != expected:
        errors.append(f"expected {expected} valid outputs, got {ok_count}")
    if render_fail:
        errors.append(f"render_reframe_plan failures: {render_fail}")
    expected_lens = {item.key for item in FIXED_7X3_LENSES}
    expected_comps = {"center", "left", "right"}
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        parts = path.stem.split("__")
        if len(parts) != 3:
            errors.append(f"{path.name}: filename pattern mismatch")
            continue
        src, lens, comp = parts
        if lens not in expected_lens:
            errors.append(f"{path.name}: unknown lens {lens}")
        if comp not in expected_comps:
            errors.append(f"{path.name}: unknown composition {comp}")
        seen.add((src, lens, comp))
    for product in products:
        for lens in expected_lens:
            for comp in expected_comps:
                if (product.stem, lens, comp) not in seen:
                    errors.append(f"missing combo {product.stem}__{lens}__{comp}")
                    break
    return CaseResult(
        case_id="TC02",
        expected_outputs=expected,
        output_dir=str(output_dir),
        output_count=len(files),
        ok_count=ok_count,
        failed_count=max(render_fail, expected - ok_count),
        seconds=round(time.time() - started, 3),
        verdict="PASS" if not errors else "FAIL",
        errors=errors[:50],
        outputs=outputs,
        extra={"mode": "reframe_no_batch", "render_ok": render_ok, "render_fail": render_fail},
    )


def _tc03(ctx: PortableRunContext, products: list[Path], output_dir: Path, log: Callable[[str], None]) -> CaseResult:
    from ..batch_pingpong import BatchSettings, MatchMode, estimate_duration_split_count, render_batch
    from ..green_render import GreenSettings

    base_settings = GreenSettings(
        width=1920,
        height=1080,
        fps=30,
        bitrate="8000k",
        encoder_alias="nvenc",
        key_color="#00FF00",
        similarity=0.29,
        blend=0.04,
        despill=0.32,
        despill_screen=True,
        cover_enabled=False,
        cover_duration=0.0,
        cover_scale=1.0,
        audio_source="product",
        preset="ultrafast",
    )
    batch_settings = BatchSettings(
        segment_duration=10.0,
        num_outputs=1,
        split_by_duration=True,
        product_ping_pong=True,
        background_ping_pong=False,
        cover_mode=MatchMode.NO_REPEAT,
        background_mode=MatchMode.NO_REPEAT,
        audio_mode=MatchMode.NO_REPEAT,
        use_uploaded_audio=False,
        use_product_audio=True,
        seed=42,
    )
    expected = estimate_duration_split_count([str(path) for path in products], batch_settings.segment_duration)
    batch_settings.num_outputs = expected
    errors: list[str] = []
    started = time.time()
    results = render_batch(
        products=[str(path) for path in products],
        backgrounds=[str(path) for path in products],
        audios=[],
        out_dir=str(output_dir),
        base_settings=base_settings,
        batch_settings=batch_settings,
        on_log=log,
        on_progress=lambda current, total, progress: None,
        on_match=lambda match: log(f"TC03 match {match.output_index}: {Path(match.product_path).name}"),
        stop_check=lambda: False,
        ffmpeg_cmd=str(ctx.ffmpeg_cmd),
        ffprobe_cmd=str(ctx.ffprobe_cmd),
    )
    render_ok = sum(1 for item in results if item.success)
    render_fail = sum(1 for item in results if not item.success)
    files = sorted(output_dir.glob("*.mp4"))
    outputs = _verify_files(files, ctx.ffprobe_cmd, min_size=1024)
    ok_count = sum(1 for item in outputs if item["ok"])
    if len(files) != expected:
        errors.append(f"expected {expected} mp4 files, got {len(files)}")
    if ok_count != expected:
        errors.append(f"expected {expected} valid outputs, got {ok_count}")
    if render_fail:
        errors.append(f"render_batch failures: {render_fail}")
    for path in files:
        stem = path.stem
        if not stem.startswith("batch_"):
            errors.append(f"{path.name}: filename does not start with batch_")
    return CaseResult(
        case_id="TC03",
        expected_outputs=expected,
        output_dir=str(output_dir),
        output_count=len(files),
        ok_count=ok_count,
        failed_count=max(render_fail, expected - ok_count),
        seconds=round(time.time() - started, 3),
        verdict="PASS" if not errors else "FAIL",
        errors=errors,
        outputs=outputs,
        extra={
            "mode": "batch_no_reframe_duration_split",
            "segment_duration_sec": batch_settings.segment_duration,
            "split_by_duration": True,
            "render_ok": render_ok,
            "render_fail": render_fail,
        },
    )


def _tc04(ctx: PortableRunContext, products: list[Path], output_dir: Path, log: Callable[[str], None]) -> CaseResult:
    from ..ai_reframe import FIXED_7X3_LENSES, ReframeSettings, render_reframe_plan
    from ..batch_pingpong import BatchSettings, MatchMode, estimate_duration_split_count, render_batch
    from ..green_render import GreenSettings

    reframe_dir = output_dir / "reframe"
    batch_dir = output_dir / "batch"
    reframe_dir.mkdir(parents=True, exist_ok=False)
    batch_dir.mkdir(parents=True, exist_ok=False)
    base_settings = GreenSettings(
        width=1080,
        height=1920,
        fps=30,
        bitrate="6000k",
        encoder_alias="h264_nvenc",
        key_color="#00FF00",
        similarity=0.29,
        blend=0.04,
        despill=0.32,
        despill_screen=True,
        cover_enabled=False,
        cover_duration=0.0,
        cover_scale=1.0,
        audio_source="product",
        preset="medium",
    )
    batch_settings = BatchSettings(
        segment_duration=10.0,
        num_outputs=1,
        split_by_duration=True,
        product_ping_pong=True,
        background_ping_pong=False,
        cover_mode=MatchMode.NO_REPEAT,
        background_mode=MatchMode.NO_REPEAT,
        audio_mode=MatchMode.NO_REPEAT,
        use_uploaded_audio=False,
        use_product_audio=True,
        seed=42,
    )
    reframe_settings = ReframeSettings(
        use_fixed_7x3=True,
        platform_key="custom",
        output_width=1080,
        output_height=1920,
        compositions=["center", "left", "right"],
        encoder_alias="h264_nvenc",
        bitrate="6000k",
        max_parallel_ffmpeg=3,
    )
    expected_reframe = len(products) * len(FIXED_7X3_LENSES) * len(reframe_settings.compositions)
    errors: list[str] = []
    started = time.time()
    log(f"TC04 step 1 reframe: {len(products)} products -> {expected_reframe} clips")
    reframe_results = render_reframe_plan(
        sources=[str(path) for path in products],
        out_dir=str(reframe_dir),
        settings=reframe_settings,
        on_log=log,
        on_progress=lambda current, total, task: log(f"TC04 progress {current}/{total}") if current % 20 == 0 or current == total else None,
        stop_check=lambda: False,
        ffmpeg_cmd=str(ctx.ffmpeg_cmd),
        ffprobe_cmd=str(ctx.ffprobe_cmd),
    )
    reframe_ok = sum(1 for item in reframe_results if item.success)
    reframe_fail = sum(1 for item in reframe_results if not item.success)
    reframe_files = sorted(reframe_dir.glob("*.mp4"))
    reframe_checks = _verify_files(reframe_files, ctx.ffprobe_cmd, min_size=1024)
    valid_reframe = sum(1 for item in reframe_checks if item["ok"])
    if len(reframe_files) != expected_reframe:
        errors.append(f"expected {expected_reframe} reframe files, got {len(reframe_files)}")
    if valid_reframe != expected_reframe:
        errors.append(f"expected {expected_reframe} valid reframe outputs, got {valid_reframe}")
    if reframe_fail:
        errors.append(f"render_reframe_plan failures: {reframe_fail}")

    expected_lens = {item.key for item in FIXED_7X3_LENSES}
    expected_comps = {"center", "left", "right"}
    seen: set[tuple[str, str, str]] = set()
    expected_product_names = {path.stem for path in products}
    for path in reframe_files:
        parts = path.stem.rsplit("__", 2)
        if len(parts) != 3:
            errors.append(f"{path.name}: filename pattern mismatch")
            continue
        product, lens, comp = parts
        if product not in expected_product_names:
            errors.append(f"{path.name}: unknown product {product}")
        if lens not in expected_lens:
            errors.append(f"{path.name}: unknown lens {lens}")
        if comp not in expected_comps:
            errors.append(f"{path.name}: unknown composition {comp}")
        seen.add((product, lens, comp))
    for product in expected_product_names:
        for lens in expected_lens:
            for comp in expected_comps:
                if (product, lens, comp) not in seen:
                    errors.append(f"missing combo {product}__{lens}__{comp}")
                    break

    expected = 0
    batch_results = []
    batch_render_ok = 0
    batch_render_fail = 0
    if not errors:
        reframe_sources = [str(path) for path in reframe_files]
        expected = estimate_duration_split_count(reframe_sources, batch_settings.segment_duration)
        batch_settings.num_outputs = expected
        log(f"TC04 step 2 batch: {len(reframe_sources)} reframe clips -> {expected} outputs")
        batch_results = render_batch(
            products=reframe_sources,
            backgrounds=[str(path) for path in products],
            audios=[],
            out_dir=str(batch_dir),
            base_settings=base_settings,
            batch_settings=batch_settings,
            on_log=log,
            on_progress=lambda current, total, progress: None,
            on_match=lambda match: log(
                f"TC04 batch match {match.output_index}: {Path(match.product_path).name} "
                f"{match.segment.time_range.start:.3f}-{match.segment.time_range.end:.3f}s"
            ),
            stop_check=lambda: False,
            ffmpeg_cmd=str(ctx.ffmpeg_cmd),
            ffprobe_cmd=str(ctx.ffprobe_cmd),
        )
        batch_render_ok = sum(1 for item in batch_results if item.success)
        batch_render_fail = sum(1 for item in batch_results if not item.success)
    else:
        errors.append("batch skipped because reframe contract failed")

    files = sorted(batch_dir.glob("*.mp4"))
    outputs = _verify_files(files, ctx.ffprobe_cmd, min_size=1024)
    ok_count = sum(1 for item in outputs if item["ok"])
    if expected and len(files) != expected:
        errors.append(f"expected {expected} batch files, got {len(files)}")
    if expected and ok_count != expected:
        errors.append(f"expected {expected} valid batch outputs, got {ok_count}")
    if batch_render_fail:
        errors.append(f"render_batch failures: {batch_render_fail}")
    return CaseResult(
        case_id="TC04",
        expected_outputs=expected,
        output_dir=str(output_dir),
        output_count=len(files),
        ok_count=ok_count,
        failed_count=max(render_fail, expected - ok_count),
        seconds=round(time.time() - started, 3),
        verdict="PASS" if not errors else "FAIL",
        errors=errors[:50],
        outputs=outputs,
        extra={
            "mode": "reframe_then_batch_duration_split",
            "reframe_dir": str(reframe_dir),
            "batch_dir": str(batch_dir),
            "pipeline_order": "reframe_then_batch",
            "segment_duration_sec": batch_settings.segment_duration,
            "split_by_duration": True,
            "expected_reframe_outputs": expected_reframe,
            "actual_reframe_outputs": len(reframe_files),
            "valid_reframe_outputs": valid_reframe,
            "reframe_checks": reframe_checks,
            "reframe_render_ok": reframe_ok,
            "reframe_render_fail": reframe_fail,
            "batch_render_ok": batch_render_ok,
            "batch_render_fail": batch_render_fail,
        },
    )


CASE_CONFIGS: dict[str, dict[str, Any]] = {
    "TC01": {"batch": False, "reframe": False, "expected_outputs": 3},
    "TC02": {"batch": False, "reframe": True, "expected_outputs": 63},
    "TC03": {"batch": True, "reframe": False, "expected_outputs": "duration_split_10s"},
    "TC04": {"batch": True, "reframe": True, "expected_outputs": "reframe_7x3_then_duration_split_10s"},
}

CASE_RUNNERS: dict[str, Callable[[PortableRunContext, list[Path], Path, Callable[[str], None]], CaseResult]] = {
    "TC01": _tc01,
    "TC02": _tc02,
    "TC03": _tc03,
    "TC04": _tc04,
}


def run_suite(target_dir: str | Path, report_dir: str | Path | None = None, pair_prefix: str = "PORTABLE_EXE") -> dict[str, Any]:
    target = Path(target_dir)
    if report_dir is None:
        report = target / "evidence" / f"portable_tc_{_now_id()}"
    else:
        report = Path(report_dir)
    ctx = PortableRunContext(target, report, pair_prefix)
    started = time.time()
    preflight = ctx.preflight()
    _write_json(ctx.api_dir / f"{pair_prefix}_preflight.json", preflight)

    results: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    stop_after_failure = False
    for case_id in ("TC01", "TC02", "TC03", "TC04"):
        if stop_after_failure:
            skipped = {
                "case_id": case_id,
                "verdict": "SKIPPED",
                "reason": "previous testcase failed; TC contract gate stops cascade",
            }
            results.append(skipped)
            continue
        result, binding = _run_case(ctx, case_id, CASE_CONFIGS[case_id], CASE_RUNNERS[case_id])
        results.append(asdict(result))
        bindings.append(binding)
        if result.verdict != "PASS":
            stop_after_failure = True

    pass_count = sum(1 for item in results if item.get("verdict") == "PASS")
    fail_count = sum(1 for item in results if item.get("verdict") == "FAIL")
    skipped_count = sum(1 for item in results if item.get("verdict") == "SKIPPED")
    summary = {
        "verdict": "PASS" if fail_count == 0 and skipped_count == 0 and pass_count == 4 else "FAIL",
        "strict_ui_api_verdict": "PENDING_COMPUTER_SCREENSHOTS",
        "target_dir": str(ctx.target_dir),
        "report_dir": str(ctx.report_dir),
        "run_root": str(ctx.run_root),
        "seconds": round(time.time() - started, 3),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "skipped_count": skipped_count,
        "results": results,
        "bindings": bindings,
    }
    _write_json(ctx.report_dir / "portable_tc_summary.json", summary)
    _write_json(ctx.api_dir / f"{pair_prefix}_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run portable SJ88 Green Screen (formerly AutoMv V3) TC01-TC04 suite")
    parser.add_argument("--target", required=True, help="Portable target folder, e.g. F:\\Green final exe")
    parser.add_argument("--report", default=None, help="Evidence output folder")
    parser.add_argument("--pair-prefix", default="PORTABLE_EXE")
    args = parser.parse_args(argv)
    summary = run_suite(args.target, args.report, args.pair_prefix)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
