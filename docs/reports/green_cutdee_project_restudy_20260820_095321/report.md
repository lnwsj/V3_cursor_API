# Green Cutdee / V3_cursor_API — Project Restudy and Documentation Audit

**สถานะ:** `FAIL_WITH_SCOPE_NOTE` — ศึกษาและเก็บหลักฐานครบตามขอบเขต แต่ยังไม่ใช่ production PASS

**เวลา:** 2026-08-20 09:59:38 (+07:00)

**Project:** `green.cutdee.com`

**Repository ที่ตรวจ:** `/Users/sj88/Documents/codex/V3_cursor_API`

**Repo HEAD:** `refactor-base`, `ba921c0 docs: audit current Gateway refactor state`

**Code baseline under audit:** `25e1032 fix(gateway): repair router package after Phase 3 refactor`; `ba921c0` is docs-only on top of this code

**Production source/runtime ที่ตรวจแยกกัน:** Dedicated Server `/opt/v3-cursor-api`, remote git `f6299fa`; gateway `127.0.0.1:8788`, worker `127.0.0.1:8789`

**กติกาขอบเขต:** ไม่ใส่ token/password ลง artifact, ไม่อัปโหลด media จริง, ไม่สร้าง job จริง และไม่อ้างผล authenticated download เป็น PASS

**AI Full Dev log:** preflight activity `12178`, during activity `12180`, closeout activities `12181` และ docs extension `12182`; create ได้ HTTP 201 และ PATCH status ทุก activity เป็น `done` ได้ HTTP 200 (หลักฐาน response/timing อยู่ใน `logs/ai_full_dev_*.json`).

## 1. คำตอบสั้นที่สุด

จุดที่ต้องอัปเดตเอกสารมี 4 กลุ่มใหญ่:

1. **Route และ public surface:** เอกสารจำนวนมากยังบอกให้ใช้ `/v3api` ทุกจุดหรือใช้ `/api/{tc}/render` แบบ dynamic ทั้งที่หน้าเว็บจริงเรียก `/api/render/<tc>` และ API schema จริงอยู่ที่ `/v3api/openapi.json`; `/healthz` ที่ root กลับเป็น HTML ของ frontend ไม่ใช่ JSON health ของ gateway
2. **Source architecture:** source ปัจจุบันแยก `routers/`, `services/`, `deps.py`, `templates/` แล้ว แต่ README/Deep Dive/Development Guide ยังอธิบาย gateway เป็น monolithic `main.py`
3. **Release lineage:** UI แสดง `v1.1.1`, API ตอบ `1.2.0`/`f6299fa`, local candidate เป็น `25e1032`; ต้องมีเอกสารแยก “deployed production” กับ “local candidate” ห้ามใช้คำว่า current โดยไม่ระบุ commit
4. **Operational truth:** Dedicated Server service รันจริงที่ `/opt/v3-cursor-api` และ systemd สร้างจาก `deploy/install.sh`; ปัจจุบัน live cluster รายงาน 6 configured, 4 enabled/healthy, 2 disabled แต่ยังไม่มีหลักฐานว่าไฟล์ที่ local branch จะถูก deploy แล้ว

นอกจากนี้พบ source blocker และ security documentation blocker ที่เอกสารห้ามเขียนว่าเสร็จแล้ว: lifecycle ใน gateway มีชื่อที่ไม่ถูกนิยาม, route งานหลายตัวเป็น placeholder, dispatch ไป worker ยังมี comment ว่า omitted, และ installer/โค้ดมีค่า secret default/literal ที่ต้องเอาออกและ rotate โดยไม่บันทึกค่าจริงในเอกสาร

## 2. Source of truth ที่ใช้

