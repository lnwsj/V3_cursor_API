# V3_cursor_API: เอกสารศึกษาเชิงลึกและแผนฟื้นฟูระบบ

เอกสารฉบับนี้เป็นเอกสารศึกษาระบบแบบ end-to-end ของ `V3_cursor_API` ตั้งแต่ source code, pipeline, API contract, persistence, deployment, production surface, cluster workers, reverse tunnels, การทดสอบ และแผนแก้ไขก่อนเปิดใช้งานจริง

เอกสารนี้เขียนจากหลักฐาน 4 กลุ่ม:

1. Source ใน repository `/Users/sj88/Documents/codex/V3_cursor_API`
2. ประวัติ Git และ working tree ณ วันที่ 2026-08-18
3. รายงาน static/runtime/browser ที่อยู่ใน `docs/reports/`
4. การตรวจสอบ live infrastructure แบบ read-only ผ่าน public health endpoints และ SSH read-only commands

ไม่มีการบันทึกข้อมูลลับลงในเอกสารนี้ และไม่มีการเริ่ม render, upload, TTS generation, restart หรือหยุด service ในรอบการศึกษา

---

## 1. สรุปสำหรับผู้บริหาร

### 1.1 ระบบนี้คืออะไร

`V3_cursor_API` เป็น API cluster สำหรับประมวลผลวิดีโอ green screen โดยแยกหน้าที่เป็น:

- Gateway: รับ request, รับไฟล์, เก็บ job metadata, เลือก worker และ proxy output
- Worker: รับไฟล์, เรียก pipeline TC01-TC06, เรียก FFmpeg และคืนผลลัพธ์
- Core: ชุด render engine ที่คัดลอกจาก `V3_cursor` เดิม ซึ่งมี chroma key, reframe, batch, audio master, GPU detection และ checkpoint
- PostgreSQL: เก็บสถานะ job ฝั่ง Gateway
- Local job directory: เก็บ input/output จริงฝั่ง Worker

### 1.2 Verdict ปัจจุบัน

ระบบมี source code และ deployment ที่ทำงานได้บางส่วน แต่ยังไม่ควรประกาศ production parity หรือ render acceptance เต็มรูปแบบ เนื่องจากมีปัญหาหลักดังนี้:

1. Worker render แบบ synchronous ทำให้ `/health` และ `/status` ตอบไม่ได้ระหว่าง render
2. Worker แต่ละเครื่องใช้ revision และ dirty state ไม่เหมือนกัน
3. Gateway กับ Worker มี output/status/input contract ไม่ตรงกัน
4. TC05 และ TC06 ยังไม่มี input bridge ที่ครบจาก API ถึง pipeline
5. Output บางชนิดถูกค้นพบผิดหรือดาวน์โหลดไม่ได้
6. Dry-run ของ Gateway ไม่ได้คำนวณตาม contract จริงของ TC01-TC06
7. Source ใน repository, source ที่ deploy บน worker และ public production gateway เป็นคนละ generation

### 1.3 สิ่งที่ควรทำก่อน

สิ่งแรกไม่ใช่การ tune GPU หรือเพิ่ม parallelism แต่คือ:

1. Freeze baseline และเก็บ inventory ของ worker ทุกเครื่อง
2. แยก FFmpeg render ออกจาก FastAPI event loop
3. กำหนด Gateway-Worker contract กลาง
4. สร้าง release artifact เดียวแล้ว deploy แบบ canary

เหตุผลคือ ถ้ายังมี worker หลาย revision และ health ถูกบล็อก จะไม่สามารถระบุสาเหตุของผลลัพธ์หรือวัดผลการแก้ไขได้อย่างน่าเชื่อถือ

---

## 2. ขอบเขตและวิธีอ่านเอกสาร

### 2.1 แหล่งอ้างอิงหลัก

| ประเภท | แหล่งอ้างอิง | ความหมาย |
|---|---|---|
| SOURCE | `gateway/app/backend/main.py` | Gateway ที่อยู่ใน working tree |
| SOURCE | `worker/app/backend/main.py` | Worker ที่อยู่ใน working tree |
| SOURCE | `worker/app/backend/core/` | Render engine และ pipeline |
| DEPLOY | `deploy/install.sh` | วิธีติดตั้ง systemd, venv, data directory และ tunnel |
| DOC | `README.md`, `CLAUDE.md` | Intended architecture และวิธีรัน |
| REFERENCE | `/Users/sj88/Documents/codex/V3_cursor` | Source of truth เดิมของ V3 GUI/pipeline |
| REPORT | `docs/reports/compare_v3_cursor_api_tc01_tc04_20260818_131911/` | ผลเปรียบเทียบ TC01-TC04 |
| REPORT | `docs/reports/green_cutdee_dedicated_server_study_20260818_133756/` | ผลศึกษา production read-only เดิม |
| LIVE | Public health/API และ SSH read-only | สถานะ runtime ที่ตรวจพบจริง |

### 2.2 สถานะของข้อสรุป

- **ยืนยันจาก source**: อ่านจากไฟล์ใน repository โดยตรง
- **ยืนยันจาก runtime**: เรียก endpoint หรืออ่าน service/process แบบ read-only แล้วได้ผลตรงกัน
- **อ้างอิงจากรายงานเดิม**: เกิดขึ้นในรอบตรวจสอบก่อนหน้าและควรใช้เป็น historical evidence
- **ข้อสังเกตเชิงสถาปัตยกรรม**: เป็นผลวิเคราะห์จาก flow และข้อจำกัดของ implementation
- **ยังไม่ประเมิน**: ยังไม่มี real media acceptance หรือไม่ได้ส่ง mutation request

### 2.3 Snapshot Git ก่อนสร้างเอกสาร

ข้อมูลสำคัญ ณ ตอนเริ่มงานเอกสาร:

- Branch: `main`
- Local HEAD: `a4110db`
- Upstream `origin/main` ก่อน commit เอกสาร: `f3ef05e`
- Local working tree มีการแก้โค้ดค้างอยู่หลายไฟล์
- เอกสารนี้จะถูก commit แยกโดยไม่ stage โค้ดที่ไม่ได้เป็นงานเอกสาร

ไฟล์โค้ดที่พบว่ามีการเปลี่ยนค้างก่อนทำเอกสาร:

- `gateway/app/backend/main.py`
- `worker/app/backend/core/ai_reframe.py`
- `worker/app/backend/core/gpu_detector.py`
- `worker/app/backend/core/green_render.py`
- `worker/app/backend/core/media_probe.py`

ไฟล์ untracked ที่พบก่อนทำเอกสาร:

- `.playwright-mcp/`
- `CLAUDE.md`
- `docs/` ซึ่งมีรายงานและ evidence bundle อยู่แล้ว

---

## 3. โครงสร้าง repository

```text
V3_cursor_API/
├── gateway/
│   ├── app/backend/main.py
│   └── requirements.txt
├── worker/
│   ├── app/backend/main.py
│   ├── requirements.txt
│   └── app/backend/core/
│       ├── pipelines/
│       │   ├── _common.py
│       │   ├── tc01_chroma.py
│       │   ├── tc02_reframe.py
│       │   ├── tc03_batch.py
│       │   ├── tc04_rebatch.py
│       │   ├── tc05_reframe_only.py
│       │   └── tc06_video_loop.py
│       ├── contract.py
│       ├── green_render.py
│       ├── ai_reframe.py
│       ├── batch_pingpong.py
│       ├── audio_master.py
│       ├── ffmpeg_runner.py
│       ├── gpu_detector.py
│       ├── media_probe.py
│       ├── render_checkpoint.py
│       ├── tc06_products.py
│       └── _legacy/
├── deploy/
│   └── install.sh
├── docs/
│   ├── reports/
│   └── V3_CURSOR_API_DEEP_DIVE_TH.md
├── README.md
└── CLAUDE.md
```

