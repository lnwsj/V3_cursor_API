# V3 Cursor API: Current State Audit

วันที่ตรวจ: 2026-08-20 (Asia/Bangkok)

เอกสารนี้เป็น source/runtime audit ล่าสุดสำหรับแยก **source refactor ปัจจุบัน**, **production release ที่ deploy อยู่** และ **historical reports** ออกจากกัน

## สรุปสถานะ

| พื้นที่ | สถานะ | หลักฐาน |
|---|---|---|
| Worker core และ TC01-TC06 source | มี contract/lifecycle และ unit coverage | `worker/app/backend/`, `tests/unit/` |
| Unit tests | 45 passed; มี warning 2 กลุ่ม | `python3 -m pytest -q` |
| Gateway `refactor-base` | **RELEASE BLOCKED** | lifespan, auth, upload และ dispatch wiring ยังไม่ครบ |
| Production Gateway | แยกจาก source refactor | version `1.2.0`, commit `f6299fa` |
| Production M4 Worker | healthy ผ่าน tunnel | version `1.2.0`, commit `aa671b5`, preferred `hevc_videotoolbox` |
| CI | มี workflow แต่ยัง non-blocking | lint/test ใช้ `|| echo`/`|| true` |

## Source Snapshot

- Branch: `refactor-base`
- HEAD: `25e1032` (`fix(gateway): repair router package after Phase 3 refactor`)
- `origin/refactor-base`: `1853df0`
- Refactor sequence: services extraction -> router extraction -> compatibility shims
- Working tree มี `CLAUDE.md` ที่แก้ค้าง และ untracked local/report artifacts; audit นี้ไม่แก้หรือ stage ไฟล์เหล่านั้น

## Production Snapshot

Production ไม่ได้รัน source HEAD ปัจจุบัน จึงห้ามใช้ผล source audit เป็นหลักฐานว่า production ถูกทำให้เสียหรือดีขึ้นแล้ว

| Component | Current live fact |
|---|---|
| Gateway | `1.2.0 / f6299fa` |
| M4 Worker | `1.2.0 / aa671b5`, `vt_ready=true` |
| M4 preferred encoder | `hevc_videotoolbox` |
| Cluster | 6 slots, 4 enabled/healthy, capacity 6, active 0 ณ snapshot |

M4 benchmark ที่บันทึกค่า H.264 ไว้ใน [`V3_MAC_M4_SPEED_BENCHMARK_TH.md`](V3_MAC_M4_SPEED_BENCHMARK_TH.md) เป็น historical result ของ encoder/config รุ่นก่อน preference เปลี่ยนเป็น HEVC และไม่ควรใช้แทน HEVC benchmark ปัจจุบัน

## P0 Release Blockers

### Gateway startup wiring

`gateway/app/backend/main.py` เรียก `_init_workers()` และ `_reconcile_active_jobs()` ใน lifespan แต่ source ปัจจุบันยังไม่มี `WORKERS_FILE`, `log` และ `_reconcile_active_jobs` ที่ wiring นี้ต้องใช้

ผลกระทบ: import อาจผ่าน แต่ startup/runtime ของ Gateway ไม่ควรถูกถือว่าผ่านจนกว่าจะมี app smoke test ที่สร้าง lifespan จริง

### Authentication dependency

`gateway/app/backend/deps.py` alias `resolve_token_to_user` เป็น `_user_for_token` แต่เรียกด้วย argument เดียว ขณะที่ implementation ใน `services/users.py` ต้องการ context เพิ่มเติม

ผลกระทบ: user-authenticated routes เช่น upload, job และ portal อาจจบด้วย `TypeError` แทน 401/handler response

### Upload route

`gateway/app/backend/routers/uploads.py` มี expression ที่เรียก `secrets_token_hex()` ก่อน import alias `_sec.token_hex`

ผลกระทบ: upload ที่ผ่าน auth อาจ fail ด้วย `NameError` ก่อนเขียนไฟล์