| เรื่อง | หลักฐานปัจจุบัน | ใช้ตัดสินอะไร |
|---|---|---|
| Public UI | Browser captures `screenshots/01_open_page.png` ถึง `05_audio_ready_or_error.png` | สิ่งที่ผู้ใช้เห็นและ route ที่ frontend เรียก |
| Public API | `api/*/request.json`, `response.json`, `timing.json` | HTTP status/body/latency ณ เวลาศึกษา |
| API contract | `api/TC03_openapi_contract/response.json` จาก `/v3api/openapi.json` | route ที่ production เปิดเผยจริง |
| Local code | `gateway/app/backend`, `worker/app/backend`, `logs/local_validation.log` | code baseline `25e1032` ที่ยังไม่ยืนยันว่า deploy แล้ว |
| Dedicated Server | `logs/remote_server_readonly.log` | service/path/port/remote checkout แบบอ่านอย่างเดียว |
| Documentation rules | `logs/notion_context.md` | รูปแบบ project/source-of-truth/evidence/closeout |

## 3. สิ่งที่หน้าแรกทำจริง

จาก browser UI:

- หน้า root เป็น `🟢 SJ88 Green Screen`, มี pipeline TC01–TC06 และ Stats
- UI label เป็น `v1.1.1`; แสดง encoder `h264_nvenc` และ disk free ประมาณ 397 GB
- TC01–TC04 ใช้ Product + Background เป็นแกนหลัก; TC05 เป็น reframe-only และแสดง Product เป็น required; TC06 แสดง Product/Background/Audio controls
- เมื่อไม่มีไฟล์ ปุ่ม Render ถูก disabled และการกดไม่สร้าง job
- History/Output ถูกป้องกัน: หน้าแสดง `Token required` และ `Error: 401 authentication required`
- JavaScript ของหน้าเว็บอ้าง route `/api/render/<tc>`, poll `/api/job/<job_id>`, output `/api/job/<job_id>/output`, download-all `/api/job/<job_id>/download-all`, thumbnails, `/api/outputs`, `/api/download/<path>`, history และ list

หลักฐานภาพอยู่ใน [browser_ui_observations.md](logs/browser_ui_observations.md) และรูปจริงใน [screenshots](screenshots/).

### ถ้าอัปโหลดไฟล์ จะรู้ได้อย่างไรว่าไปทำเครื่องไหน และโหลดอย่างไร

สิ่งที่ยืนยันได้จากระบบตอนนี้:

1. Public UI ส่งงานเข้า public gateway ผ่าน `/api/render/<tc>`; prefix `/v3api` ก็ proxy ถึง API เดียวกันสำหรับ API surface
2. Dedicated Server ที่ตรวจมี gateway service ที่ `127.0.0.1:8788` และ worker service ที่ `127.0.0.1:8789`; ทั้งสอง service active/enabled
3. Live health ณ เวลาตรวจบอก cluster 6 slots, enabled/healthy 4, disabled 2, active jobs 0; นี่เป็นหลักฐาน capacity/health ไม่ใช่หลักฐานว่า job ใดถูกเลือกไป node ใด
4. หน้า authenticated history/dashboard และ API live-job surface เป็นจุดที่ควรแสดง `worker_id`/node หลังมี job จริง แต่รอบนี้ไม่มี token จึงยังไม่สามารถพิสูจน์ mapping ของ job หนึ่งรายการได้
5. หลัง job จริงต้อง poll job status จน terminal status แล้วใช้ output/download endpoint ที่ตอบจาก production พร้อม response และไฟล์จริง; รอบนี้ยังไม่ทำ จึงยังไม่รับรอง flow download

ดังนั้นเอกสารต้องเขียนเป็น **“ระบบมี gateway → worker cluster และมี endpoint สำหรับ poll/output/download; การยืนยันว่า job นี้ทำที่ node ไหนและไฟล์ไหนโหลดได้ ต้องใช้ authenticated rerun พร้อม job_id/worker_id/output hash”** ไม่ควรเขียนเหมือนทุก job ทำที่ Dedicated Server เครื่องเดียวหรือเหมือนมีไฟล์เสร็จแล้วเสมอ

## 4. Production API evidence