### 3.1 โค้ด active และโค้ด legacy

โค้ดที่ Worker ใช้จริงผ่าน `worker/app/backend/main.py` คือ:

- `core.green_render`
- `core.gpu_detector`
- `core.media_probe`
- `core.ffmpeg_runner`
- `core.pipelines`
- `core.contract`
- `core.ai_reframe`
- `core.batch_pingpong`
- `core.audio_master`
- `core.tc06_products`

โค้ดใต้ `core/_legacy/` เป็น V2 หรือ compatibility code เช่น `video_editor`, `vdo_long`, `podcast_engine`, `auto_mv` และ `portable_tc_runner` ไม่ใช่เส้นทางหลักของ API Worker ปัจจุบัน

### 3.2 จุดเริ่มต้นการทำงาน

| Entry point | หน้าที่ | Default port |
|---|---|---:|
| `gateway.app.backend.main:app` | FastAPI Gateway | 8788 |
| `app.backend.main:app` ใน worker | FastAPI Worker | 8789 |
| `core/cli_runner.py` | CLI ที่ reuse pipeline เดียวกับ core | ไม่กำหนดตายตัว |
| `deploy/install.sh` | ติดตั้ง venv, systemd และ data dirs | ไม่ใช่ service |

### 3.3 Dependency ที่ตรวจพบ

Gateway:

- `fastapi==0.115`
- `uvicorn[standard]==0.30`
- `httpx==0.27`
- `pydantic==2.9`
- `psycopg2-binary==2.9.12`

Worker:

- `fastapi==0.115`
- `uvicorn[standard]==0.30`
- `pydantic==2.9`

ข้อสังเกต:

- Gateway ใช้ `UploadFile`, `File`, `Form` แต่ `python-multipart` ไม่ได้ระบุใน requirements
- FFmpeg และ FFprobe เป็น external runtime dependency ไม่ได้ติดตั้งโดย requirements
- PyTorch, PIL หรือ library เสริมบางตัวเป็น optional path ใน core และไม่ได้อยู่ใน requirements หลัก
- `psycopg2` ไม่มีใน Python environment ที่ใช้ตรวจ local ทำให้ import Gateway ไม่ผ่านจนกว่าจะติดตั้ง dependency

---

## 4. สถาปัตยกรรมระบบ

### 4.1 Intended architecture ตาม README

```text
Client / V3 UI
       │
       ▼
Nginx /v3api/
       │
       ▼
Gateway :8788
       ├── PostgreSQL / PgBouncer
       ├── GATEWAY_DATA_DIR/uploads/
       ├── GATEWAY_DATA_DIR/outputs/
       └── workers.json
              │ HTTP + X-Cutdee-Internal
              ▼
       Worker :8789 x N
              ├── jobs/{job_id}/ inputs
              ├── jobs/{job_id}/ outputs
              ├── FFmpeg / FFprobe
              └── GPU encoder or libx264
```

### 4.2 Request flow ที่ source ตั้งใจทำ

1. Client upload product/background/audio/cover/source ไป Gateway
2. Gateway สร้าง file id และเก็บ bytes ใน local upload cache
3. Client สร้าง job หรือส่ง V3 render request
4. Gateway insert job status `queued` ลง PostgreSQL
5. Gateway เลือก worker ที่ healthy และ active count ต่ำกว่า capacity
6. Gateway forward bytes ไป worker
7. Gateway ส่ง render payload ไป worker
8. Worker เรียก pipeline synchronously
9. Pipeline เรียก FFmpeg ตาม stage
10. Worker สแกน output และคืน result
11. Gateway update PostgreSQL
12. Client poll job status
13. Gateway proxy download จาก worker และส่งกลับ client

### 4.3 Runtime flow ที่เกิดขึ้นจริง

ใน implementation ปัจจุบันข้อ 8 เกิดภายใน async FastAPI handler โดยตรง ไม่ได้ส่งเข้า queue แยก ดังนั้น event loop ของ Worker สามารถถูกบล็อกตั้งแต่เริ่ม pipeline จน FFmpeg จบ

ผลที่ตามมา:

- `/health` อาจ timeout ระหว่าง render
- `/status` อาจ timeout ระหว่าง render
- Gateway health poll มอง worker เป็น unhealthy ทั้งที่ process ยังทำงาน
- request อื่น ๆ รอคิวใน socket หรือ timeout
- cancel ที่เปลี่ยนแค่ DB ไม่ได้หยุด subprocess จริง
- active job count ไม่ตรงกับงานที่กำลังทำจริง

### 4.4 ความแตกต่างระหว่าง “service active” กับ “worker healthy”

ต้องแยกสถานะเหล่านี้:

| สถานะ | ความหมาย |
|---|---|
| systemd/launchd active | process หลักยังอยู่ |
| TCP listen | port ถูก bind แล้ว |
| HTTP health responsive | event loop ตอบ request ได้ |
| pipeline active | มีงาน FFmpeg กำลังทำงาน |
| job result valid | output ผ่าน media validation และ count invariant |

Runtime ที่ตรวจพบกับ RTX2050 แสดงให้เห็นว่า service active และ TCP listen ไม่ได้แปลว่า HTTP health responsive

---

## 5. Gateway รายละเอียด

ไฟล์หลัก: `gateway/app/backend/main.py`

### 5.1 หน้าที่ของ Gateway

- อ่าน environment configuration
- สร้าง `GATEWAY_DATA_DIR`
- โหลด `workers.json`
- สร้าง/migrate ตาราง `v3_jobs`
- รับ upload และเก็บ bytes
- ตรวจ authentication ตาม working-tree revision
- เลือก worker
- ส่ง RPC ไป Worker ผ่าน HTTP
- เก็บ job metadata ใน PostgreSQL
- ทำ health aggregation
- proxy output และสร้าง compatibility response ให้ V3 UI

### 5.2 Endpoint families

#### Liveness และ cluster

| Method | Path | หน้าที่ | สถานะเชิงสถาปัตยกรรม |
|---|---|---|---|
| GET | `/healthz` | Gateway liveness | ไม่ควรเรียก worker หนัก |
| GET | `/api/health` | Aggregated health และ encoder summary | ต้องตั้ง timeout ต่อ worker |
| GET | `/api/cluster/health` | สรุป worker slots/capacity | production ใหม่ลด metadata แล้ว |
| GET | `/api/version` | version และ Python runtime | ใช้ตรวจ release parity |
| GET | `/api/ffmpeg` | FFmpeg info จาก worker | ควรผูกกับ worker id |
| GET | `/api/encoders` | encoder aggregation | ขึ้นกับ worker health |
| GET | `/api/lens` | lens catalog | ค่า hard-coded จาก V3 defaults |
| GET | `/api/config` | supported TCs และ cluster config | ต้อง sync กับ UI |

#### Legacy v1 API

| Method | Path | หน้าที่ |
|---|---|---|
| POST | `/api/v1/uploads/{role}` | upload product/background/cover/audio |
| POST | `/api/v1/jobs` | สร้างและ render job แบบ legacy |
| GET | `/api/v1/jobs/{id}` | อ่านสถานะ job |
| GET | `/api/v1/jobs/{id}/download/{filename}` | download ผ่าน worker |

