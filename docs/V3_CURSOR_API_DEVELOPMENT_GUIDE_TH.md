# V3 Cursor API: คู่มือ Developer

> **Current source status:** `refactor-base / 25e1032` ทดสอบ unit ผ่าน แต่ Gateway runtime ยัง release-blocked. `45 passed` ไม่ได้แปลว่า lifespan, PostgreSQL, worker dispatch หรือ real-media E2E ผ่าน

## 1. โครงสร้างโค้ด

```text
gateway/app/backend/main.py       Gateway bootstrap/OpenAPI/router registration
gateway/app/backend/routers/       Gateway auth/jobs/cluster/system/users/ws routes
gateway/app/backend/services/      Gateway DB/user/job/worker/metrics services
gateway/app/backend/templates/     HTML portal/status/dashboard templates
worker/app/backend/main.py        Worker API, queue, job state
worker/app/backend/core/contract.py
                                   settings validation และ TC defaults
worker/app/backend/core/green_render.py
                                   filter graph, FFmpeg command, output validation
worker/app/backend/core/gpu_detector.py
                                   encoder detection และ preset mapping
worker/app/backend/core/media_probe.py
                                   stream/codec/duration probes
worker/app/backend/core/pipelines/  TC01-TC06 entry points
deploy/install.sh                  Linux installer และ service generation
tests/                             unit/contract tests
.github/workflows/ci.yml           CI workflow (currently non-blocking)
```

## 2. Prerequisites

- Python ที่รองรับ dependency ของ project; แนะนำ 3.12 หรือ 3.13
- FFmpeg และ FFprobe ใน PATH
- PostgreSQL/PgBouncer สำหรับ Gateway integration
- media fixture สำหรับ real-media test

ติดตั้ง dependency:

```bash
python3 -m venv gateway/.venv
gateway/.venv/bin/pip install -r gateway/requirements.txt

python3 -m venv worker/.venv
worker/.venv/bin/pip install -r worker/requirements.txt
```

หากใช้ `deploy/install.sh` installer จะตรวจ Python และเตรียม venv ให้ตาม role

## 3. Run Local

ตัวอย่าง Worker:

```bash
export CUTDEE_INTERNAL_TOKEN='<INTERNAL_TOKEN>'
export WORKER_ID='local-worker'
export WORKER_PORT=8789
export WORKER_DATA_DIR="$PWD/.local-data/worker"
worker/.venv/bin/python -m uvicorn app.backend.main:app \
  --app-dir worker/app/backend --host 127.0.0.1 --port 8789
```

ตัวอย่าง Gateway ต้องกำหนด `GATEWAY_DATA_DIR`, database connection และ worker registry ให้ครบก่อน start

```bash
gateway/.venv/bin/python -m uvicorn app.backend.main:app \
  --app-dir gateway/app/backend --host 127.0.0.1 --port 8788
```

ตรวจ:

```bash
curl -fsS http://127.0.0.1:8788/healthz
curl -fsS http://127.0.0.1:8789/health
```

สองคำสั่งนี้เป็น direct local probe เท่านั้น เมื่อทดสอบ public host ให้ใช้
`https://green.cutdee.com/v3api/healthz`; root `https://green.cutdee.com/healthz`
เป็น frontend HTML catch-all ไม่ใช่ JSON gateway health

## 4. Validation ก่อนส่งงาน

```bash
python3 -m pytest -q
python3 -m compileall -q gateway worker tests
bash -n deploy/install.sh
```

หลังแก้ pipeline ต้องเพิ่มหรือปรับ tests ที่ครอบคลุม:

- invalid input และ missing role
- expected/succeeded/failed invariants
- cancellation/pause behavior
- output manifest และ safe filename
- encoder fallback
- duration/audio mapping