| Testcase | Request | ผลที่สังเกต | ความหมาย |
|---|---|---|---|
| STUDY-TC01 | `GET /api/health?request_id=...` | 200, API `1.2.0`, commit `f6299fa`, 4 healthy/6 configured | gateway ตอบจริง |
| STUDY-TC02 | `GET /api/version` | 200, `1.2.0`, Python 3.12.3 | ไม่ตรงกับ UI `v1.1.1` |
| STUDY-TC03 | `GET /v3api/openapi.json` | 200, JSON 54,632 bytes, 50 paths | schema จริงอยู่ใต้ `/v3api` |
| STUDY-TC04 | `GET /api/outputs` ไม่มี auth | 401 `invalid API token` | output ไม่ public |
| STUDY-TC05 | `GET /api/cluster/health` | 6 slots; slots 1/3/4/5 enabled+healthy; 2/6 disabled | current health |
| STUDY-TC06 | `GET /api/cluster/public?hours=24` | current summary + historical 24h metrics | ห้ามรวม current กับ history |
| STUDY-TC07 | `POST /api/tc06/render` ไม่มี auth/ไม่มีไฟล์ | 401 | guard เท่านั้น ไม่ใช่ render PASS |
| STUDY-TC08 | `GET /v3api/healthz` | 200 JSON health | API health URL ที่ควรเขียนในคู่มือ |

Root-path mismatch ที่ต้องเขียนให้ชัด:

- `GET /healthz` → HTTP 200 แต่ `content-type: text/html`, body เป็น frontend HTML
- `GET /openapi.json` → HTTP 200 แต่เป็น frontend HTML
- `GET /api/openapi.json` → HTTP 404 `Not Found`
- `GET /v3api/openapi.json` → JSON OpenAPI จริง

หลักฐาน raw อยู่ใน `api/` และ [public_surface_mismatch.log](logs/public_surface_mismatch.log).

## 5. Local source audit

### 5.1 โครงสร้างใหม่ที่ต้องอธิบายในเอกสาร

Local source มีการแยกจริงแล้ว:

- `gateway/app/backend/routers/{auth,cluster,jobs,pages,system,uploads,users,ws}.py`
- `gateway/app/backend/services/{db,jobs,metrics,users,workers}.py`
- `gateway/app/backend/deps.py`
- `gateway/app/backend/templates/pages.py`
- `gateway/app/backend/core/helpers.py`
- worker pipeline แยก `tc01` ถึง `tc06` และ `_common`

แต่ `gateway/app/backend/main.py` ยัง 155 บรรทัดและมีทั้ง import/re-export/config/lifecycle/router wiring ปะปนกัน จึงยังไม่ควรเขียนว่า “wiring only” แบบสมบูรณ์จนกว่าจะซ่อม lifecycle และตรวจ startup จริง

### 5.2 Blocker ที่เอกสารห้ามอ้างว่า complete

หลักฐานตรงอยู่ใน [source_and_docs_findings.log](logs/source_and_docs_findings.log):

- `gateway/app/backend/main.py:90-105` อ้าง `WORKERS_FILE`, `log`, `_reconcile_active_jobs` แต่ไม่พบ symbol ใน module; import ผ่านและ route inventory ผ่าน ไม่เท่ากับ startup/lifespan ผ่าน
- `gateway/app/backend/main.py:59-77` redefines config ที่ import จาก helpers ทำให้ source of truth ซ้ำ
- `gateway/app/backend/routers/jobs.py:60-71` `/api/render/{tc}` รับ form แล้วคืน metadata; ไม่มี upload/dispatch จริงใน handler นี้
- `gateway/app/backend/routers/jobs.py:161-193` สร้าง DB job แต่มี comment `Forward to worker (omitted for brevity)`
- `gateway/app/backend/routers/jobs.py:204-207` download proxy ยัง raise 404 ว่า not yet extracted
- `gateway/app/backend/routers/jobs.py:211-213` upload route คืน `files: []` เป็น placeholder
- `gateway/app/backend/routers/jobs.py:231-238` pause/resume คืนค่า success โดยไม่แสดง state mutation จริง
- มี absolute imports ใน route-time functions ที่ `jobs.py`, `auth.py`, `system.py`, `ws.py`; หลัง package refactor ควรเปลี่ยนเป็น import ที่สอดคล้องกับ package runtime และทดสอบผ่าน actual endpoint