#### V3 UI-compatible API

| Method | Path | หน้าที่ |
|---|---|---|
| POST | `/api/render/{tc}` | multipart render สำหรับ TC01-TC06 |
| POST | `/api/jobs/upload` | upload ที่ UI ใช้ |
| GET | `/api/jobs/list` | รายการ jobs |
| GET | `/api/jobs/{id}` | job response แบบ V3 |
| GET | `/api/job/{id}` | singular alias |
| GET | `/api/job/{id}/thumbnails` | thumbnail/file metadata |
| GET | `/api/job/{id}/output` | compatibility output route |
| GET | `/api/job/{id}/download-all` | ZIP output ทั้ง job ใน working tree ใหม่ |
| GET | `/api/jobs/history` | history alias |
| POST | `/api/jobs/{id}/cancel` | เปลี่ยนสถานะ cancel |
| POST | `/api/jobs/{id}/pause` | ปัจจุบันยังไม่ implement จริง |
| POST | `/api/jobs/{id}/resume` | ปัจจุบันยังไม่ implement จริง |
| GET | `/api/outputs` | output gallery contract |
| GET | `/api/download/{path}` | authenticated output proxy |

#### Dynamic TC endpoints

Gateway สร้าง route แบบ dynamic สำหรับ:

- `POST /api/tc01/render`
- `POST /api/tc02/render`
- `POST /api/tc03/render`
- `POST /api/tc04/render`
- `POST /api/tc05/render`
- `POST /api/tc06/render`
- `POST /api/tc01/dry-run` ถึง `POST /api/tc06/dry-run`

Dry-run ชุดนี้ยังเป็น generic planner ใน source และไม่ reuse planner เต็มของ `core/cli_runner.py`

### 5.3 Worker selection

ฟังก์ชันหลักคือ `_pick_worker`

เจตนา:

- ข้าม worker ที่ disabled
- ข้าม worker ที่ไม่ healthy
- ข้าม worker ที่ active ถึง `max_concurrent`
- เลือก worker ที่ active ต่ำที่สุด

ข้อจำกัด:

- ไม่มี atomic reservation ก่อน dispatch
- ไม่มี increment/decrement active count ที่เชื่อถือได้
- health response บาง worker ไม่มี `active_jobs`
- การ dispatch หลาย request พร้อมกันสามารถเลือก worker เดียวกันซ้ำ
- capacity ใน `workers.json` ไม่เท่ากับ concurrency ที่ Worker บังคับจริง

### 5.4 PostgreSQL job state

Gateway ใช้ table ชื่อ `v3_jobs` ใน database ปัจจุบัน แม้เอกสารบางส่วนเรียกว่า schema `v3_jobs`

ข้อมูลที่ job ใช้โดยทั่วไป:

| Field | ความหมาย |
|---|---|
| `job_id` | public/internal job identifier |
| `user_id` | owner ของ job |
| `tc` | pipeline label |
| `status` | queued/running/succeeded/failed/cancelled ตาม revision |
| `progress` | numeric progress |
| `current_step` | stage ปัจจุบัน |
| `settings` | JSONB settings ที่ client ส่ง |
| `output_file` | primary output เดิม |
| `output_files` | output list สำหรับ multi-output |
| `output_size` | ขนาด output หรือ aggregate ตาม implementation |
| `worker_id` | worker ที่รับงาน |
| `started_at` | เวลาเริ่ม |
| `finished_at` | เวลาจบ |
| `log` | log list |
| `result` | pipeline result JSON |
| `error` | error message |

ข้อจำกัดเชิง persistence:

- Gateway update สถานะหลัง RPC จบเป็นหลัก จึงไม่มี progress แบบ live ที่เชื่อถือได้
- Worker `_JOBS` เป็น in-memory state และหายเมื่อ process restart
- Job ที่ Gateway insert แล้ว worker ตายอาจค้าง `queued` หรือ `running`
- Gateway upload cache ไม่ได้มี lifecycle cleanup ที่ชัดเจน
- Job output อยู่บน worker ทำให้ shared PostgreSQL อย่างเดียวไม่ทำให้ระบบ HA จริง

### 5.5 Auth model ใน source และ production

มี header สองประเภท:

- `Authorization`: public API bearer/session layer
- `X-Cutdee-Internal`: Gateway-to-Worker RPC และ cluster administration

สถานะที่ต้องแยกให้ชัด:

- Source baseline เดิมเคยมี anonymous UI-compatible mutation routes
- Working tree ปัจจุบันเพิ่ม configured API keys, HTTP bearer, session cookie และ ownership checks
- Public production OpenAPI ที่ตรวจล่าสุดประกาศ `HTTPBearer` สำหรับ output/config/render/upload
- Worker internal routes ยังต้องตรวจ internal header

ดังนั้น auth remediation ใน working tree และ production ต้องถูก commit/release และทดสอบคู่กัน ไม่ควรดูจาก source file เดียว

---

## 6. Worker รายละเอียด

ไฟล์หลัก: `worker/app/backend/main.py`

### 6.1 Worker responsibilities

- สร้าง data directory และ job directory
- ตรวจ internal header
- รับ upload ตาม role
- สร้าง `TCRenderRequest`
- map request ไป `PipelineInputs`
- เรียก `render_tc01` ถึง `render_tc06`
- เก็บ status/log ใน `_JOBS`
- ค้นหา output ใน job directory
- คืน status และ file response
- ทำ cleanup ตามอายุ job

### 6.2 Worker routes

| Method | Path | หน้าที่ |
|---|---|---|
| GET | `/health` | health และ encoder summary |
| GET | `/v1/capabilities` | capability พร้อม internal auth |
| POST | `/v1/jobs/{id}/upload/{role}` | รับไฟล์ตาม role |
| POST | `/v1/jobs/{id}/render` | legacy TC01 render |
| POST | `/v1/tc01/render/{id}` | pipeline TC01 |
| POST | `/v1/tc02/render/{id}` | pipeline TC02 |
| POST | `/v1/tc03/render/{id}` | pipeline TC03 |
| POST | `/v1/tc04/render/{id}` | pipeline TC04 |
| POST | `/v1/tc05/render/{id}` | pipeline TC05 |
| POST | `/v1/tc06/render/{id}` | pipeline TC06 |
| GET | `/v1/jobs/{id}/status` | status ใน memory |
| GET | `/v1/jobs/{id}/output` | ส่ง output file |
| POST | `/v1/admin/cleanup` | cleanup job dirs |

### 6.3 Input bridge

`TCRenderRequest` รองรับชุดข้อมูลประมาณนี้:

- `products`
- `backgrounds`
- `audios`
- `covers`
- `sources`
- `product_roots`
- `values`
- `settings`
- `run_seed`

แต่ `_build_tc_inputs` ใน baseline source ส่งต่อเพียง products/backgrounds/audios/covers เป็นหลัก ทำให้:

- TC05 ไม่มี source input จาก generic V3 request
- TC06 ไม่มี product roots จาก API
- `source_ids` ที่ประกาศใน request ไม่ได้ถูก map ครบ
- `extra` ของ gateway ไม่ได้กลายเป็น `PipelineInputs.product_roots`

นี่เป็น contract defect ไม่ใช่เพียง validation defect

### 6.4 Output discovery

Worker pipeline runner สแกนไฟล์ใน job directory เพื่อสร้าง `output_files`

ปัจจัยที่ทำให้ผิดพลาด:

- input upload อยู่ directory เดียวกับ output
- TC01 output อาจขึ้นต้นด้วย `product_`
- TC02/TC05 ใช้ marker `__lens`
- TC03/TC04 ใช้ชื่อ `batch_`
- TC06 output อาจอยู่ใน product folder ที่อยู่นอก job directory
- intermediate อาจอยู่ใน `reframe/` subdirectory
- endpoint download เดิมเคย whitelist เฉพาะ `output_`

### 6.5 Health endpoint

Health ควรแสดงอย่างน้อย:

- `ok`
- worker id
- application version
- source commit
- active jobs
- max concurrent
- encoder selected
- available encoders
- CUDA/VideoToolbox readiness
- FFmpeg version
- data directory health
- disk free
- process uptime

ปัจจุบัน response shape ระหว่าง worker ไม่เหมือนกัน เช่น Mac M4 ส่ง `active_jobs` และ `system` แต่ worker Linux บางตัวส่งเพียง GPU/data_dir fields

---

## 7. Core render engine

### 7.1 `core/contract.py`

เป็นศูนย์กลางของ:

- default settings ต่อ TC
- conversion ของ settings เป็น dataclass
- validation ของ width/height/fps
- validation ของ composition
- validation ของ segment duration
- validation ของ worker count
- expected output count
- TC06 target duration
- null/default fallback policy

หลักการสำคัญคือ pipeline ไม่ควรสร้าง `GreenSettings`, `ReframeSettings` หรือ `BatchSettings` ด้วย constructor แบบกระจายเอง แต่ควรใช้ factory และ validator ใน contract

### 7.2 `core/green_render.py`

หน้าที่:

- probe media
- เลือก input streams
- สร้าง chroma filter graph
- scale/pad product และ background
- chromakey
- despill
- overlay
- cover overlay
- audio mapping
- encoder arguments
- temporary partial output
- validation และ publish output

Paths ของ filter:

- CPU chroma path
- full CUDA path ถ้ามี filters ที่จำเป็น
- hybrid CUDA chromakey + CPU despill/overlay ใน working tree เมื่อ opt-in
- image background path
- cover path

ข้อควรระวัง:

- CUDA filter support ไม่เหมือนกันในแต่ละ FFmpeg build
- `chromakey_cuda` อาจมี แต่ `despill_cuda` ไม่มี
- hardware encode ไม่ได้แปลว่า hardware filter/decode พร้อม
- ต้อง fallback แบบ fail-safe และตรวจ output จริง

### 7.3 `core/ai_reframe.py`

หน้าที่:

- lens preset catalog
- fixed 7x3 lens matrix
- composition เช่น center/left/right
- build reframe tasks
- crop/rotate/pad/filter graph
- FFmpeg command ต่อ task
- parallel reframe executor
- encoder recovery

งาน reframe ต่อ source โดยทั่วไปเป็น:

```text
source
  -> lens preset x selected composition
  -> intermediate MP4 ต่อ task
  -> downstream chroma หรือคืนเป็น TC05 output
```

ข้อสังเกต:

- reference cap ของ parallelism อยู่ที่ 3 แต่ target baseline เคย clamp ที่ 10
- target มี `reframe_short_side=720` ซึ่งเป็น performance feature ที่ไม่ตรง reference เดิม
- Windows-style path เคยสร้าง stem ผิดเพราะใช้ `Path()` ตาม host separator
- current working tree เพิ่ม NVENC preset override และ auto parallel scaling

### 7.4 `core/batch_pingpong.py`

หน้าที่:

- แบ่งช่วงเวลา
- สร้าง forward/ping-pong segments
- match product/background/audio
- render segment ด้วย FFmpeg
- รักษา duration
- ตรวจ output duration

ใช้โดย:

- TC03 batch split
- TC04 reframe output -> batch split -> chroma

### 7.5 `core/audio_master.py`

หน้าที่:

- probe audio duration
- เลือก audio source
- loop/pad/trim audio
- resample เป็น 48kHz
- stereo layout
- mux audio กับ final video
- validate duration coverage

TC04 และ TC06 มี policy เรื่อง final duration ที่ต้องทดสอบแยกจาก video-only pipeline

### 7.6 `core/ffmpeg_runner.py`

หน้าที่:

- spawn FFmpeg
- อ่าน `-progress pipe:1`
- แปลง progress เป็น `FfmpegProgress`
- log stderr/stdout
- wall-clock watchdog
- idle timeout
- stop/cancel
- partial output cleanup
- register/unregister process

### 7.7 `core/ffmpeg_registry.py`

เป็น global registry ของ live FFmpeg handles เพื่อให้:

- cancel all ตอน shutdown
- track `FfmpegRunner`
- track raw `subprocess.Popen` ของ reframe
- force kill หลัง grace period

ข้อจำกัดเชิงระบบคือ registry อยู่ใน process เดียว หาก Worker process ตาย registry และ state จะหายทั้งหมด

### 7.8 `core/gpu_detector.py`

ทำหน้าที่:

- ตรวจ encoder alias
- smoke test hardware encoder
- เลือก encoder ตาม platform
- cache readiness
- คืน encoder args
- CPU fallback
- working-tree มี opt-in NVDEC input decode

ลำดับโดยแนวคิด:

```text
VideoToolbox บน macOS
  -> NVENC บน NVIDIA
  -> QSV/AMF ตาม platform
  -> libx264 CPU fallback
```

ต้องไม่ใช้ health result จาก encoder เพียงอย่างเดียวเป็นหลักฐานว่า full filter graph ใช้งานได้ เพราะ encoder, decoder และ filter เป็นคนละ capability

---

## 8. TC01-TC06 แบบละเอียด

### 8.1 TC01: Product -> Chroma -> Final

Input:

- product video อย่างน้อย 1
- background video/image บังคับตาม contract
- cover optional
- audio optional

Stages:

1. ตรวจ file existence และ media streams
2. ตรวจ role overlap
3. สร้าง `GreenSettings`
4. เรียก `render_green`
5. เขียน temporary partial output
6. validate video/audio/duration
7. publish final output

Expected behavior:

- 1 final output ต่อ product
- output name เป็น product stem + single marker
- progress ต้องรองรับ callback ที่เป็น object, number และ mapping

Known risks:

- output ที่เริ่มด้วย `product_` อาจถูกมองเป็น upload แล้ว filter ทิ้ง
- worker output endpoint เดิมรับเฉพาะ `output_`
- progress mapping ใน target baseline ไม่ถูกอ่านเป็นเปอร์เซ็นต์

### 8.2 TC02: Reframe -> Chroma

Input:

- source/product video
- background
- selected compositions
- reframe settings

Stages:

1. สร้าง reframe task ต่อ source/lens/composition
2. รัน FFmpeg reframe
3. validate intermediate
4. ส่ง intermediate เข้า chroma
5. validate final count

Output count ต่อ source:

| Composition count | Output count |
|---:|---:|
| 1 | 7 |
| 2 | 14 |
| 3 | 21 |

Known risks:

- parallel cap target/reference ไม่ตรงกัน
- parallel chroma path ทำให้ result order เป็น completion order
- progress ใน parallel mode ไม่ได้สะท้อนทุก item
- intermediate 720p แตกต่างจาก reference full resolution
- Windows stem portability

### 8.3 TC03: Batch split -> Chroma

Input:

- products
- backgrounds
- optional audio/cover ตาม contract
- `segment_duration` 0.5 ถึง 600 วินาที โดย default 10

Stages:

1. probe duration
2. สร้าง forward segment ranges
3. render batch/ping-pong segment
4. ส่ง batch output เข้า chroma
5. validate output count และ duration

