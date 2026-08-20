# รายงานศึกษาเชิงลึก — green.cutdee.com Dedicated Server

## Verdict

**STUDY_COMPLETE_WITH_CRITICAL_FINDINGS / NO_PRODUCTION_PASS**

ศึกษาจริงแบบ read-only บน 103.253.75.161 และ https://green.cutdee.com เมื่อ 2026-08-18 Asia/Bangkok. ไม่ได้แก้ service, restart, firewall, DNS, Nginx, database, source, output หรือข้อมูลใด ๆ และไม่เก็บ password/API token/raw env.

ข้อสรุปหลัก: public UI เปิดได้ แต่ UI/API ไม่สอดคล้องกันจริง — Browser แสดง 0 videos ขณะที่ GET /api/outputs?page=1&limit=5 ตอบ HTTP 200 และมี 100 records. Legacy instance /opt/green.cutdee.com มี migration checksum mismatch และ systemd crash-loop แต่มี process root แบบ detached บน port 21002 ทำให้ /old/health ตอบ 200 จาก process ที่ไม่ได้อยู่ใต้ service หลัก.

## Scope และ project identity

- Project ID: green.cutdee.com
- Project Name: green.cutdee.com
- Repo Root: /Users/sj88/Documents/codex/V3_cursor_API (reference context only; no source change)
- Environment: production dedicated server, read-only study
- Server: 103.253.75.161, hostname server
- Public UI: https://green.cutdee.com/
- API aliases: /api/* and /v3api/*
- Origin: Nginx -> /var/www/green/v3 and gateway 127.0.0.1:8788
- Legacy: /old/* -> 127.0.0.1:21002
- Source of truth: fresh origin SSH/DNS/TLS/HTTP/browser probes plus deployed source/config
- Machine: CPU=12th Gen Intel Core i9-12900K|CORE=24|RAM=64GB|GPU=Intel AlderLake-S GT1 iGPU|SSD=1TB WDC WDS100T2B0C
- Scope hash: 420a3edd2ec5a323f62116faf407f3c33c0fd060cffc86bfe7113cceab82eaf7

Project identity ไม่ได้เดาจาก repo เดิม แต่ยืนยันจาก /opt/green.cutdee.com, systemd description, Nginx server_name green.cutdee.com และ public URL.

## Notion/context sources

- [Second Brain Operating Rules](https://www.notion.so/3435a17a475f818bae05c4dca1bb6aba) — project identity, source-of-truth, no raw secrets, evidence/closeout.
- [green1.cutdee.com workspace study](https://app.notion.com/p/f0e5a17a475f8279a2ff01b7fc721cf1?pvs=204) — historical related project, not current live evidence.
- [103.253.75.161 server registry](https://app.notion.com/p/3525a17a475f8168a38bd94807fef2d7?pvs=204) — historical IP context.
- [green.sj88ai.com deep study](https://app.notion.com/p/3855a17a475f81029952cf2bd4f2d286?pvs=204) — historical runtime context only.

## Analyze -> Plan -> Execute -> Test -> Evaluate

1. แยก live primary, V3 gateway/worker, legacy 21002 และ historical references.
2. ตรวจ DNS/TLS/origin, host hardware/listeners/firewall, Nginx, systemd, logs และ source lineage.
3. ทำ safe GET/HEAD และ real browser navigation/screenshot.
4. ไม่ส่ง POST/PUT/PATCH/DELETE/render/upload เพราะผู้ใช้ขอศึกษาและกำหนด read-only boundary.
5. Evaluate จากคู่ UI/API จริง; production render acceptance ยัง NOT_EVALUATED_READ_ONLY_NO_INPUT_MEDIA.

## 1) Host และ resource

- Ubuntu 24.04.4 LTS; kernel 6.8.0-107-generic; x86_64.
- CPU i9-12900K, 24 logical CPUs; RAM 62 GiB reported, approximately 64 GB installed.
- Disk WDC WDS100T2B0C-00PXH0, 931.5G; root 914G, used 474G, free 402G, 55%.
- GPU พบเพียง Intel AlderLake-S GT1 iGPU; ไม่พบ discrete NVIDIA GPU.
- First probe load 9.79, 9.96, 10.02; swap used 5.6GiB/8.0GiB.
- Local V3 worker รายงาน libx264; cluster มี remote RTX 5060 Ti/RTX 2050 และ Apple M4 worker แยกต่างหาก.

Firewall evidence:

- ufw command ไม่ติดตั้ง.
- nftables INPUT policy เป็น accept.
- iptables: INPUT ACCEPT, FORWARD ACCEPT, OUTPUT ACCEPT.
- มี listener จำนวนมากบน 0.0.0.0/[::], ไม่ใช่ default-deny perimeter.

## 2) DNS, TLS และ Nginx

- DNS A: 104.21.64.128, 172.67.150.217; AAAA เป็น Cloudflare.
- Public certificate CN=cutdee.com, SAN *.cutdee.com/cutdee.com, หมดอายุ 2026-10-13.
- Origin certificate CN=green.cutdee.com, หมดอายุ 2026-10-28.
- Public HTTPS และ direct-origin HTTPS / ตอบ 200; HTTP redirect 301 ไป HTTPS.

Active vhost /etc/nginx/sites-available/green.cutdee.com.conf:

    /                  -> /var/www/green/v3
    /api/              -> http://127.0.0.1:8788/api/
    /v3api/            -> http://127.0.0.1:8788/
    /old/              -> http://127.0.0.1:21002/
    /gs-api/           -> https://greenstats.sj88ai.com/api/v1/
    /cluster-status    -> /var/www/v3-dashboard.html

nginx -t syntax/test successful แต่มี warnings ซ้ำจำนวนมาก: protocol options redefined และ conflicting server name สำหรับ green.cutdee.com, cutdee.com, www.cutdee.com, green.sj88ai.com, green1.cutdee.com. ใน sites-enabled มี backup vhost dated .bak อยู่ใน include path จึงเกิด route ambiguity.

## 3) Live primary V3

v3-cursor-api-gateway.service active/running ที่ /opt/v3-cursor-api/gateway, port 8788, และ v3-cursor-api-worker.service active/running ที่ /opt/v3-cursor-api/worker, port 8789.

- Deployed HEAD: f3ef05e7a83448ac3283f92c6df668e6ac4fbe12.
- Local source hashes เท่ากับ deployed hashes สำหรับ gateway/worker main.py.
- Tracked diff บน server = 0 แต่มี untracked 24 files ได้แก่ backup source, test media, test output และ temporary artifacts; จึงไม่ใช่ clean release tree.
- Gateway log มี health polling ของ workers และ worker log มี TC01 CPU/libx264 render สำเร็จในประวัติ.

Safe GET /api/cluster/health:

- ok=true, healthy=4, total=5, total_capacity=9, active_jobs=0.
- Unhealthy worker: wsl-rtx3050-edit.
- Response เปิดเผย worker IDs, internal URLs, encoder readiness และ system metrics โดยไม่ต้อง login.

## 4) Legacy /opt/green.cutdee.com RCA

sj88-green-cutdee.service:

- enabled; intended 127.0.0.1:21002; MainPID=0; ActiveState=activating; SubState=auto-restart.
- restart counters observed 310313, 310324, later 310406.
- journal error: RuntimeError: Migration checksum mismatch for phase2_user_system_v1.
- DB checksum daec5c01530634ad71678c702a478e599b822149dfa495202e6429baa0bcce49.
- code checksum 730afc7827880aa226bdd33415b62f9b4873439bb091f192ee7cbb3c6cca4281.
- failure occurs in create_app() -> ensure_phase4_schema() -> _ensure_migration().

ขณะเดียวกัน port 21002 มี process แยก:

- PID 2321697, parent 2321696, user root.
- started 2026-08-05 12:28:47 UTC.
- cgroup /user.slice/user-0.slice/session-810635.scope.
- detached uvicorn จาก /opt/green.cutdee.com/WebAppCodex/backend, log /tmp/green.log.

ดังนั้น /old/health ที่ตอบ 200 ไม่ใช่ proof ว่า systemd service ผ่าน แต่เป็น unmanaged root process ที่ทำให้ health หลอกตาและ lifecycle/audit/rollback ควบคุมไม่ได้. Log ยังมี historical scanner requests เช่น /.git/config, /robots.txt, /auth/me และ unauthenticated API probes. Current direct checks ไม่พบ raw .git/config ถูกส่งกลับ; ได้ UI fallback แทน.

## 5) UI/API pair findings

### Browser UI จริง

- title: 🟢 SJ88 Green Screen — ตัดจอเขียว • ออโต้ซูม • แบทช์
- visible tabs: TC01–TC06 และ Stats
- static UI version: V1.0.0.20
- UI: ffmpeg: ok, Idle, 0 products -> 0 outputs, 0 videos
- GPU badge: No GPU detected — using CPU
- console: 0 errors/warnings

### Safe public API

| Endpoint | HTTP | Observation |
|---|---:|---|
| /api/health | 200 | gateway 1.1.0; worker aggregation |
| /api/cluster/health | 200 | 4/5 healthy; capacity 9 |
| /api/outputs?page=1&limit=5 | 200 | top key outputs; 100 records |
| /v3api/healthz | 200 | gateway liveness |
| /v3api/api/cluster/health | 200 | path alias works |
| /v3api/openapi.json | 200 | public API schema |
| /old/health | 200 | unmanaged legacy process |

### Confirmed mismatch A: gallery

UI source /var/www/green/v3/index.html:2520-2530 reads d.total, d.pages, and d.files. Gateway source /opt/v3-cursor-api/gateway/app/backend/main.py:946-960 returns {"outputs": [...]} with job_id, filename, size, finished_at, worker.

ผลที่เห็นจริง: API มี 100 output records แต่ UI ใช้ d.files || [] จึงแสดง 0 videos. นี่คือ paired UI/API inconsistency ที่ยืนยันด้วย Browser screenshot + API response.

### Confirmed mismatch B: version/health

- UI static version v1.0.0.20; /api/version returns 1.1.0-cluster.
- UI checks h.recommended_encoder; gateway health response ไม่มี field นี้.
- UI จึงแสดง CPU badge แม้ cluster health รายงาน remote NVENC/VideoToolbox.

### Confirmed mismatch C: contract/docs

- UI แสดง TC01–TC06 และ gateway /api/render/{tc} รับทั้ง 6 modes.
- /api/config claims six TCs.
- deployed README roadmap ยังระบุ multi-mode TC02/TC03 เป็น currently TC01 only.
- UI มี stale comment ว่า only TC01 enabled.
- Real render TC01–TC06 ยังไม่ถูกประเมิน เพราะไม่ส่ง input media/ไม่ทำ mutation.

## 6) Security findings

### S1 — anonymous mutation path

Gateway _verify_user() lines 169-177: ไม่มี Authorization แล้วคืน user anon. อีกทั้ง UI-compatible /api/render/{tc}, /api/jobs/upload, cancel/pause/resume, /api/outputs, /api/download/{file_path} ไม่มี auth dependency ใน route function. Public OpenAPI ไม่มี global security scheme.

ผมไม่ได้ส่ง mutating request ดังนั้นยังไม่ claim live upload/render success แต่ source/config หลักฐานชี้ว่า anonymous operations มีความเสี่ยงสูงและต้องแก้ก่อนเปิด production.

### S2 — public metadata

Unauthenticated GET เปิดเผย worker IDs/URLs, encoder capabilities, health state, system metrics และ 100 output job metadata.

### S3 — host perimeter

INPUT policy เป็น ACCEPT และมี public listeners จำนวนมากบนเครื่องเดียวกับหลาย production services. Blast radius สูง.

### S4 — unmanaged root runtime

Detached root process บน 21002 ไม่ตรงกับ systemd MainPID และใช้ /tmp/green.log; health/lifecycle/audit ไม่เป็น single source of truth.

### S5 — Nginx ambiguity

Backup vhost files ใน enabled include path ทำให้ nginx -t เตือน conflicting server names ซ้ำ และอาจเลือก server block ต่างจากที่ operator คิด.

## 7) Evidence gate

- Browser screenshots: 5/5.
- API request/response/timing: complete for PUBLIC_SURFACE_001.
- Pair completeness: 100%.
- Pair consistency: 71.43% (5/7 checks); gallery count and version/health shape fail.
- Critical errors: 6.
- Render flow: NOT_EVALUATED_READ_ONLY_NO_INPUT_MEDIA.
- Overall: FAIL_WITH_CRITICAL_FINDINGS.

ไม่มี synthetic screenshot และไม่มี production write เพื่อสร้าง PASS.

## 8) Corrective plan

1. Freeze one live source of truth: V3 API cluster or legacy WebApp; stop advertising both.
2. Remove /old/ from public route until DB checksum is reconciled and systemd is the only owner of 21002.
3. Backup/verify DB before any mutation; do not edit migration records blindly.
4. Move dated Nginx backups out of enabled include path; re-test route by direct origin request.
5. Enforce auth on upload/render/cancel/download and declare OpenAPI security; protect or sanitize health/output metadata.
6. Unify gallery contract (files/total/pages vs outputs) and version/health schema; add UI/API contract tests.
7. Replace fake/static disk and missing encoder fields with documented real values.
8. Remove detached root process only with explicit approval and preserved evidence.
9. Rerun with real media and paired Browser/API evidence for TC01-TC06; accept only at 100% pair completeness/consistency and zero critical errors.

## AI Full Dev recording

- Preflight activity 12063, POST /api/activities HTTP 201.
- During-analysis activity 12065, POST /api/activities HTTP 201.
- Closeout activity 12066, POST /api/activities HTTP 201; status done for the study, with production acceptance explicitly blocked by the critical findings above.
- No password or raw secret is in this report/bundle.

## Artifact index

- report.md, report.html, summary.json, test_matrix.json
- screenshots/TC_PUBLIC_01..05*.png
- api/PUBLIC_SURFACE_001/{request,response,timing}.json
- pairs/PUBLIC_SURFACE_001__binding.json
- logs/ browser network/console, DNS/TLS, host, Nginx, service RCA, source lineage