### 5.3 Validation ที่ทำได้และทำไม่ได้

- รอบแรกหลัง docs commit full suite มี transient failure 1 รายการใน `test_worker_lifecycle` (`running` แทน `succeeded`); rerun เฉพาะไฟล์ 3/3 ผ่าน และ full suite ถัดมา **45 passed**, 1 deprecation warning (`regex` → `pattern`)
- `.venv/bin/python -m compileall -q gateway worker tests`: **ผ่าน**
- `git diff --check`: **ผ่าน**
- `ruff check gateway/app/backend/main.py worker/app/backend/main.py`: **ไม่ผ่าน 89 errors**
- `ruff format --check ...`: **ไม่ผ่าน; 2 files would be reformatted**
- import/OpenAPI: import ได้, 71 routes/50 paths, แต่ missing symbols ตามด้านบน

Unit tests จึงพิสูจน์เฉพาะ contract/unit subset ไม่ได้พิสูจน์ lifespan, real worker dispatch, authenticated upload, media output hash หรือ download stream

## 6. Security/documentation findings

ค่าลับไม่ได้ถูกคัดลอกลง report นี้ แต่ source scan พบว่าต้องแก้ก่อน release:

- `deploy/install.sh:172-186` เขียน environment และมี literal database password assignment ใน installer
- `gateway/app/backend/services/db.py:20` มี default database password ใน code
- `gateway/app/backend/deps.py:41` มี default internal token ใน code
- `CLAUDE.md:205` อธิบาย default token ที่ไม่ควรใช้ใน production
- `README.md` และ `CLAUDE.md` มีตัวอย่าง env/token ที่ต้องทำให้เป็น placeholder และย้ำ secret manager/rotation

เอกสารที่แก้ต้องบอกเพียงชื่อตัวแปร, แหล่ง secret, rotation procedure และ permission; ห้ามใส่ค่าจริงใน Markdown, Notion, API log, screenshot หรือ browser storage. ควร rotate ค่าที่เคยอยู่ใน source หลังแก้ code และตรวจ git history ตามนโยบายของทีม

## 7. จุดที่ควรอัปเดตเอกสาร — จัดลำดับ

ระหว่างรอบนี้มี process อื่น commit `ba921c0` ซึ่งอัปเดตเอกสาร 9 ไฟล์และเพิ่ม `docs/V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md` แล้ว จากนั้นรอบนี้ตรวจซ้ำและเติม correction ที่มีหลักฐานตรงจาก public surface ได้แก่ repo HEAD/code baseline, `/v3api/openapi.json`, `/v3api/healthz`, root `/healthz` HTML และ link มาที่ evidence package นี้ การแก้ที่ยังไม่ commit แสดงใน `git status` และต้อง review ร่วมกับ `CLAUDE.md` ที่มี user changes

