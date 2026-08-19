# V3 Cursor API Documentation

ชุดเอกสารภาษาไทยสำหรับผู้ใช้ API, developer และ operator ของ `V3_cursor_API`

## เริ่มอ่านจากไฟล์ไหน

| บทบาท | เอกสาร |
|---|---|
| ผู้เรียก API | [`V3_CURSOR_API_USER_GUIDE_TH.md`](V3_CURSOR_API_USER_GUIDE_TH.md) |
| ผู้เลือก pipeline | [`V3_CURSOR_API_PIPELINE_GUIDE_TH.md`](V3_CURSOR_API_PIPELINE_GUIDE_TH.md) |
| ผู้ดูแล production | [`V3_CURSOR_API_OPERATIONS_RUNBOOK_TH.md`](V3_CURSOR_API_OPERATIONS_RUNBOOK_TH.md) |
| Developer | [`V3_CURSOR_API_DEVELOPMENT_GUIDE_TH.md`](V3_CURSOR_API_DEVELOPMENT_GUIDE_TH.md) |
| ศึกษาสถาปัตยกรรม | [`V3_CURSOR_API_DEEP_DIVE_TH.md`](V3_CURSOR_API_DEEP_DIVE_TH.md) |
| Benchmark Mac M4 | [`V3_MAC_M4_SPEED_BENCHMARK_TH.md`](V3_MAC_M4_SPEED_BENCHMARK_TH.md) |

รายงานหลักฐานจากการทดสอบอยู่ใน [`reports/`](reports/)

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

## ข้อห้ามด้านความลับ

- ใช้ `<API_KEY>`, `<INTERNAL_TOKEN>`, `<DB_PASSWORD>` ในตัวอย่างเท่านั้น
- ห้าม commit token, password, private key, cookie หรือข้อมูลผู้ใช้จริง
- อย่าใส่ internal worker URL หรือ filesystem path จริงใน public documentation
