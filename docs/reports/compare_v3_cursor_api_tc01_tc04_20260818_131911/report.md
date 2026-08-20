# เปรียบเทียบ V3_cursor กับ V3_cursor_API — TC01–TC04

## Verdict

**FAIL_WITH_RCA / ACCEPTANCE_BLOCKED**

Repo อ้างอิงมี contract และ source-level regression ของ TC01–TC04 ผ่าน 120/120 ในชุดทดสอบที่รันจริง แต่ target ยังไม่ parity และยังไม่มีหลักฐานคู่ Browser UI + live API ของ target จึงห้ามประกาศ PASS.

## Scope และ source of truth

- Project ID: greenpc-v3-cursor
- Project Name: SJ88 Green Screen
- Environment: local macOS
- Reference: /Users/sj88/Documents/codex/V3_cursor
- Reference revision: fdf50f00d67ec79a16f5014c9a15c35114e45dcb, branch mac_os, working tree มี dirty changes ที่เกี่ยวข้องกับ TC01 progress และ composition fallback
- Target: /Users/sj88/Documents/codex/V3_cursor_API
- Target revision: f3ef05e7a83448ac3283f92c6df668e6ac4fbe12, branch main
- Target origin: https://github.com/lnwsj/V3_cursor_API.git
- Machine: CPU=Apple M4|CORE=10|RAM=16GB|GPU=Apple M4 8-core|SSD=228GB

ผู้ใช้พิมพ์ path V3_cursor สองครั้งเหมือนกันทั้งเชิง filesystem; จาก cwd และ README/CLAUDE ที่ระบุว่า V3_cursor_API เป็น API cluster fork จึงตีความ target เป็น V3_cursor_API. ไม่มีการแก้ source code หรือ dirty files เดิมของ reference.

Notion source ที่อ่านก่อนทำงาน: Second Brain Operating Rules, https://www.notion.so/3435a17a475f818bae05c4dca1bb6aba. กติกาที่ใช้คือ project identity ก่อนเริ่ม, repo เป็นหลักฐานจริง, แยก source/reference ออกจาก target, และไม่ claim PASS เมื่อ paired evidence ไม่ครบ.

## AI Full Dev recording

- Preflight activity: `12057`, POST สำเร็จ HTTP 201; ปิดสถานะด้วย PATCH เป็น `blocked` สำเร็จ HTTP 200 เพราะ acceptance gate ของ target ยังไม่ผ่าน.
- ระหว่างวิเคราะห์: activity `12060`, POST สำเร็จ HTTP 201.
- Closeout: activity `12061`, POST สำเร็จ HTTP 201, status `completed_with_boundary` ตามผลจริงของรอบนี้.
- ความผิดพลาดที่ตรวจพบ: การ PATCH `completed_with_boundary` ไม่ผ่าน HTTP 400 เพราะ endpoint อนุญาตเฉพาะ `todo`, `in_progress`, `blocked`, `done`, `cancelled`; จึงแก้รายการ preflight เป็น `blocked` ด้วยค่าที่ระบบยอมรับ. Evidence: `logs/ai_full_dev.log`.

## Contract ที่ reference ยืนยัน

จาก reference docs/TC01_TC05_SUMMARY.md:

- TC01: Product -> Chroma -> Final, background บังคับ, cover/audio optional, 1 output ต่อ product (บรรทัด 19–32, 37–44)
- TC02: Reframe 7 lenses x composition -> Chroma, 7/14/21 final outputs, intermediate แยกใน reframe/ (บรรทัด 46–55)
- TC03: split ตาม segment_duration 0.5–600 วินาที, default 10, แล้ว Chroma, นับแยกต่อ product (บรรทัด 57–70)
- TC04: Reframe -> Batch split -> Chroma, audio เป็น final duration เมื่อมี audio, reframe workers default 3 และ cap ตาม contract (บรรทัด 72–89)

Reference regression ที่รันจริง:

- Command: python3 -m unittest -v tests.test_contract tests.test_contract_values tests.test_pipeline_results_tc01_tc02 tests.test_pipeline_results_tc03_tc04 tests.test_tc01_tc04_settings_validation tests.test_tc01_tc04_media_safety tests.test_tc01_tc04_seed_cancel_contract tests.test_tc04_audio_duration tests.test_path_utils_cross_platform
- Result: 120 tests, 120 OK, 0 failure
- Evidence: logs/reference_regression.log