| ลำดับ | ไฟล์/พื้นที่ | สิ่งที่ผิดหรือขาด | สิ่งที่ควรเขียนเพิ่ม |
|---|---|---|---|
| P0 | `docs/Readme.md`, `docs/index/index.md`, `docs/memory/memory.md`, `docs/timesheet/timesheet.md`, `docs/readme/readme.md`, `docs/debug_db/debug_db.md`, `docs/report/report.md` | เดิมไม่มี; รอบนี้สร้าง master/daily/report ตาม Notion และเติม pointers แล้ว | รักษา project identity, repo/branch, source of truth, current/live split, timesheet และ known blockers; ต้อง resolve exact Notion project registry และ update pointersทุก closeout |
| P0 | `README.md` | มี public-prefix warning แล้ว แต่ endpoint table เดิมยังเป็น legacy/target และต้องใช้ release marker | คง dual-prefix map, OpenAPI URL จริง, root catch-all mismatch, router/service tree, generated systemd, current status และ safe curl |
| P0 | `docs/README_TH.md` | มี current/source-live chronology แล้ว แต่ต้องรักษาลิงก์ evidence ล่าสุดและไม่ยืนยัน M4 claims ที่ไม่มีหลักฐานในรอบนี้ | ระบุ live `/api` + `/v3api`, API health/OpenAPI URLs, frontend source แยก checkout, report lineage |
| P0 | `docs/V3_CURSOR_API_DEVELOPMENT_GUIDE_TH.md` | source tree/blocker caveat ดีขึ้น แต่ public-vs-direct health distinction และ local Python/venv matrix ต้องชัด | tree ใหม่, package import, exact test/lint commands, Python 3.9 local vs CI/production 3.12, non-blocking CI caveat, no generic `/api/{tc}/render` claim |
| P0 | `docs/V3_CURSOR_API_USER_GUIDE_TH.md` | มี release-blocked banner/target route แล้ว แต่ยังต้องแยก live UI multipart route กับ target dynamic JSON อย่างเด่น และไม่ใช้ `/healthz` public แบบกำกวม | ใช้ public base อย่างมี scope, multipart `/api/render/<tc>`, auth requirement, poll job, worker_id/output evidence, quoted filename/no shell glob, download only after terminal success |
| P0 | `docs/V3_CURSOR_API_OPERATIONS_RUNBOOK_TH.md` | มี no-deploy/generated-unit note แล้ว; ต้องคง direct host health กับ public `/v3api/healthz` แยกกัน | `/v3api/healthz`, `/api/health`, `/v3api/openapi.json`, service ExecStart, `/opt/v3-cursor-api`, current-vs-history cluster, rollback/source commit rules |
| P0 | `docs/V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md` | เพิ่งเพิ่มและแก้ snapshot metadata แล้ว; ต้อง review M4 claims แยกจาก Green Cutdee probe และ keep this report link | source/live/frontend separation, current API evidence, root-prefix mismatch, exact release lineage, release gates |
| P1 | `docs/V3_CURSOR_API_DEEP_DIVE_TH.md` | เป็น baseline 2026-08-18 และยังมี monolithic/current claims | ใส่ banner `historical snapshot`, link report ปัจจุบัน, แยก deployed `f6299fa` กับ candidate `25e1032`, ไม่ rewrite historical result ทับ |
| P1 | `docs/V3_CURSOR_API_PIPELINE_GUIDE_TH.md` | ต้องทบทวน route/pipeline contract ให้ตรง UI TC01–TC06 และ source worker pipeline | per-TC inputs/outputs, no-file guard, authenticated 202 enqueue, terminal/output/download evidence, known route placeholders |
| P1 | `docs/V3_MAC_M4_SPEED_BENCHMARK_TH.md` | benchmark dated/release-specific | คงไว้เป็น historical benchmark, เพิ่ม release/source hash และห้ามใช้แทน production health/current media proof |
| P1 | `CLAUDE.md` | ไฟล์มี user changes อยู่แล้วและมี claims เกี่ยวกับ dispatch/download/dynamic route ที่ source ปัจจุบันขัดแย้ง | patch เฉพาะหลังแยก dirty changes; แก้ route map, source tree, security defaults, public health/OpenAPI, real acceptance gate |
| P1 | `.github/workflows/ci.yml` | trigger แค่ `main`; lint และ pytest ใช้ `|| echo/true`; lint ตรวจแค่ main.py | ให้ refactor branch/PR รัน, ตรวจ routers/services, fail เมื่อ lint/test fail, เพิ่ม startup smoke/lifespan check |
| P1 | `deploy/install.sh` | installer สร้าง service/config จริง แต่ docs อ้างไฟล์ tracked และมี secret defaults/literal | แยก generated artifact จาก tracked source, เอา literal/default secrets ออก, document secret injection/rotation, record exact ExecStart |
| P1 | project registry/Notion | search ไม่พบ exact current registry page `green.cutdee.com` | สร้าง/resolve registry entry ที่มี repo root, branch, UI/API/OpenAPI, server path, runtime, version, permissions โดยไม่ใส่ secret |

