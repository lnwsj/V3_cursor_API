# V3 Cursor API Documentation

ชุดเอกสารภาษาไทยสำหรับผู้ใช้ API, developer และ operator ของ `V3_cursor_API`

## เริ่มอ่านจากไฟล์ไหน

| บทบาท | เอกสาร |
|---|---|
| ผู้เรียก API | [`V3_CURSOR_API_USER_GUIDE_TH.md`](V3_CURSOR_API_USER_GUIDE_TH.md) |
| ผู้เลือก pipeline | [`V3_CURSOR_API_PIPELINE_GUIDE_TH.md`](V3_CURSOR_API_PIPELINE_GUIDE_TH.md) |
| ผู้ดูแล production | [`V3_CURSOR_API_OPERATIONS_RUNBOOK_TH.md`](V3_CURSOR_API_OPERATIONS_RUNBOOK_TH.md) |
| Developer | [`V3_CURSOR_API_DEVELOPMENT_GUIDE_TH.md`](V3_CURSOR_API_DEVELOPMENT_GUIDE_TH.md) |
| Current source audit | [`V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md`](V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md) |
| ศึกษาสถาปัตยกรรม | [`V3_CURSOR_API_DEEP_DIVE_TH.md`](V3_CURSOR_API_DEEP_DIVE_TH.md) |
| Benchmark Mac M4 | [`V3_MAC_M4_SPEED_BENCHMARK_TH.md`](V3_MAC_M4_SPEED_BENCHMARK_TH.md) |

รายงานหลักฐานจากการทดสอบอยู่ใน [`reports/`](reports/)

Repo docs master ตาม Global Operating Rules: [`Readme.md`](Readme.md) และ index ล่าสุดอยู่ที่ [`index/index.md`](index/index.md)

## Current Status

`refactor-base` ณ 2026-08-20 อยู่ในสถานะ **RELEASE BLOCKED** สำหรับ Gateway เนื่องจาก refactor router ยังมี lifespan, auth, upload และ worker-dispatch wiring ที่ต้องแก้และทำ E2E acceptance ใหม่

Production เป็นคนละ snapshot กับ source ปัจจุบัน:

| Snapshot | Marker | ใช้เพื่อ |
|---|---|---|
| Current source audit | `refactor-base / 25e1032` | ตรวจ code และวาง release gates |
| Production Gateway | `1.2.0 / f6299fa` | live operational reference |
| Production M4 Worker | `1.2.0 / aa671b5` | live M4 runtime; preferred `hevc_videotoolbox` |
| Historical M4 benchmark | `aa671b5` | H.264 acceptance result ก่อน encoder preference เปลี่ยน |

ห้ามนำผลจาก source HEAD, production หรือ historical report มาปะปนกันโดยไม่ระบุ snapshot

## Public surface ที่ตรวจล่าสุด

- หน้าเว็บ: `https://green.cutdee.com/`
- API proxy: `https://green.cutdee.com/v3api/`
- OpenAPI จริง: `https://green.cutdee.com/v3api/openapi.json`
- API health จริง: `https://green.cutdee.com/v3api/healthz`
- Root `/healthz` และ `/openapi.json` บน public host ตอบเป็น HTML ของ frontend; `/api/openapi.json` ไม่ใช่ OpenAPI URL
- หน้าเว็บใช้ `/api/...` สำหรับ render/job/output และ output/history ต้องผ่าน authentication

หลักฐานคู่ UI+API ล่าสุด: [`green_cutdee_project_restudy_20260820_095321`](reports/green_cutdee_project_restudy_20260820_095321/)

## Report Chronology

| ช่วงเวลา | ประเภท | วิธีอ่าน |
|---|---|---|
| 2026-08-18 | deep dive, remediation และ production studies | historical evidence; ไม่ใช่ current source contract |
| 2026-08-19 | Mac M4 H.264 optimization benchmark | historical performance snapshot ของ `aa671b5` |
| 2026-08-20 | Current State Audit | source/live parity และ release gates ล่าสุด |

รายงานเก่าควรเก็บเป็น immutable evidence และไม่แก้ผลเดิมทับด้วย runtime รุ่นใหม่ หากข้อสรุปถูก supersede ให้เพิ่ม current-state link แทน

## ภาพรวมระบบ

```text
Client
  -> Gateway : auth, upload, PostgreSQL job state, worker selection
  -> Worker  : bounded queue, pipeline dispatch, FFmpeg, local files
  -> Output  : worker local storage, download ผ่าน Gateway
```

- Gateway รับ public traffic ที่ port `8788`
- Worker รับ internal traffic ที่ port `8789`
- Public production มักมี nginx prefix `/v3api`
- Gateway ใช้ Bearer token หรือ session cookie สำหรับ user routes
- Gateway ใช้ `X-Cutdee-Internal` เฉพาะ gateway-to-worker และ admin routes
- Output จริงอยู่ที่ local storage ของ Worker ไม่ได้อยู่ใน PostgreSQL

## สถานะที่ควรรู้

สถานะ job ที่พบในระบบคือ `queued`, `running`, `cancelling`, `succeeded`, `partial`, `failed`, `cancelled`, `paused` และ `invalid_input`

`succeeded` เท่านั้นที่ถือว่าสำเร็จเต็มรูปแบบ ส่วน `partial` ต้องตรวจ output manifest และนโยบายของ caller เพิ่มเติม

## Source Of Truth

- Endpoint และ auth: `gateway/app/backend/main.py`, `worker/app/backend/main.py`
- Settings และ validation: `worker/app/backend/core/contract.py`
- Render/filter/encoder: `worker/app/backend/core/green_render.py`, `gpu_detector.py`, `media_probe.py`
- Pipeline behavior: `worker/app/backend/core/pipelines/`
- Deployment: `deploy/install.sh` และ service configuration ของแต่ละ host

README และรายงานเก่าอาจเป็น historical snapshot หากขัดกับ source หรือ health runtime ให้ยึด source, release artifact และผล acceptance ล่าสุด

สำหรับความขัดแย้งระหว่างเอกสารกับ source refactor ให้ดู [`V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md`](V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md) ก่อน

## ข้อห้ามด้านความลับ

- ใช้ `<API_KEY>`, `<INTERNAL_TOKEN>`, `<DB_PASSWORD>` ในตัวอย่างเท่านั้น
- ห้าม commit token, password, private key, cookie หรือข้อมูลผู้ใช้จริง
- อย่าใส่ internal worker URL หรือ filesystem path จริงใน public documentation