Output count:

```text
segments = ceil(source_duration / segment_duration)
final_count = products x segments
```

Known risks:

- Gateway dry-run เดิมใช้ generic count แทนการคำนวณจริง
- final ชื่อ `batch_*.mp4` ไม่ตรง download whitelist เดิม
- pipeline source file ของ TC03 เคย byte-identical กับ reference แต่ API bridge ยังไม่ครบ

### 8.4 TC04: Reframe -> Batch -> Chroma

Input:

- product/source
- background
- optional audio
- reframe compositions/lenses
- segment duration

Stages:

1. reframe ทุก lens/composition
2. validate intermediates
3. batch split ทุก intermediate
4. chroma ทุก batch segment
5. mux audio ตาม duration policy
6. reconcile expected/succeeded/failed/cancelled

Known risks:

- skip path อาจมี valid reframe output แต่ไม่ surface ใน top-level result
- TC04 ใช้ wall-clock factor สูงกว่า TC อื่น
- audio render ทำให้ runtime ยาวและ event loop block ได้ชัดเจน
- current remote RTX2050 พบ FFmpeg TC04 ทำงานอยู่ แต่ health/status timeout

### 8.5 TC05: Reframe only

Input:

- source videos
- reframe settings

Stages:

1. สร้าง fixed lens/composition tasks
2. render reframe intermediates
3. validate output
4. คืน reframe outputs โดยไม่ chroma

Known risks:

- worker upload role baseline ไม่มี `source`
- gateway generic input bridge ไม่ส่ง `source_ids` ครบ
- output names ต้องได้รับการยอมรับจาก output registry

### 8.6 TC06: Folder products -> chroma -> audio master

Folder contract:

```text
product-root/
├── product/
├── bg/
└── audio/
```

Stages:

1. resolve product folder layouts
2. discover product/bg/audio files
3. render TC01-like chroma ต่อ product folder
4. loop/concatenate video ตาม audio duration
5. audio master/mux
6. write final output ตาม layout policy

Known risks:

- API request ไม่ส่ง `product_roots` ถึง Worker
- output อาจถูกเขียนนอก worker job directory
- folder paths จาก client ไม่ใช่ filesystem ที่ Worker เข้าถึงได้โดยอัตโนมัติ
- ต้องแยก uploaded file model กับ server-side folder model ให้ชัดเจน

---

## 9. Input, output และ status contract

### 9.1 Canonical input contract ที่ควรมี

```json
{
  "job_id": "...",
  "tc": "tc01",
  "inputs": {
    "products": ["product-a.mp4"],
    "backgrounds": ["background.mp4"],
    "audios": [],
    "covers": [],
    "sources": [],
    "product_roots": []
  },
  "values": {},
  "run_seed": null
}
```

หลักการ:

- แต่ละ role ต้องมี semantic เดียวตลอด gateway/worker/pipeline
- uploaded file id ต้องแยกจาก server path
- `source` และ `product_root` ต้องไม่ถูกตัดทิ้งเพราะเป็น TC05/TC06 contract
- `values` ต้อง parse type ให้ถูก ไม่ส่งทุกอย่างเป็น string โดยไม่มี coercion
- path ที่ client ส่งต้องไม่ถูกถือว่าเป็น path ที่มีอยู่บน worker

### 9.2 Canonical status contract ที่ควรใช้

```text
queued
running
paused
succeeded
failed
cancelled
```

ไม่ควรใช้ uppercase/lowercase ปะปน เช่น `SUCCEEDED` จาก pipeline แต่ `succeeded` จาก `_JOBS`

ทุก status response ควรมี:

- `job_id`
- `status`
- `progress`
- `current_stage`
- `expected_count`
- `succeeded_count`
- `failed_count`
- `cancelled_count`
- `outputs`
- `errors`
- `worker_id`
- `encoder`
- `started_at`
- `finished_at`

### 9.3 Canonical output contract ที่ควรใช้

```json
{
  "job_id": "...",
  "files": [
    {
      "name": "product_single_001.mp4",
      "path": "job-id/product_single_001.mp4",
      "size": 123456,
      "media_type": "video/mp4"
    }
  ],
  "total": 1,
  "page": 1,
  "pages": 1
}
```

Gateway ต้องใช้ `job_id` จริงในการเรียก Worker และ Worker ต้อง validate filename แบบ single path component เพื่อป้องกัน traversal

---

## 10. ปัญหาที่ค้นพบและผลกระทบ

### P0: Worker event loop ถูกบล็อกระหว่าง render

หลักฐาน:

- Source: route เรียก pipeline sync ภายใน async handler
- Remote RTX2050: service active, port listen, มี FFmpeg TC04 process
- Remote RTX2050: `/health` และ `/status` timeout ระหว่างงาน

ผลกระทบ:

- Gateway health poll ผิดพลาด
- worker ถูกตัดออกจาก dispatch ทั้งที่ GPU กำลังทำงาน
- status/cancel ไม่ทำงานทันเวลา
- request timeout และ retry อาจสร้าง duplicate jobs

### P0: Worker fleet ใช้ revision ไม่เหมือนกัน

Snapshot ที่ตรวจพบ:

| Node | Revision | Dirty state |
|---|---|---|
| Local repository | `a4110db` | dirty หลายไฟล์ |
| RTX2050 | `17447c5` | dirty core และ requirements |
| Mac M4 | `ea2b980` | dirty `worker/main.py` และ backup files |
| RTX3050Ti | `f3ef05e` | dirty GPU/media files |

ผลกระทบ:

- output naming และ download behavior ต่างกัน
- health response ต่างกัน
- encoder settings ต่างกัน
- bug fix อาจมีเฉพาะบาง node
- canary result ไม่สามารถ generalize ไปทุก worker

### P0: Output publication/download contract ไม่ครบ

ปัญหาที่พบใน baseline:

- Worker output endpoint whitelist ชื่อ `output_` แต่ pipeline ใช้หลาย naming scheme
- TC01 output ที่ขึ้นต้น `product_` อาจถูก filter เป็น upload
- Gateway download alias เคยส่ง job id `_`
- TC06 output อาจอยู่นอก job dir
- output list และ primary output ไม่ถูกเก็บครบทุก route

ผลกระทบ: Render สำเร็จแต่ client รับไฟล์ไม่ได้ หรือ gallery แสดงว่าง

### P0: TC05/TC06 input bridge ไม่ครบ

ปัญหา:

- source IDs ไม่ถูก map ถึง `PipelineInputs.sources`
- product roots ไม่ถูก map ถึง `PipelineInputs.product_roots`
- worker upload role baseline ไม่มี source
- client filesystem path ไม่ใช่ path ที่ worker ใช้งานได้

ผลกระทบ: API ประกาศ TC05/TC06 แต่ request จริงไม่สามารถขับ pipeline ได้ครบ

### P1: Status และ progress contract ไม่สม่ำเสมอ

ปัญหา:

- pipeline คืน uppercase status
- Gateway filter/query ใช้ lowercase
- progress callback mapping เคยถูกอ่านเป็น 0
- failed pipeline อาจถูก update progress เป็น 100
- Gateway poll ไม่ได้อ่าน worker live state อย่างต่อเนื่อง

### P1: Dry-run ไม่ใช่ source of truth

ปัญหา:

- Gateway generic dry-run ใช้จำนวน product เป็นหลัก
- ไม่คำนวณ TC02 matrix
- ไม่คำนวณ TC03 segment count
- ไม่คำนวณ TC04 reframe x segment count
- มี planner ที่ถูกต้องกว่าอยู่ใน `core/cli_runner.py` แต่ไม่ได้ reuse