### ลำดับการแก้เอกสารที่ปลอดภัย

1. สร้าง current project registry + master docs/index ก่อน เพื่อให้เอกสารใหม่ชี้ source of truth เดียวกัน
2. แก้ `README.md` และ `docs/README_TH.md` ให้ route/public surface ไม่หลอกผู้ใช้
3. แก้ Development/User/Operations ให้ตรงกับ production และแยก “observed” กับ “intended”
4. ติดป้าย Deep Dive/Benchmark เป็น historical แล้ว link current report แทนการเขียนทับผลเก่า
5. แก้ CLAUDE/CI/deploy หลัง source blocker และ secret handling มี owner/commit ที่ตรวจได้
6. หลัง code fix ต้องทำ authenticated media rerun ใหม่: UI screenshot + API request/response + job_id/worker_id + output hash/ffprobe/decode + download response

## 8. RCA และ corrective plan

### RCA-01: Documentation drift

- **Cause:** refactor branch (`25e1032`) และ production (`f6299fa`) ไม่ใช่ lineage เดียวกับ docs; frontend deployed แยกจาก checkout นี้
- **Effect:** route, version, service path, OpenAPI/health URL และ architecture ใน docs ไม่ตรงกัน
- **Smallest safe fix:** สร้าง current snapshot + source-of-truth header ก่อนแก้ prose; ห้าม merge/overwrite historical docs แบบไม่มี attempt history

### RCA-02: Refactor incomplete

- **Cause:** Phase 1–3 แยก routers/services แต่เหลือ lifecycle/route-time imports/placeholder handlers
- **Effect:** unit tests ผ่านแต่ startup/real job/download ยังไม่พิสูจน์
- **Smallest safe fix:** repair undefined lifecycle symbols and relative imports; wire upload → worker dispatch → status → output/download; add endpoint integration tests, then rerun UI+API pair

### RCA-03: Release identity mismatch

- **Cause:** UI static asset label `v1.1.1` ไม่ตรง API `1.2.0/f6299fa`
- **Effect:** user/support ไม่รู้ว่ากำลังใช้ code/release ใด
- **Smallest safe fix:** expose one build metadata source to UI/API, update asset cache/versioning, and add a release-lineage check to acceptance

### RCA-04: Secret/config hygiene

- **Cause:** installer/code/docs มี default หรือ literal secret assignments
- **Effect:** production credential leakage/rotation risk และ docs เสี่ยงคัดลอกค่าไปใช้ผิด
- **Smallest safe fix:** remove literal/default secrets, inject via protected environment/secret manager, rotate exposed values, scan source/history, document only variable names and permissions

## 9. Acceptance boundary

สิ่งที่ **ผ่านในรอบนี้**: public home/UI state, unauthenticated guards, public health/version/OpenAPI/cluster probes, source/test/lint inventory, Dedicated Server service/path observation, และ evidence package ที่ผูก screenshot/API ครบ 8 คู่

สิ่งที่ **ยังไม่ผ่าน/ยังไม่ประเมิน**: authenticated upload, real worker selection for a specific job, gateway-to-worker dispatch, job completion, output file existence, output hash/ffprobe/decode, and authenticated download. ห้ามใช้สถิติ public หรือ history self-report แทน media proof เหล่านี้

## 10. Artifact index

- [summary.json](summary.json)
- [test_matrix.json](test_matrix.json)
- [report.html](report.html)
- [screenshots](screenshots/)
- [api](api/)
- [pairs](pairs/)
- [local_validation.log](logs/local_validation.log)
- [source_and_docs_findings.log](logs/source_and_docs_findings.log)
- [remote_server_readonly.log](logs/remote_server_readonly.log)
- [notion_context.md](logs/notion_context.md)
- [concurrent_docs_reaudit.log](logs/concurrent_docs_reaudit.log)
- [worker_lifecycle_recheck.log](logs/worker_lifecycle_recheck.log)