### Job dispatch

`gateway/app/backend/routers/jobs.py` มี `CreateJobRequest` ที่ไม่มี `tc` แต่ handler ใช้ `req.tc`; handler เรียก `_pick_worker` ที่ไม่ได้ import/define และส่วน forward ไป Worker ยังเป็น comment placeholder

ผลกระทบ: `/api/v1/jobs` ไม่ใช่ end-to-end queue/dispatch contract แม้ response อาจสร้าง job row ได้ในบาง path

### Portal/render route mismatch

Current router มี `/api/render/{tc}` ซึ่งอ่าน multipart แล้วคืน echo payload แต่ไม่มี dynamic `/api/{tc}/render` ที่คู่มือบางฉบับระบุ และไม่มี actual enqueue ใน handler นี้

ผลกระทบ: portal submit และ JSON examples ในคู่มือไม่ควรถูกใช้เป็น production acceptance กับ `refactor-base`

## P1 Security และ Wiring Risks

- `POST /api/cluster/workers/reload`, add, patch และ delete ไม่มี `_verify_internal` หรือ `_require_admin`
- Internal WebSocket publish route ไม่มี internal auth dependency
- Worker probe ใน cluster service ถูกเรียกด้วย internal token ว่างจาก router
- CORS ใช้ `allow_origins=["*"]` พร้อม `allow_credentials=True`
- `/api/outputs` และ `/api/download/{file_path}` ต้องตรวจ ownership/path exposure ก่อนเปิดเป็น public contract
- Worker `/health` เปิด operational data และ `data_dir` หาก port ถูกเข้าถึงโดยตรง
- `WORKERS_FILE_PATH` รอ `init_config()` แต่ current `main.py` ยังไม่ wire config นี้ชัดเจน
- DB defaults ใน source, installer และ README ไม่ตรงกัน (`v3_cursor_api` เทียบกับ `cutdee_cluster`)

## Current API Matrix

| Surface | Source ปัจจุบัน | เอกสารควรเรียกว่า |
|---|---|---|
| Worker `/health`, `/v1/*` | มี route และ Worker core ตอบได้ | internal worker contract; ต้องตรวจ token/network |
| Gateway `/healthz`, version, cluster health | มี route ใน router | public liveness/summary; ไม่ใช่ render acceptance |
| `/api/v1/uploads/{role}` | route มี แต่มี P0 `NameError` | blocked until upload smoke passes |
| `/api/v1/jobs` | model/dispatch wiring ไม่ครบ | legacy/blocked in current refactor |
| `/api/render/{tc}` | multipart echo/compatibility handler | compatibility placeholder, not render proof |
| `/api/{tc}/render` | ไม่พบ route ใน current Gateway source | planned/contract target only |
| `/api/{tc}/dry-run` | planner มีเป็น module แต่ไม่พบ HTTP route | library/planner capability; not public endpoint |
| `/api/v1/jobs/{id}/download/{file}` | placeholder 404 ใน extracted router | blocked until worker proxy is wired |
| `/ws/jobs/{id}` | route exists, broker/auth/publish still need acceptance | source feature, not production E2E proof |

## Worker Contract ที่ยังใช้เป็นฐานได้

Worker source มี surface ที่ชัดกว่า Gateway:

- `TCRenderRequest` รองรับ singular/plural product/background/cover/audio, source, product root, settings, values, extra และ run seed
- `settings` ถูก merge ก่อน `values`; `values` มี precedence สูงกว่า
- queue ใช้ bounded executor และ persist `.job_state.json`
- terminal states มี `succeeded`, `partial`, `failed`, `cancelled`, `paused`, `invalid_input`
- TC02 streaming และ M4 tuning เป็น runtime configuration ไม่ใช่ universal default ของทุก worker

อย่างไรก็ตามต้องแยก **Worker direct acceptance** ออกจาก **Gateway E2E acceptance** เพราะ Gateway dispatch ยังเป็น blocker ใน source refactor

