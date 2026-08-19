"""
Podcast Edit Engine — รวมคลิปวิดีโอจากหลาย sub-folder เป็นไฟล์เดียว

ใช้สำหรับ workflow แบบ Podcast:
  main_folder/
    001/  ← sub-folder ที่ 1 (เช่น intro, คำถาม, เนื้อหา)
    002/  ← sub-folder ที่ 2
    003/
    Used/ ← (สร้างอัตโนมัติตอน move_to_used)
      001/  002/  003/

Modes:
  - "subfolder": หยิบไฟล์จากแต่ละ sub-folder → ต่อกันเป็น 1 output
  - "flat":      หยิบไฟล์จาก main_folder โดยตรง (ไม่สน sub-folder)

Shuffle modes (เฉพาะ subfolder):
  - folder_shuffle: "sequential" | "shuffle" — ลำดับการ traverse sub-folder
  - file_shuffle:   "sequential" | "shuffle" — ลำดับการ traverse file ในแต่ละ sub-folder

Output: {output_folder}/podcast_{idx:03d}_{ts}.mp4

Code by Mini Max 2.1, 2026-06-11
"""
import os
import re
import shutil
import random
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Dict, Any


VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".m4v", ".avi")


# ==================== Helpers ====================

def _log(callback: Optional[Callable[[str], None]], msg: str):
    if callback:
        try:
            callback(msg)
        except Exception:
            pass