## Matrix สรุป

| TC | Reference | Target | ผลเทียบ |
|---|---|---|---|
| TC01 | pipeline และ result truth ผ่าน tests; progress รองรับ object/number/mapping | ไม่มี mapping progress extractor; worker publish/download output contract ใช้ชื่อผิด | FAIL |
| TC02 | Reframe -> Chroma, cap parallel 3, portable stem, full output resolution | cap 10, parallel chroma optional, Path().stem บน Windows path, default intermediate 404x720 | FAIL |
| TC03 | pipeline source file byte-identical กับ reference | pipeline file เหมือน แต่ dry-run เป็น generic count และ download final batch_* ไม่ได้ | FAIL |
| TC04 | skip batch ยัง surface valid reframe outputs และ contract เป็น Reframe -> Batch -> Chroma | skip path ไม่คืน reframe outputs ที่ valid; API publication/download ยัง reject final batch_* | FAIL |

## Findings หลักแบบมีบรรทัด

### F01 — Contract cap ของ TC02/TC04 ผิด

- Reference core/contract.py:109 กำหนด REFRAME_PARALLEL_MAX = 3 และ validator ที่ 472 บังคับช่วง 1–3.
- Target worker/app/backend/core/contract.py:108–109 กำหนด max = 10.
- Target worker/app/backend/core/ai_reframe.py:1087–1089 clamp ที่ 10.
- Probe จริง: reference validate_parallel_4 ได้ ValueError; target คืนค่า 4.

ผลกระทบ: API ยอมรับ parallelism ที่ reference contract ห้าม, เพิ่ม resource contention/OOM และทำให้ TC02/TC04 ไม่ใช่ behavior เดียวกัน.

### F02 — Target ไม่มี fallback สำหรับค่า composition ที่เป็น null

- Reference working tree core/contract.py:751–767 รองรับ key ที่มีค่า None โดย fallback ไป default; probe คืน center/left/right.
- Target worker/app/backend/core/contract.py:769–786 ตรวจ key ที่มีอยู่ทันที; use_center=null ทำให้ ValueError.

ผลกระทบ: JSON/Form bridge ที่ส่ง null จาก UI หรือ legacy caller ถูก reject ใน target ทั้งที่ reference รับได้. นี่เป็น contract drift ไม่ใช่แค่ข้อความ error ต่างกัน.

### F03 — Target เพิ่ม 720p intermediate ที่ reference ไม่ใช้

- Target contract: worker/app/backend/core/contract.py:646–665 เพิ่ม reframe_short_side default 720.
- Target reframe: worker/app/backend/core/ai_reframe.py:521–525, 783–816 สร้าง scale 404:720 เมื่อ output contract คือ 1080x1920.
- Reference command probe สร้าง scale 1080:1920; target probe สร้าง scale 404:720.

ผลกระทบ: intermediate TC02/TC04 ไม่เท่ากับ reference และ feature นี้ไม่มีใน reference contract/docs ที่ใช้เป็น source of truth. จะยอมรับได้ก็ต่อเมื่อมี decision/spec และ real media proof ใหม่; รอบนี้ไม่มี.

### F04 — Windows path portability ถูกถอยกลับ

- Reference core/path_utils.py มี portable_basename/portable_stem และ reference TC02 ใช้ portable_stem ที่ core/pipelines/tc02_reframe.py:124–130.
- Target ลบ helper เหล่านี้ออกจาก worker/app/backend/core/path_utils.py; target ai_reframe.py:595–605 และ TC02:131–133 ใช้ Path/อาศัย host separator.
- Probe เดียวกันด้วย C:\\Users\\foo\\Bar-1.mp4:
  - reference output stem: Bar-1
  - target output filename มี C:\\Users\\foo\\Bar-1 ติดอยู่

ผลกระทบ: client ที่ส่ง Windows-style path หรือ fixture ข้าม platform ได้ชื่อ output ไม่ตรง contract และอาจสร้างไฟล์ชื่อผิดบน Unix.

### F05 — TC01 progress callback ไม่รับ mapping