หลังแก้ Gateway refactor ต้องเพิ่ม smoke tests สำหรับ import/lifespan, auth dependency, upload, worker selection/dispatch, status polling และ download proxy; ชุด unit ปัจจุบันยังไม่ครอบคลุม flow เหล่านี้

## 5. เพิ่มหรือแก้ Pipeline

ลำดับที่ควรทำ:

1. กำหนด input/output contract ใน `contract.py`
2. ใช้ `PipelineInputs` และ `PipelineCallbacks`
3. ทำ input validation ก่อน probe/render ที่แพง
4. ใช้ `render_green`, `render_reframe_plan` หรือ core engine ที่มีอยู่
5. คืน `PipelineResult` พร้อม stage results
6. เรียก `.finalize(...)` ก่อนคืนผลเสมอ
7. อย่าเขียน absolute filesystem path ลง public response
8. เพิ่ม unit test และ dry-run behavior ถ้าเหมาะสม

Pipeline ต้องไม่ถือว่า HTTP `202` เป็น render success เพราะ `202` หมายถึง enqueue สำเร็จเท่านั้น

ใน source ปัจจุบัน `/api/render/{tc}` ยังเป็น compatibility echo handler และ `/api/{tc}/render` ยังไม่ถูก register; ต้องตรวจ route จาก `openapi.json` และทำ canary ก่อนเขียนตัวอย่างว่าใช้งานได้

## 6. Settings และ Compatibility

- ใช้ canonical key `encoder`, ไม่ใช้ alias ที่ไม่อยู่ใน `ALIAS_MAP`
- ใช้ preset profile เช่น `medium`, `fast`, `hq`; อย่าส่ง raw NVENC preset เช่น `p4` เข้า contract โดยตรง
- `settings` กับ `values` อาจถูก merge หลายชั้น ให้ตรวจ precedence ก่อนเพิ่ม field
- Worker route ใช้ `TCRenderRequest`; plural IDs มี priority เหนือ singular IDs
- `/api/v1/jobs` เป็น legacy TC01-style route; dynamic `/api/{tc}/render` ใช้สำหรับ TC-specific dispatch

## 7. FFmpeg และ Hardware

ก่อนเพิ่ม hardware path ให้ตรวจ binary จริง:

```bash
ffmpeg -hide_banner -hwaccels
ffmpeg -hide_banner -encoders
ffmpeg -hide_banner -decoders
ffmpeg -hide_banner -filters
ffmpeg -hide_banner -h encoder=h264_videotoolbox
```

ข้อควรระวัง:

- `h264_videotoolbox` เป็น encoder name ใน FFmpeg build ที่ใช้กับ M4
- input acceleration ใช้ `-hwaccel videotoolbox` ไม่ใช่ encoder name เป็น decoder
- filter graph chromakey/despill/overlay อาจยังเป็น CPU แม้ encoder เป็น hardware
- ทุก hardware path ต้องมี smoke test และ CPU fallback ที่ตรวจได้
- อย่าเปิด CUDA filter path บน worker ที่ไม่มี CUDA filter capability

บน M4 live ปัจจุบัน preferred encoder เป็น `hevc_videotoolbox`; H.264 benchmark เดิมเป็น historical result ของ `aa671b5`

## 8. Git และ Working Tree

ก่อน commit:

```bash
git status --short
git diff
git diff --check
git log --oneline -10
```

stage เฉพาะไฟล์ในงาน อย่าใช้ `git add .` เมื่อมี user changes, reports หรือ local secrets ปะปนอยู่

ก่อน push:

```bash
git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m "<concise message>"
git push origin main
```

## 9. Real-media Acceptance

ขั้นต่ำต่อ release ที่แตะ render engine:

```text
TC01: 1 output, codec/resolution/duration/audio valid
TC02: expected 21 outputs, all valid
TC03-TC06: input-specific count and stage result valid
```

เก็บ fixture, commit marker, worker health, settings, elapsed time และ ffprobe summary ใน report เพื่อให้เทียบ regression ได้