def _natural_key(s: str):
    """Sort key ที่เรียงตามตัวเลขในชื่อ เช่น clip_2 < clip_10"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _list_videos(folder: str, exclude_used: bool = True) -> List[str]:
    """คืน full path ของไฟล์วิดีโอในโฟลเดอร์ เรียงตามชื่อ (natural sort)"""
    if not folder or not os.path.isdir(folder):
        return []
    used_set: set = set()
    if exclude_used:
        used_path = os.path.join(folder, "Used")
        if os.path.isdir(used_path):
            used_set = set(os.listdir(used_path))
    files: List[str] = []
    for name in os.listdir(folder):
        if name in used_set:
            continue
        if name.lower().endswith(VIDEO_EXTS):
            files.append(os.path.join(folder, name))
    files.sort(key=lambda p: _natural_key(os.path.basename(p)))
    return files


def _shuffle_or_sequential(items: List, mode: str) -> List:
    """คืน list ใหม่ ถ้า mode เป็น 'shuffle' ก็สับเปลี่ยนแบบ in-place แล้วคืนเดิม"""
    if mode == "shuffle":
        random.shuffle(items)
    return list(items)


def _ffprobe_duration(path: str, ffprobe_cmd: str = "ffprobe") -> float:
    cmd = [
        ffprobe_cmd, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
        ).strip()
        return float(out)
    except Exception as e:
        raise RuntimeError(f"ffprobe error for {path}: {e}")


def _has_audio_stream(path: str, ffprobe_cmd: str = "ffprobe") -> bool:
    """ตรวจว่าไฟล์มี audio stream ไหม"""
    cmd = [
        ffprobe_cmd, "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0", path,
    ]
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
        ).strip()
        return bool(out)
    except Exception:
        return False


# ==================== Main API ====================

def run_podcast_edit(
    main_folder: str,
    output_folder: str,
    files_per_output: int,
    num_outputs: int,
    mode: str,
    folder_shuffle_mode: str,
    file_shuffle_mode: str,
    fps: Optional[str],
    resolution: Optional[Tuple[int, int]],
    bitrate: Optional[str],
    move_to_used_flag: bool,
    callback: Optional[Callable[[str], None]] = None,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
) -> Tuple[int, int]:
    """
    รวมคลิปวิดีโอเป็น podcast outputs

    Args:
        main_folder: โฟลเดอร์หลัก (มี sub-folder หรือไฟล์วิดีโอ)
        output_folder: โฟลเดอร์ output (ถ้าว่าง → ใช้ main_folder)
        files_per_output: จำนวนคลิปต่อ 1 output
        num_outputs: จำนวน output ที่ต้องการสร้าง
        mode: "subfolder" หรือ "flat"
        folder_shuffle_mode: "sequential" หรือ "shuffle" (สำหรับ subfolder mode)
        file_shuffle_mode: "sequential" หรือ "shuffle" (สำหรับไฟล์ในแต่ละ sub-folder)
        fps: optional fps เช่น "30" (None = คงเดิม)
        resolution: optional (w, h) (None = คงเดิม)
        bitrate: optional เช่น "8000k" (None = default libx264)
        move_to_used_flag: ถ้า True จะย้ายไฟล์ที่ใช้แล้วไป {subfolder}/Used/
        callback: ฟังก์ชันรับ log message
        ffmpeg_cmd / ffprobe_cmd: path ไปยัง ffmpeg/ffprobe

    Returns:
        (success_count, fail_count)
    """
    _log(callback, f"📂 main_folder: {main_folder}")
    _log(callback, f"📂 output_folder: {output_folder}")
    _log(callback, f"⚙️  mode={mode} | folder={folder_shuffle_mode} | file={file_shuffle_mode}")
    _log(callback, f"⚙️  files_per_output={files_per_output} | num_outputs={num_outputs}")
    _log(callback, f"⚙️  fps={fps} | resolution={resolution} | bitrate={bitrate} | move_used={move_to_used_flag}")

    # ---------- Validate ----------
    if not main_folder or not os.path.isdir(main_folder):
        _log(callback, "❌ ไม่พบโฟลเดอร์หลัก")
        return 0, 0

    output_folder = output_folder or main_folder
    os.makedirs(output_folder, exist_ok=True)

    if files_per_output <= 0 or num_outputs <= 0:
        _log(callback, "❌ files_per_output และ num_outputs ต้องมากกว่า 0")
        return 0, 0

    # ---------- Discover clips ----------
    if mode == "flat":
        all_picks = [_list_videos(main_folder, exclude_used=True)]
        _log(callback, f"📁 flat mode: พบ {len(all_picks[0])} ไฟล์ในโฟลเดอร์หลัก")
    else:
        # subfolder mode
        subfolders = [
            f for f in os.listdir(main_folder)
            if os.path.isdir(os.path.join(main_folder, f)) and f != "Used"
        ]
        if not subfolders:
            _log(callback, "❌ ไม่พบ sub-folder ในโฟลเดอร์หลัก")
            return 0, 0
        # เรียง folder ตาม natural key
        subfolders = sorted(subfolders, key=_natural_key)
        if folder_shuffle_mode == "shuffle":
            random.shuffle(subfolders)
        all_picks = []
        for sf in subfolders:
            sf_path = os.path.join(main_folder, sf)
            vids = _list_videos(sf_path, exclude_used=True)
            if not vids:
                _log(callback, f"⚠️  {sf}/ ว่างเปล่า (skip)")
                continue
            vids = _shuffle_or_sequential(vids, file_shuffle_mode)
            all_picks.append(vids)
            _log(callback, f"📁 {sf}/ → {len(vids)} ไฟล์")

    if not all_picks:
        _log(callback, "❌ ไม่มีไฟล์วิดีโอให้ใช้งาน")
        return 0, 0

    # ---------- Build outputs ----------
    success = 0
    fail = 0
    ts_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")

    for out_idx in range(1, num_outputs + 1):
        try:
            # รวบรวมคลิป: หมุนเวียน sub-folder → หยิบ 1 ไฟล์ต่อ folder
            chosen: List[str] = []
            pick_iterators = [list(p) for p in all_picks]
            # สลับ folder ทุกไฟล์ เพื่อกระจาย sub-folder ใน output เดียวกัน
            for round_i in range(files_per_output):
                progressed = False
                for i, it in enumerate(pick_iterators):
                    if len(chosen) >= files_per_output:
                        break
                    if it:
                        chosen.append(it.pop(0))
                        progressed = True
                        if not it:
                            _log(callback, f"⚠️  sub-folder #{i} หมดแล้ว")
                if not progressed:
                    # ทุก sub-folder หมดแล้ว → หยุด
                    _log(callback, f"⚠️  คลิปหมดก่อนครบ {files_per_output} ไฟล์ (ได้ {len(chosen)})")
                    break

            if not chosen:
                _log(callback, f"[{out_idx}/{num_outputs}] ⏭️  ข้าม (ไม่มีคลิป)")
                continue

            out_path = os.path.join(
                output_folder,
                f"podcast_{out_idx:03d}_{ts_prefix}.mp4",
            )

            _log(callback,
                 f"[{out_idx}/{num_outputs}] 🎬 กำลังรวม {len(chosen)} คลิป → {os.path.basename(out_path)}")

            _concat_clips(
                clips=chosen,
                out_path=out_path,
                fps=fps,
                resolution=resolution,
                bitrate=bitrate,
                ffmpeg_cmd=ffmpeg_cmd,
                ffprobe_cmd=ffprobe_cmd,
                callback=callback,
            )

            if not os.path.exists(out_path):
                raise RuntimeError("output file ไม่ถูกสร้าง")

            _log(callback, f"[{out_idx}/{num_outputs}] ✅ บันทึก: {out_path}")
            success += 1

            # ---------- Move to Used ----------
            if move_to_used_flag:
                _move_used(chosen, callback=callback)

        except Exception as ex:
            _log(callback, f"[{out_idx}/{num_outputs}] ❌ Error: {ex}")
            fail += 1

    _log(callback, f"🏁 เสร็จสิ้น! สำเร็จ: {success}, ล้มเหลว: {fail}")
    return success, fail


# ==================== ffmpeg concat ====================

def _concat_clips(
    clips: List[str],
    out_path: str,
    fps: Optional[str],
    resolution: Optional[Tuple[int, int]],
    bitrate: Optional[str],
    ffmpeg_cmd: str,
    ffprobe_cmd: str,
    callback: Optional[Callable[[str], None]],
) -> None:
    """ต่อคลิปหลายไฟล์เป็น 1 mp4 ใช้ filter_complex concat (รองรับ resize + fps)"""
    n = len(clips)
    w, h = (resolution if resolution else (None, None))

    # ตรวจ audio: ถ้าทุกคลิปมี audio → concat เสียงด้วย
    has_audio_list = [_has_audio_stream(c, ffprobe_cmd) for c in clips]
    all_have_audio = all(has_audio_list)
    some_have_audio = any(has_audio_list)

    cmd: List[str] = [ffmpeg_cmd, "-hide_banner", "-loglevel", "warning", "-y"]
    for c in clips:
        cmd += ["-i", c]

    fc_parts: List[str] = []

    # Scale + pad (ถ้ามี resolution) + setsar ทุก input
    for i in range(n):
        if w and h:
            vf = (
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            )
        else:
            vf = "setsar=1"
        if fps:
            vf = f"fps={fps},{vf}"
        fc_parts.append(f"[{i}:v]{vf}[v{i}]")

    # Concat video
    concat_v = "".join(f"[v{i}]" for i in range(n))
    fc_parts.append(f"{concat_v}concat=n={n}:v=1:a=0[v]")

    # Audio: ถ้าทุกคลิปมีเสียง → concat a=1, ถ้าไม่มีเลย → a=0, ถ้าระคน → ใช้เสียงคลิปแรก
    if all_have_audio:
        concat_a = "".join(f"[{i}:a]" for i in range(n))
        # normalize ทุก stream ก่อน concat เพื่อลดปัญหา sample rate
        for i in range(n):
            fc_parts.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        concat_a_norm = "".join(f"[a{i}]" for i in range(n))
        fc_parts.append(f"{concat_a_norm}concat=n={n}:v=0:a=1[a]")
        audio_map = "[a]"
    elif not some_have_audio:
        audio_map = None
    else:
        # mixed: ใช้เสียงจากคลิปแรกที่มีเสียง + silent สำหรับอันอื่น
        # สร้าง silent audio streams
        for i in range(n):
            if has_audio_list[i]:
                fc_parts.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
            else:
                # หา duration ของ video stream
                try:
                    dur = _ffprobe_duration(clips[i], ffprobe_cmd)
                except Exception:
                    dur = 60.0  # fallback
                fc_parts.append(
                    f"anullsrc=r=48000:cl=stereo[d{i}];"
                    f"[d{i}]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]"
                )
        concat_a_norm = "".join(f"[a{i}]" for i in range(n))
        fc_parts.append(f"{concat_a_norm}concat=n={n}:v=0:a=1[a]")
        audio_map = "[a]"

    cmd += [
        "-filter_complex", ";".join(fc_parts),
        "-map", "[v]",
    ]
    if audio_map:
        cmd += ["-map", audio_map]
    cmd += [
        "-c:v", "libx264",
        "-preset", "slow",
        "-pix_fmt", "yuv420p",
    ]
    if bitrate:
        cmd += ["-b:v", bitrate]
    else:
        cmd += ["-crf", "18"]
    if audio_map:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    cmd += [
        "-shortest" if audio_map else "",  # ไม่มี audio ไม่ต้องใส่ -shortest
        "-movflags", "+faststart",
        out_path,
    ]
    # กรอง empty string ที่อาจหลงมา
    cmd = [c for c in cmd if c]

    _log(callback, f"[ffmpeg] {subprocess.list2cmdline(cmd)[:300]}{'…' if len(subprocess.list2cmdline(cmd)) > 300 else ''}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else f"Exit {result.returncode}"
        raise RuntimeError(f"ffmpeg concat failed: {err[:500]}")


# ==================== Move to Used ====================

def _move_used(
    files: List[str],
    callback: Optional[Callable[[str], None]],
) -> None:
    """ย้ายไฟล์ที่ใช้แล้วไปยัง {parent}/Used/"""
    for f in files:
        try:
            parent = os.path.dirname(f)
            name = os.path.basename(f)
            used_dir = os.path.join(parent, "Used")
            os.makedirs(used_dir, exist_ok=True)
            dest = os.path.join(used_dir, name)
            # ถ้ามีไฟล์ชื่อเดียวกันอยู่แล้ว → เติม suffix
            if os.path.exists(dest):
                stem, ext = os.path.splitext(name)
                i = 1
                while os.path.exists(os.path.join(used_dir, f"{stem}_{i}{ext}")):
                    i += 1
                dest = os.path.join(used_dir, f"{stem}_{i}{ext}")
            shutil.move(f, dest)
            _log(callback, f"📦 moved → Used/{os.path.basename(dest)}")
        except Exception as e:
            _log(callback, f"⚠️  move failed: {f}: {e}")