## Tests และ CI

Local test command ล่าสุด:

```text
python3 -m pytest -q
45 passed
```

Warnings ที่ต้องเก็บเป็น follow-up:

- `asyncio_mode` เป็น unknown config option ใน environment ที่ตรวจ
- FastAPI `Path(..., regex=...)` deprecated; ควรเปลี่ยนเป็น `pattern=`

CI ปัจจุบันยังไม่เป็น release gate:

- lint failure ถูกแปลงเป็น warning ด้วย `|| echo`
- pytest failure ถูกกลบด้วย `|| true`
- รันเฉพาะ `tests/unit`
- ไม่มี Gateway lifespan smoke, PostgreSQL integration, installer test หรือ real-media acceptance

## เอกสารที่ต้องอัปเดต

| ไฟล์ | การแก้ที่จำเป็น |
|---|---|
| `README.md` | แยก target architecture ออกจาก current refactor; เอา E2E command ที่ใช้ route/model ไม่ตรงออกจาก quick-start; อัปเดต source tree และ encoder order |
| `docs/README_TH.md` | เพิ่ม chronology, current audit และตาราง source/live/historical |
| `docs/V3_CURSOR_API_USER_GUIDE_TH.md` | ใส่ release-blocked banner; ระบุว่า dynamic render/upload/download examples เป็น target contract จนกว่า Gateway acceptance ผ่าน |
| `docs/V3_CURSOR_API_PIPELINE_GUIDE_TH.md` | ระบุ dry-run route ยังไม่มีใน current Gateway และ M4 ปัจจุบัน preferred HEVC |
| `docs/V3_CURSOR_API_OPERATIONS_RUNBOOK_TH.md` | เพิ่ม no-deploy gate สำหรับ refactor-base, source/runtime parity และ generated systemd units |
| `docs/V3_CURSOR_API_DEVELOPMENT_GUIDE_TH.md` | เปลี่ยน tree เป็น routers/services/templates; เพิ่ม import/lifespan smoke และ CI non-blocking caveat |
| `docs/V3_MAC_M4_SPEED_BENCHMARK_TH.md` | ติดป้าย historical H.264 benchmark และเพิ่ม current HEVC runtime note |
| `docs/V3_CURSOR_API_DEEP_DIVE_TH.md` | เพิ่ม current-state banner; คง historical findings ไว้แต่ link มาที่ audit นี้ |
| `docs/reports/*` | คงเป็น immutable evidence และเพิ่ม snapshot commit/superseded-by ใน index |

## Release Gate ก่อนเปลี่ยนเอกสารจาก Blocked เป็น Ready

1. แก้ Gateway lifespan/import wiring และรัน app startup smoke
2. แก้ auth resolver signature และยืนยัน 401/200 ของ user routes
3. แก้ upload route และรัน upload fixture จริง
4. สร้าง worker selection/dispatch/status polling/download proxy จริง
5. ใส่ internal/admin auth ให้ cluster mutation และ WebSocket publish
6. ทำให้ source, installer, env matrix และ workers registry ใช้ path/token เดียวกัน
7. ทำ Gateway-to-Worker TC01 canary ผ่าน
8. ทำ TC02-TC06 acceptance และตรวจ output ด้วย ffprobe
9. ทำ CI ให้ test failure เป็น non-zero และเพิ่ม lifespan/integration coverage
10. ออก release marker ใหม่ แล้วอัปเดต live parity table

## ข้อสรุป

เอกสารเดิมยังมีประโยชน์ในฐานะ architecture/contract และ historical evidence แต่ไม่ควรใช้เป็นคำยืนยันว่า `refactor-base` deploy ได้แล้ว. Current source ต้องถูกจัดเป็น **release-blocked** จน Gateway wiring และ E2E acceptance ผ่านตาม gate ด้านบน