### P1: Reference contract drift

จาก `docs/reports/compare_v3_cursor_api_tc01_tc04_20260818_131911/report.md`:

- parallel cap target/reference ต่างกัน
- composition `null` fallback ต่างกัน
- 720p intermediate เพิ่มเข้ามาโดยไม่มี reference parity decision
- Windows path portability ถอยกลับ
- progress mapping support ต่างกัน
- TC02 parallel output ordering ต่างกัน
- TC04 skip path ไม่ surface valid reframe outputs

### P1: Deployment configuration drift

ตัวอย่าง:

- source worker default port 7701 ในบาง generation แต่ service ใช้ 8789
- installer DB defaults ต่างจาก README
- worker capacity ใน registry ต่างจาก process concurrency
- production version 1.1.1 ต่างจาก source version เดิม
- Mac ใช้ clone path คนละตำแหน่งกับ Linux worker

### P2: Test coverage ไม่พอ

- Target ไม่มี `tests/`
- ไม่มี pytest/unittest suite ใน repository target
- ไม่มี linter/formatter CI ที่กำหนดชัดเจน
- Reference มี regression 120/120 แต่ target ไม่ได้ run suite เดียวกัน
- ไม่มี end-to-end target media acceptance ครบ TC01-TC06

---

## 11. Live infrastructure topology

ส่วนนี้เป็น topology ที่สังเกตได้จาก read-only checks ไม่ใช่การประกาศว่าเป็น canonical production design

### 11.1 Hub

Hub อยู่ที่ `103.253.73.29`

บริการที่ตรวจพบ:

- Voice Proxy Hub
- Voice cluster registry ที่ port 19090
- Edit Cluster Gateway/Worker
- reverse tunnel listener หลาย port
- V3 reverse tunnel ports ที่ตอบได้บางส่วน

Hub CPU edit worker ที่ port 7701 คืน capability เช่น trim, concat, transition, color, blur, subtitle_burn, audio_mix และ encode ซึ่งเป็นคนละ contract กับ V3 green-screen Worker ที่ port 8789

### 11.2 V3 reverse tunnels ที่ตอบได้

| Hub port | Worker ที่ตรวจพบ | Health |
|---:|---|---|
| 55520 | Mac M4 V3 worker | HTTP 200 |
| 55522 | RTX2050 V3 worker | timeout ระหว่าง TC04 render |
| 55523 | Linux RTX3050Ti V3 worker | HTTP 200 |

### 11.3 V3 worker facts

#### Mac M4

- Worker id: `m4-mlx`
- Encoder: `h264_videotoolbox`
- Fallback: `libx264`
- CPU: 10 cores
- RAM: 16 GB unified memory
- Data dir: `/Users/sj88/.v3-cursor-api/worker`
- Source cwd: `/Users/sj88/v3-cursor-api/worker`
- Health response มี `active_jobs` และ `system`
- Disk ที่ probe ได้ใช้ประมาณ 78.8%

#### RTX2050

- V3 service path: `/opt/v3-cursor-api/worker`
- Port: 8789
- Service: `v3-cursor-api-worker.service`
- GPU: NVIDIA GeForce RTX 2050 4 GB
- Driver ที่ probe ได้: 580.173.02
- Revision: `17447c5`
- Dirty core/requirements changes
- มี FFmpeg TC04 process ขณะตรวจ
- HTTP health timeout เพราะ event loop ไม่ตอบระหว่างงาน

#### Linux RTX3050Ti

- Worker id: `sjnb3050ti-rtx3050`
- V3 service path: `/opt/v3-cursor-api/worker`
- Port: 8789
- Revision: `f3ef05e`
- GPU: NVIDIA GeForce RTX 3050 Ti Laptop GPU 4 GB
- Driver ที่ probe ได้: 580.173.02
- `h264_nvenc`, `hevc_nvenc`, `libx264` พร้อม
- `supports_chromakey_cuda=true`
- Dirty GPU/green/media probe changes

### 11.4 TTS cluster

TTS เป็นระบบคนละ pipeline กับ V3 Video API

Health ที่ตรวจได้:

| Port | Platform | Model/runtime | Result |
|---:|---|---|---|
| 55510 | RTX2050 | OmniVoice CUDA | HTTP 200 |
| 55511 | Mac M4 | OmniVoice MLX | HTTP 200 |
| 55512 | WSL RTX4060 | OmniVoice CUDA | HTTP 200 |
| 55513 | RTX3050Ti | OmniVoice CUDA | HTTP 200 |

ทุกตัวรายงาน `model_loaded=true` ขณะตรวจ และ worker registry port 19090 รายงาน healthy ครบ 4 ตัว

### 11.5 Node ที่ยังยืนยัน V3 ไม่ครบ

- WSL RTX4060: TTS health ผ่าน แต่ยังไม่พบ V3 reverse port ที่ตอบได้ในการ probe รอบนี้
- 64GB Windows: SSH ผ่านทาง Tailscale แต่ไม่พบ local V3 port 8789
- LAN1: ยังไม่มีหลักฐาน V3 health ในรอบนี้

สถานะเหล่านี้ควรถือเป็น `NOT_VERIFIED` ไม่ใช่ `FAILED` จนกว่าจะมี endpoint mapping และ health evidence ครบ

---

## 12. Production public surface ที่ตรวจล่าสุด

Public host: `https://green.cutdee.com`

ผลล่าสุดที่อ่านแบบ GET:

### 12.1 Gateway health

- Version: `1.1.1`
- Total configured workers: 5
- Enabled workers: 4
- Healthy workers: 4
- Disabled workers: 1
- Total capacity: 9
- Recommended encoder: `h264_nvenc`
- Disk free ประมาณ 401 GB

### 12.2 Cluster health

Production response ใหม่ใช้ slot summary แทนการเปิดเผย worker URL และ host metadata โดยมี:

- `slot`
- `max_concurrent`
- `active`
- `healthy`
- `enabled`

นี่เป็น contract ที่ต่างจาก source baseline ซึ่งเคยคืน worker id, URL, tier และ system details

### 12.3 OpenAPI security

`/v3api/openapi.json` ที่ตรวจล่าสุดระบุ `HTTPBearer` protection สำหรับอย่างน้อย:

- `/api/outputs`
- `/api/config`
- `/api/render/{tc}`
- `/api/jobs/upload`

จึงต้องรักษา production schema นี้ไว้ใน source และเพิ่ม contract test ป้องกัน regression

### 12.4 Historical production mismatch

รายงานก่อนหน้าใน `docs/reports/green_cutdee_dedicated_server_study_20260818_133756/` พบว่า:

- UI แสดง 0 videos แต่ API outputs มี 100 records
- UI version กับ API version ไม่ตรงกัน
- health schema เดิมเปิดเผย metadata มากเกินไป
- legacy service มี migration checksum mismatch และ detached process

เนื่องจาก production ปัจจุบันเปลี่ยน response shape แล้ว ต้อง rerun paired UI/API evidence เพื่อยืนยันว่า gallery mismatch ถูกแก้จริง ไม่ควรสรุปจาก health endpoint อย่างเดียว

---

## 13. Deployment และ installation

ไฟล์: `deploy/install.sh`

### 13.1 สิ่งที่ installer ทำ