- Reference core/pipelines/tc01_chroma.py:128–144 มี _extract_progress_pct รองรับ pct attribute, number และ mapping["pct"].
- Target worker/app/backend/core/pipelines/tc01_chroma.py:591–598 ใช้ getattr/number inline; dict ที่มี pct จะกลายเป็น 0.0.

ผลกระทบ: final media อาจยัง render ได้ แต่ progress evidence/status ไม่ตรงกับงานจริง จึงไม่ผ่าน UI/API consistency gate.

### F06 — Target เพิ่ม parallel TC02 แบบไม่อยู่ใน reference และทำ order/progress แตกต่าง

- Target worker/app/backend/core/pipelines/tc02_reframe.py:410–473 เปิด ThreadPoolExecutor เมื่อ V3_TC02_PARALLEL > 1 และเรียง final_outputs ตาม as_completed ไม่ใช่ task order.
- Target ส่ง chroma_max_parallel ที่บรรทัด 432–444 และปิด per-item progress ใน parallel mode.
- Reference TC02 ใช้ sequential final chroma path ที่ core/pipelines/tc02_reframe.py:407 เป็นต้น.

ผลกระทบ: output ordering, progress trace และ resource budget แตกต่าง; default env 1 ไม่ได้ลบ code-path ที่ผิดเมื่อ deployment ตั้งค่า >1.

### F07 — TC04 skip path สูญเสีย valid reframe outputs

- Reference core/pipelines/tc04_rebatch.py:314–344 surface valid reframe outputs ใน top-level PipelineResult และปรับ expected/succeeded เพื่อไม่รายงาน 0 ทั้งที่มี MP4 valid.
- Target worker/app/backend/core/pipelines/tc04_rebatch.py:314–326 คืน PipelineResult โดยไม่มี outputs และ succeeded ของ reframe.

ผลกระทบ: partial/intermediate evidence และ status ที่ API ส่งกลับไม่สะท้อน valid reframe stage.

### F08 — Worker download whitelist ทำให้ final ของ TC01–TC04 ดาวน์โหลดไม่ได้

- Target worker/app/backend/main.py:414–425 ค้น output เฉพาะ .mp4 ที่อยู่ direct job directory.
- Pipeline names ของ TC01/TC02/TC03/TC04 เป็น *_single_*, *__tc02_chroma_*, และ batch_* ไม่ใช่ output_*.
- Target worker/app/backend/main.py:515–520 ปฏิเสธทุกชื่อที่ไม่ขึ้นต้น output_.
- Runtime probe สร้าง batch_*.mp4 จริงใน job dir แล้วเรียก get_output ได้ HTTPException: output not found; output_legacy.mp4 เท่านั้นที่คืน FileResponse.

ผลกระทบ: ต่อให้ pipeline result เป็น success, gateway download route ที่ 749–782 จะ proxy แล้วได้ 404. นี่เป็น critical API publication bug ครอบคลุม TC01–TC04.

### F09 — Gateway download alias ใช้ job id เป็น underscore

- Target gateway/app/backend/main.py:962–1003 รองรับ /api/download แต่เรียก worker ที่ /v1/jobs/_/output (บรรทัด 995–997).
- Worker ต้องการ job_id จริงตาม route ที่ worker/app/backend/main.py:515.

ผลกระทบ: alias download นี้ fail deterministic แม้ DB หา worker ได้.

### F10 — Dry-run ไม่คำนวณ contract ของ TC01–TC04

- Target gateway/app/backend/main.py:1180–1203 ตั้ง planned_output_count/final_count เท่ากับจำนวน products, composition_count=1, reframe_per_source=1 สำหรับทุก TC.
- ตาม reference contract TC02 ต้อง 7/14/21, TC03 ต้องคำนวณ ceil(duration/segment_duration), TC04 ต้องคูณ reframe matrix กับ segment count.

ผลกระทบ: API plan/response ไม่สามารถใช้เป็น source of truth หรือคู่ consistency กับ UI ได้.

### F11 — Target worker generic runner ทิ้ง source_ids และไม่ใช่ full TC input bridge

- Target worker/app/backend/main.py:314–354 ประกาศ source_ids แต่ _build_tc_inputs คืนแค่ products/backgrounds/audios/covers.
- Runtime probe source_ids=['source_1'] ได้ inputs เป็น [] สี่ชุด.

