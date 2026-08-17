"""Preflight and job-history helpers for the desktop render UI."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS


@dataclass
class PreflightItem:
    key: str
    label: str
    status: str
    detail: str = ""
    fix: str = ""
    action: str = ""
    file: str = ""

    @property
    def passed(self) -> bool:
        return self.status in {"ok", "warn"}


@dataclass
class PreflightResult:
    ok: bool
    items: List[PreflightItem] = field(default_factory=list)
    input_bytes: int = 0
    free_bytes: int = 0
    required_free_bytes: int = 0
    selected_encoder: str = ""
    gpu: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["items"] = [asdict(item) for item in self.items]
        return data

    @property
    def failed_items(self) -> List[PreflightItem]:
        return [item for item in self.items if item.status == "fail"]


def user_data_dir() -> Path:
    path = Path.home() / ".green_pc"
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_history_path() -> Path:
    return user_data_dir() / "job_history.json"


def _flatten_files(groups: Dict[str, Iterable[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for files in groups.values():
        for value in files or []:
            if not value:
                continue
            path = os.path.abspath(str(value))
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _run_cmd(cmd: List[str], timeout: float = 8.0) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=flags,
    )


def ffmpeg_ready(ffmpeg_cmd: str = "ffmpeg") -> tuple[bool, str]:
    exe = shutil.which(ffmpeg_cmd) or ffmpeg_cmd
    try:
        result = _run_cmd([exe, "-version"], timeout=5)
        first = (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else ""
        return result.returncode == 0 and "ffmpeg" in first.lower(), first or exe
    except Exception as exc:
        return False, str(exc)


def ffprobe_ready(ffprobe_cmd: str = "ffprobe") -> tuple[bool, str]:
    exe = shutil.which(ffprobe_cmd) or ffprobe_cmd
    try:
        result = _run_cmd([exe, "-version"], timeout=5)
        first = (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else ""
        return result.returncode == 0 and "ffprobe" in first.lower(), first or exe
    except Exception as exc:
        return False, str(exc)


def probe_media_readable(
    path: str,
    ffprobe_cmd: str = "ffprobe",
    *,
    expected_stream: str = "",
) -> tuple[bool, str]:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        try:
            from PIL import Image

            with Image.open(path) as img:
                img.verify()
            return True, "image readable"
        except Exception as exc:
            return False, str(exc)

    if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
        exe = shutil.which(ffprobe_cmd) or ffprobe_cmd
        try:
            stream_name = str(expected_stream).strip().lower()
            stream_selector = {"audio": "a:0", "video": "v:0"}.get(stream_name)
            show_entries = (
                "stream=codec_type:format=duration"
                if stream_selector
                else "format=duration"
            )
            command = [exe, "-v", "error"]
            if stream_selector:
                command.extend(["-select_streams", stream_selector])
            command.extend(
                [
                    "-show_entries",
                    show_entries,
                    "-of",
                    "json",
                    path,
                ]
            )
            result = _run_cmd(
                command,
                timeout=10,
            )
            if result.returncode != 0:
                return False, (result.stderr or "ffprobe failed").strip()[:300]
            if stream_selector:
                try:
                    payload = json.loads(result.stdout or "{}")
                except (TypeError, ValueError) as exc:
                    return False, f"invalid ffprobe JSON: {exc}"
                streams = payload.get("streams")
                if not isinstance(streams, list) or not any(
                    isinstance(item, dict)
                    and item.get("codec_type") == stream_name
                    for item in streams
                ):
                    return False, f"no readable {stream_name} stream"
            return True, "media readable"
        except Exception as exc:
            return False, str(exc)

    return True, "not a media file"


def _check_output_writable(output_dir: str) -> tuple[bool, str]:
    try:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        test_path = out / f".greenpc_write_test_{os.getpid()}_{int(time.time())}.tmp"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink(missing_ok=True)
        return True, str(out)
    except Exception as exc:
        return False, str(exc)


def _disk_free(output_dir: str) -> tuple[int, str]:
    try:
        out = Path(output_dir)
        probe = out if out.exists() else out.parent
        usage = shutil.disk_usage(str(probe))
        return int(usage.free), str(probe)
    except Exception as exc:
        return 0, str(exc)


def run_preflight(
    *,
    files_by_group: Dict[str, Iterable[str]],
    output_dir: str,
    encoder_alias: str = "auto",
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
) -> PreflightResult:
    items: List[PreflightItem] = []
    files = _flatten_files(files_by_group)
    roles_by_path: Dict[str, set[str]] = {}
    for group, group_files in files_by_group.items():
        role = str(group).strip().lower()
        for value in group_files or []:
            if not value:
                continue
            path = os.path.abspath(str(value))
            roles_by_path.setdefault(path, set()).add(role)
    input_bytes = 0

    missing = [path for path in files if not os.path.isfile(path)]
    if missing:
        items.append(
            PreflightItem(
                key="files_exist",
                label="ไฟล์ input",
                status="fail",
                detail=f"หาย {len(missing)} ไฟล์",
                fix="ลบไฟล์ที่หายออกจากรายการ หรือเลือกไฟล์ใหม่",
                action="remove_missing_files",
                file=missing[0],
            )
        )
    else:
        items.append(
            PreflightItem(
                key="files_exist",
                label="ไฟล์ input",
                status="ok",
                detail=f"พบ {len(files)} ไฟล์",
            )
        )

    if files and not missing:
        unreadable: List[tuple[str, str]] = []
        for path in files:
            try:
                input_bytes += os.path.getsize(path)
            except OSError:
                pass
            ext = Path(path).suffix.lower()
            if ext in MEDIA_EXTS:
                roles = roles_by_path.get(os.path.abspath(path), set())
                expected_stream = ""
                if "audio" in roles or (ext in AUDIO_EXTS and not roles):
                    expected_stream = "audio"
                elif roles.intersection(
                    {"product", "source", "bg", "background", "cover"}
                ):
                    expected_stream = "video"
                ok, detail = probe_media_readable(
                    path,
                    ffprobe_cmd=ffprobe_cmd,
                    expected_stream=expected_stream,
                )
                if not ok:
                    if "audio" in roles:
                        if detail.strip().lower() == "no readable audio stream":
                            detail = "Audio has no readable audio stream"
                        else:
                            detail = (
                                "Audio has no readable audio stream: "
                                f"{detail}"
                            )
                    unreadable.append((path, detail))
        if unreadable:
            bad_path, detail = unreadable[0]
            items.append(
                PreflightItem(
                    key="codec_readable",
                    label="codec/ไฟล์อ่านได้",
                    status="fail",
                    detail=detail,
                    fix="แปลงไฟล์ใหม่ หรือเลือกไฟล์ที่ ffmpeg อ่านได้",
                    action="retry",
                    file=bad_path,
                )
            )
        else:
            items.append(
                PreflightItem(
                    key="codec_readable",
                    label="codec/ไฟล์อ่านได้",
                    status="ok",
                    detail=f"อ่าน media ได้ {len(files)} ไฟล์",
                )
            )
    elif not files:
        items.append(
            PreflightItem(
                key="codec_readable",
                label="codec/ไฟล์อ่านได้",
                status="warn",
                detail="ยังไม่มีไฟล์ให้ตรวจ",
                fix="เลือกไฟล์ก่อน render",
            )
        )

    writable, writable_detail = _check_output_writable(output_dir)
    items.append(
        PreflightItem(
            key="output_writable",
            label="output เขียนได้",
            status="ok" if writable else "fail",
            detail=writable_detail,
            fix="" if writable else "สร้างหรือเลือก output folder ที่เขียนได้",
            action="" if writable else "create_output_dir",
            file=output_dir,
        )
    )

    free_bytes, disk_detail = _disk_free(output_dir)
    required_free = max(512 * 1024 * 1024, input_bytes * 2)
    disk_ok = free_bytes >= required_free
    items.append(
        PreflightItem(
            key="disk_space",
            label="พื้นที่ว่าง",
            status="ok" if disk_ok else "fail",
            detail=f"free={free_bytes} required={required_free} at {disk_detail}",
            fix="" if disk_ok else "เพิ่มพื้นที่ว่างหรือเปลี่ยน output drive",
            action="" if disk_ok else "open_output_folder",
            file=output_dir,
        )
    )

    ffmpeg_ok, ffmpeg_detail = ffmpeg_ready(ffmpeg_cmd)
    items.append(
        PreflightItem(
            key="ffmpeg_ready",
            label="ffmpeg พร้อม",
            status="ok" if ffmpeg_ok else "fail",
            detail=ffmpeg_detail,
            fix="" if ffmpeg_ok else "ติดตั้ง ffmpeg หรือเปิดโปรแกรมจาก build ที่มี ffmpeg",
            action="retry" if not ffmpeg_ok else "",
        )
    )

    ffprobe_ok, ffprobe_detail = ffprobe_ready(ffprobe_cmd)
    items.append(
        PreflightItem(
            key="ffprobe_ready",
            label="ffprobe พร้อม",
            status="ok" if ffprobe_ok else "fail",
            detail=ffprobe_detail,
            fix="" if ffprobe_ok else "ติดตั้ง ffprobe คู่กับ ffmpeg",
            action="retry" if not ffprobe_ok else "",
        )
    )

    gpu: Dict[str, Any] = {}
    selected_encoder = ""
    try:
        from core.gpu_detector import effective_video_encoder, gpu_summary, resolve_encoder_alias

        gpu = gpu_summary(ffmpeg_cmd=ffmpeg_cmd)
        selected_encoder, _args = effective_video_encoder(
            preferred=resolve_encoder_alias(encoder_alias),
            ffmpeg_cmd=ffmpeg_cmd,
        )
        gpu_status = "ok" if gpu.get("nvenc_ready") or selected_encoder != "libx264" else "warn"
        items.append(
            PreflightItem(
                key="gpu_encoder",
                label="GPU/encoder",
                status=gpu_status,
                detail=f"selected={selected_encoder} gpu={gpu}",
                fix="" if gpu_status == "ok" else "ใช้ auto/libx264 ได้ แต่ถ้าต้องการ GPU ให้เช็ก driver/NVENC",
                action="" if gpu_status == "ok" else "set_encoder_auto",
            )
        )
    except Exception as exc:
        items.append(
            PreflightItem(
                key="gpu_encoder",
                label="GPU/encoder",
                status="warn" if ffmpeg_ok else "fail",
                detail=str(exc),
                fix="เปลี่ยน encoder เป็น auto หรือใช้ libx264",
                action="set_encoder_auto",
            )
        )

    ok = all(item.passed for item in items)
    return PreflightResult(
        ok=ok,
        items=items,
        input_bytes=input_bytes,
        free_bytes=free_bytes,
        required_free_bytes=required_free,
        selected_encoder=selected_encoder,
        gpu=gpu,
    )


def load_job_history(limit: int = 30) -> List[Dict[str, Any]]:
    path = job_history_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)][:limit]


def save_job_history(items: List[Dict[str, Any]], limit: int = 30) -> None:
    path = job_history_path()
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(items[:limit], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        pass


def add_job_history(record: Dict[str, Any], limit: int = 30) -> Dict[str, Any]:
    item = dict(record)
    item.setdefault("id", uuid.uuid4().hex)
    item.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    items = load_job_history(limit=limit)
    dedupe_key = (item.get("label"), item.get("output_dir"), item.get("created_at"))
    filtered = [
        old for old in items
        if (old.get("label"), old.get("output_dir"), old.get("created_at")) != dedupe_key
        and old.get("id") != item.get("id")
    ]
    filtered.insert(0, item)
    save_job_history(filtered, limit=limit)
    return item


def delete_job_history(job_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    items = [item for item in load_job_history(limit=limit) if item.get("id") != job_id]
    save_job_history(items, limit=limit)
    return items


def is_published_output_path(path: os.PathLike[str] | str) -> bool:
    """Return False for atomic-render scratch artifacts.

    Render engines publish outputs via names containing ``.partial.`` before
    an atomic rename. Such files are never user-visible results, even when
    their final suffix (for example ``.mp4``) matches a gallery scan.
    """

    try:
        return ".partial." not in Path(os.fspath(path)).name.casefold()
    except TypeError:
        return False


def list_output_files(output_dir: str, limit: int = 100) -> List[str]:
    out = Path(output_dir)
    if not out.is_dir():
        return []
    patterns = ["*.mp4", "*.mov", "*.mkv", "*.webm", "*.png", "*.jpg", "*.jpeg", "*.gif"]
    files: List[Path] = []
    for pattern in patterns:
        files.extend(
            path
            for path in out.glob(pattern)
            if path.is_file() and is_published_output_path(path)
        )
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(path) for path in files[:limit]]