1. สร้าง service user `v3api`
2. ระบุ repository เป็น install directory
3. ตรวจ Python version
4. ใช้ system Python 3.12/3.13 ถ้าเหมาะสม
5. ใช้ `uv` ติดตั้ง Python 3.12 เมื่อ system Python ใหม่เกินไป
6. สร้าง gateway/worker venv
7. install requirements
8. ตั้ง data/log directories
9. สร้าง systemd units
10. สร้าง environment files เมื่อยังไม่มี
11. สร้าง workers.json เริ่มต้น
12. enable/restart service
13. รองรับ optional Tailscale setup ผ่าน environment

### 13.2 ข้อควรระวังของ installer

- `chown -R` เปลี่ยน ownership ของ repository ที่ใช้ติดตั้ง
- installer ใช้ environment defaults ที่ไม่ตรง README ทั้งหมด
- gateway และ worker venv อยู่ใน repository
- systemd service ชี้ working tree โดยตรง ไม่ใช่ immutable release
- restart service ทันทีหลังติดตั้ง อาจชนกับงานที่ค้างหากใช้ผิดสถานการณ์
- data dirs ใช้ local disk ต่อ node
- ไม่ได้สร้าง queue หรือ shared object storage
- Tailscale setup เป็น optional path ไม่ได้ทำให้ workers.json sync อัตโนมัติ

### 13.3 Python version change ล่าสุด

Commit `a4110db` เพิ่ม:

- detection ของ Python 3.14+
- fallback ไป Python 3.12 ผ่าน `uv`
- optional Tailscale authentication path

การแก้นี้ช่วยแก้ installation compatibility แต่ไม่ได้แก้ runtime contract, event-loop blocking หรือ output publication

---

## 14. Test และ evidence inventory

### 14.1 Checks ที่ผ่าน

- `PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q gateway worker`: ผ่าน
- Target source compile probe: 55/55 ผ่านตามรายงาน
- Reference regression: 120/120 ผ่านตามรายงาน reference
- Worker import: ผ่านเมื่อกำหนด writable `WORKER_DATA_DIR`
- Linux RTX3050Ti V3 health: ผ่าน
- Mac M4 V3 health: ผ่าน
- TTS workers 4 ตัว: ผ่าน
- Production `/api/health`: ผ่าน
- Production `/api/cluster/health`: ผ่าน

### 14.2 Checks ที่ยังไม่ผ่านหรือยังไม่ประเมิน

- Local Gateway import ใน environment ปัจจุบัน: ขาด `psycopg2`
- Target end-to-end TC01-TC06: ยังไม่มีครบ
- Browser UI กับ target repository: ไม่มี frontend ใน repo
- Real media acceptance ของทุก TC: ยังไม่ครบ
- Concurrent render + health/status responsiveness: พบปัญหาจริงที่ RTX2050
- Download contract ของทุก output naming scheme: ยังไม่ผ่านครบ
- Multi-worker canary: ยังไม่มี baseline เดียวกันทุก node

### 14.3 Evidence ที่มีอยู่

`docs/reports/compare_v3_cursor_api_tc01_tc04_20260818_131911/` มี:

- report markdown/html
- summary และ test matrix
- reference regression log
- target compile log
- target runtime probes
- gateway import blocker
- TC01-TC04 static API evidence

`docs/reports/green_cutdee_dedicated_server_study_20260818_133756/` มี:

- production browser screenshots
- public API request/response/timing
- cluster health evidence
- Nginx/service/source lineage logs
- security and legacy service findings

ข้อจำกัด: reports เหล่านี้มาจากคนละเวลาและ production มีการเปลี่ยน version หลังจากนั้น จึงต้องผูก timestamp กับทุก acceptance run

---

## 15. แผนแก้ไขแบบ staged

### Phase 0: Freeze และ baseline

งาน:

- หยุดการ deploy เพิ่มชั่วคราว
- บันทึก revision/dirty state ทุก worker
- บันทึก endpoint/tunnel mapping
- บันทึก service PID และ job ที่กำลังทำงาน
- แยก `healthy`, `responsive`, `active`, `verified`

เหตุผล:

- ทำให้ทุกการแก้มี baseline เปรียบเทียบ
- ป้องกันแก้ผิด node
- ป้องกันการรวม dirty behavior เข้ากับ release

Exit criteria:

- มี worker matrix ครบทุก slot
- ทราบ source revision ของทุก worker
- ไม่มี worker ที่ unknown ใน production registry

### Phase 1: Worker liveness

งาน:

- แยก render ออกจาก FastAPI event loop
- ใช้ process/thread executor ที่มี bounded queue หรือ supervisor แยก
- ทำ heartbeat ระหว่าง render
- ให้ `/health` ตอบได้ตลอด
- ให้ `/status` อ่าน state ได้ระหว่าง render
- เชื่อม cancel กับ `ffmpeg_registry`
- กำหนด timeout แยกระหว่าง RPC, queue wait และ render runtime

เหตุผล:

- เป็น blocker ที่เห็นจาก runtime จริง
- ถ้า liveness ไม่ดี Gateway จะตัดสินใจผิดทุก feature ถัดไป

Exit criteria:

- health latency < 1-2 วินาทีระหว่าง TC04 render
- status ตอบได้ต่อเนื่อง
- cancel หยุด FFmpeg ได้
- render หนึ่งงานไม่ทำให้ health ของ worker หยุดตอบ

### Phase 2: Contract normalization

งาน:

- สร้าง Pydantic request/response models กลาง
- normalize status เป็น lowercase canonical enum
- map sources/product_roots ให้ครบ
- กำหนด upload role และ extension policy
- สร้าง output registry ที่รองรับทุก TC
- แก้ gateway download ให้ใช้ job id จริง
- เก็บ output list/log/result/count ครบ
- ปรับ ownership/auth ทั้ง route family ให้ตรงกัน

เหตุผล:

- ทำให้ render success สามารถถูกค้นพบและดาวน์โหลดได้จริง
- ลดการแก้เฉพาะ endpoint แล้วพังอีก endpoint

Exit criteria:

- TC01 output หนึ่งไฟล์ดาวน์โหลดได้
- TC02 output 7/14/21 ไฟล์ดาวน์โหลดได้
- TC03/TC04 batch files ดาวน์โหลดได้
- TC05 source outputs ดาวน์โหลดได้
- TC06 folder outputs ถูก publish ตามที่กำหนด

### Phase 3: Source parity และ release artifact

งาน:

- เลือก source of truth ระหว่าง reference กับ target
- ตัดสินใจเรื่อง 720p intermediate, parallelism และ NVDEC อย่างเป็นทางการ
- sync contract/pipeline เฉพาะ scope ที่ยอมรับ
- commit code ให้สะอาด
- สร้าง release tag หรือ artifact hash
- เพิ่ม commit/version ใน health response

เหตุผล:

- ป้องกัน node แต่ละตัวใช้ behavior คนละรุ่น
- ทำ rollback ได้

Exit criteria:

- worker ทุกตัวใช้ artifact hash เดียวกัน
- `git status` ของ deploy tree สะอาด
- health แสดง version/commit ตรงกับ release

### Phase 4: Tests และ media acceptance

งาน:

- contract unit tests
- input bridge tests
- output naming/download tests
- cross-platform path tests
- status normalization tests
- worker liveness while render test
- TC01-TC06 sample media tests
- audio duration tests TC04/TC06
- cancellation/recovery tests

Exit criteria:

- target tests ผ่านทั้งหมด
- reference parity decision ถูกบันทึก
- output file, duration, codec และ count ตรวจได้

### Phase 5: Canary rollout

งาน:

- deploy worker หนึ่งตัวก่อน
- disable worker ที่ยังไม่ parity
- run TC01 และ TC04 ด้วย sample media
- monitor health/status/download
- ตรวจ logs และ resource usage
- ขยายไป worker ที่เหลือทีละตัว

Exit criteria:

- canary ผ่านโดยไม่มี timeout
- no orphan FFmpeg
- no stale queued/running job
- output download ผ่าน

### Phase 6: Production/UI acceptance

งาน:

- pair Browser UI กับ API response
- ตรวจ `files`/`outputs`/`total`/`pages`
- ตรวจ version และ health schema
- ตรวจ OpenAPI security
- run real-media acceptance แบบมี change window

Exit criteria:

- pair completeness 100%
- pair consistency 100%
- critical errors 0
- UI count ตรง API count
- status/output/download ตรงกันทุก TC

---

## 16. Acceptance matrix

| TC | Input | Expected processing | Expected output | Minimum evidence |
|---|---|---|---:|---|
| TC01 | product + background | product chroma | 1/product | request, running status, final file, duration |
| TC02 | product + background + compositions | 7 lenses x compositions -> chroma | 7/14/21 per product | task count, final count, order, download |
| TC03 | product + background + segment duration | batch split -> chroma | ceil(duration/segment) | segment plan, count, duration |
| TC04 | product + background + audio + reframe settings | reframe -> batch -> chroma -> audio | matrix x segments | stage counts, audio duration, final files |
| TC05 | source videos | reframe only | 7 x compositions/source | source bridge, task count, files |
| TC06 | product root folders | chroma per product -> audio master | 1 final/product | folder discovery, audio duration, final path |

ทุก test case ต้องเก็บ:

- request JSON ที่ redact แล้ว
- response/status timeline
- worker id และ commit
- encoder และ FFmpeg version
- output manifest
- media probe result
- timing
- error/log summary
- evidence binding ระหว่าง UI และ API ถ้ามี UI

---

## 17. Runbook สำหรับการตรวจสอบแบบ read-only

คำสั่งต่อไปนี้เป็นตัวอย่างการตรวจ ไม่ใช่คำสั่ง deploy หรือ mutation

### 17.1 Local source

```bash
git status --short
git branch --show-current
git log --oneline --decorate -10
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q gateway worker
```

### 17.2 Local worker import

```bash
WORKER_DATA_DIR=/tmp/v3-worker-probe \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$PWD/worker" \
python3 -c 'import app.backend.main; print("worker import ok")'
```

### 17.3 Local HTTP health

```bash
curl -sS http://127.0.0.1:8788/healthz
curl -sS http://127.0.0.1:8789/health
curl -sS http://127.0.0.1:8788/api/cluster/health
```

### 17.4 Remote worker checks

ตรวจเฉพาะ endpoint health และ OpenAPI:

```bash
curl -sS --max-time 15 http://<worker-or-tunnel>/health
curl -sS --max-time 15 http://<worker-or-tunnel>/openapi.json
```

ห้ามสรุปว่า worker healthy จาก TCP port อย่างเดียว ต้องตรวจ HTTP response latency ด้วย

### 17.5 Service checks บน Linux

```bash
systemctl show v3-cursor-api-worker.service \
  -p ActiveState -p SubState -p MainPID -p ExecStart -p WorkingDirectory
ss -ltn
pgrep -af uvicorn
pgrep -af ffmpeg
```

### 17.6 Git parity checks บน worker

```bash
git -C /opt/v3-cursor-api rev-parse --short HEAD
```

ถ้า repository ownership ทำให้ Git ปฏิเสธ ต้องใช้ `safe.directory` แบบชั่วคราวใน command เท่านั้น ไม่ควรแก้ global config ระหว่างตรวจสอบ

---

## 18. Decision log ที่ต้องตัดสินใจก่อน implementation

1. Source of truth คือ reference V3 หรือ target API fork
2. 720p intermediate เป็น accepted feature หรือ contract drift
3. Parallelism สูงสุดต่อ worker และต่อ GPU เท่าใด
4. TC06 จะรับ folder path, archive upload หรือ object storage
5. Output จะอยู่ local worker, shared volume หรือ object storage
6. Job queue จะอยู่ PostgreSQL, Redis, dedicated broker หรือ process supervisor
7. Cancel semantics ต้อง kill process ระดับใด
8. Status canonical ใช้ enum ชุดใด
9. Gateway จะรองรับ multi-output แบบ files manifest อย่างไร
10. Production UI contract ใช้ `files`, `outputs` หรือรองรับทั้งสองชั่วคราว
11. Worker health จะ expose system metrics ระดับใด
12. Worker ที่ dirty หรือ revision เก่าจะ drain, disable หรือ redeploy อย่างไร

การไม่ตัดสินใจข้อเหล่านี้ทำให้ implementation ใหม่กลายเป็น compatibility patch เพิ่มอีกชั้น และทำให้ปัญหา contract drift กลับมา

---

## 19. Definition of Done

### Source

- Contract และ pipeline มี test
- ไม่มี output naming ที่ไม่มี registry รองรับ
- ไม่มี input role ที่ประกาศแต่ส่งไม่ถึง pipeline
- status และ progress canonical
- health ไม่ถูก render block

### Deployment

- ทุก worker ใช้ artifact เดียว
- ไม่มี dirty production tree
- workers.json มี owner, endpoint, capacity และ enabled state ถูกต้อง
- reverse tunnels มี supervision และ alert
- worker health แสดง commit/version

### API

- Upload, render, poll, cancel และ download ทำงานเป็น flow เดียว
- Multi-output manifest ครบ
- Ownership/auth ตรงกันทุก route
- Error status ไม่ถูกแปลงเป็น success/progress 100
- Dry-run ตรงกับ actual planner

### Operations

- มี canary procedure
- มี rollback procedure
- มี orphan job cleanup
- มี FFmpeg process cleanup
- มี disk threshold และ alert
- มี production evidence bundle

### Acceptance

- TC01-TC06 ผ่าน sample media
- pair completeness 100%
- pair consistency 100%
- critical errors 0
- health/status responsive ระหว่าง render
- output download ผ่านทุก naming scheme

---

## 20. บทสรุปสุดท้าย

ระบบนี้มี render engine ที่ค่อนข้างสมบูรณ์จาก V3 เดิม แต่ชั้น API cluster และ deployment orchestration ยังไม่เป็นระบบเดียวกันเต็มที่ จุดเสี่ยงที่สำคัญที่สุดคือการนำ synchronous render ไปอยู่ใน async web server แล้วพยายามใช้ health polling และ multi-worker dispatch รอบมัน

หลักการแก้ที่ถูกต้องคือ:

1. ทำให้ระบบสังเกตสถานะได้ก่อน
2. ทำให้ worker ทุกตัวใช้ source เดียวกัน
3. ทำให้ input/status/output contract เป็นอันเดียว
4. ทดสอบหนึ่ง worker ให้ผ่านก่อนกระจายทั้ง cluster
5. ค่อย optimize GPU และ parallelism หลัง correctness ผ่าน

การเพิ่ม NVENC preset, NVDEC, CUDA hybrid หรือ parallel FFmpeg ก่อนแก้ 5 ข้อข้างต้นจะเพิ่ม throughput ได้บางกรณี แต่ไม่แก้ปัญหา health timeout, output หาย, status ผิด และ worker parity

เอกสารนี้จึงควรใช้เป็น baseline สำหรับ implementation plan, change review, deployment checklist และ production acceptance ของ V3_cursor_API ต่อไป