เป็น bug ที่เห็นได้จาก generic runner และทำให้ claim TC05/TC06 ใน API README ยังไม่ครบ แม้รอบนี้ focus หลักคือ TC01–TC04.

### F12 — Source-level/API evidence และเอกสาร target ยังไม่พร้อมรับรอง

- Target มี Python source 55 ไฟล์ compile ผ่าน 55/55 แต่ไม่มี tests/ directory, ไม่มี frontend และไม่มี docs/reports evidence bundle ใน repo target.
- README.md:185–198 ระบุ roadmap ว่า multi-mode TC02/TC03 ยัง “currently TC01 only” ขัดกับ endpoint/pipeline ที่ประกาศ TC01–TC06.
- Gateway import ใน environment นี้รันไม่ได้เพราะ ModuleNotFoundError: psycopg2. จึงยังไม่มี live API request/response.
- ไม่มี target Browser UI ให้ capture 5 checkpoints; ตาม hard gate ทุก pair จึงเป็น FAIL_INCOMPLETE ไม่ใช่ PASS.

## RCA classification

1. code/contract drift: target vendored core ไม่ sync กับ reference fixes (F01–F07)
2. API orchestration/publication defect: generic runner/download/dry-run ไม่รักษา output และ plan contract (F08–F11)
3. runtime/toolchain gap: gateway dependency ไม่พร้อม และไม่มี target UI/evidence harness (F12)
4. external: ไม่พบ external blocker ที่ต้อง owner approval; blocker รอบนี้เป็น code/runtime ของ target เอง

## Evidence gate

- Reference source regression: 120/120 PASS
- Target source compile probe: 55/55 PASS
- Target live API: NOT_EVALUATED — gateway import ขาด psycopg2
- Target Browser UI: NOT_EVALUATED — repo ไม่มี UI
- Browser screenshots per TC: 0/20; missing by design, ห้ามสร้างภาพจำลองเป็นหลักฐาน
- API request/response/timing files: มีไฟล์สถานะ NOT_RUN ใน api/ แต่ไม่ใช่ live HTTP evidence
- Pair completeness: 0%
- Pair consistency: 0%
- Critical errors: 5 (parallel contract, path/scale drift, output download, download alias, dry-run plan)
- Overall: FAIL_WITH_RCA_AND_ACCEPTANCE_BLOCKER

## Corrective plan ก่อน rerun

1. Freeze reference commit/working-tree decision แล้วกำหนด source-of-truth commit สำหรับ target อย่างชัดเจน.
2. Sync contract/pipelines ที่เกี่ยวข้องแบบ scoped: parallel cap 3, None fallback, progress extractor, portable path, TC04 reframe-output truth; อย่ายก feature 720p/parallel ใหม่เข้ามาโดยไม่มี spec.
3. แก้ worker output registry/download ให้รองรับทุก valid output path ของ TC01–TC04 และแก้ /api/download ให้ส่ง job_id จริง.
4. แก้ dry-run ให้ใช้ contract planner จริงของแต่ละ TC และส่งค่าที่ UI ใช้ถึง pipeline.
5. แก้/ทดสอบ generic input bridge แล้วเพิ่ม tests ใน target; ติดตั้ง dependency จาก requirements ใน isolated environment โดยไม่แตะ production.
6. สร้าง live target UI หรือระบุ UI client ที่เป็น source of truth แล้วรันแต่ละ TC ด้วย Browser screenshot 5 จุด + API request/response/timing + pair binding.
7. Rerun จน pair completeness/consistency 100%, checks_passed=checks_total, critical_errors=0; ถ้าขาด UI ให้คงสถานะ BLOCKED.

## Artifact index

- report.md: ไฟล์นี้
- report.html: report แบบ HTML
- summary.json: verdict และ gate metrics
- test_matrix.json: TC01–TC04 matrix
- api/TC01..TC04/: request/response/timing ที่ระบุ NOT_RUN อย่างตรงไปตรงมา
- pairs/: binding ของทุก TC; ทุกคู่ FAIL_INCOMPLETE เพราะไม่มี target UI/live API
- logs/: regression, probes, import blocker และ hash inventory
- logs/ai_full_dev.log: หลักฐานการบันทึก preflight/ระหว่างวิเคราะห์/closeout และการแก้ status
- screenshots/: ว่างโดยเจตนา; ไม่ปลอมหลักฐาน UI
