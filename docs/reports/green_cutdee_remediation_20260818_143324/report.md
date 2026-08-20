# Green Cutdee production remediation — 2026-08-18

## Verdict

**PASS_WITH_SCOPE_NOTE** for the remediation scope: gateway/API contract, frontend contract, authentication boundary, output ownership/path safety, Nginx target route, legacy service startup, SQLite integrity, worker availability reporting, and V3 origin exposure are fixed and verified in production.

This is **not** a full media-release PASS. A real media upload/render was not run in this round, and authenticated gallery UI was not evaluated because the browser was not given the API secret. The exact boundaries are recorded below; no missing proof is promoted to PASS.

## Target and source of truth

- Project: `green.cutdee.com`
- Public site: `https://green.cutdee.com`
- Dedicated server: `103.253.75.161`
- Local working copy: `/Users/sj88/Documents/codex/V3_cursor_API`
- Baseline study: [green_cutdee_dedicated_server_study_20260818_133756](../green_cutdee_dedicated_server_study_20260818_133756/report.md)
- Source of truth for this run: deployed file SHA256, systemd/Nginx runtime state, public HTTP probes, Browser DOM/screenshots, and paired API artifacts in this directory.
- Notion format reference consulted: `https://www.notion.so/3435a17a475f818bae05c4dca1bb6aba`; searches for `green.cutdee.com`, auth, and migration did not expose a newer exact production source, so live deployment evidence remains authoritative.

The local repo was already dirty. Existing modifications to `README.md`, `deploy/install.sh`, worker files, and untracked `.playwright-mcp/`, `CLAUDE.md`, and `docs/` were preserved. No reset, wholesale branch merge, commit, or unrelated staging was performed.

## Analyze → Plan → Execute → Test → Evaluate

### Analyze / RCA

1. Gateway accepted anonymous API access and returned compatibility shapes that did not match the deployed gallery. Download paths also lacked ownership/path controls.
2. Frontend displayed a generic `Error` for expected unauthenticated gallery access, used an old thumbnail contract, omitted TC05/TC06 gallery filters, and persisted the bearer token in localStorage.
3. Nginx loaded `.bak*` regular files and a backup symlink from `sites-enabled`, causing conflicting target names. `/old/` proxied to a legacy service that was crash-looping.
4. Legacy systemd crashed with `phase2_user_system_v1` checksum mismatch. Read-only inspection showed SQLite `integrity_check=ok` and the DB checksum was the raw SQL hash while current code expected a normalized hash. This was a checksum-algorithm compatibility bug, not proven schema corruption.
5. Worker slot `sj88ai-rtx2050-01` at `103.253.73.29:55522` failed `/health` connectivity while remaining enabled, producing a degraded cluster.
6. V3 gateway and worker listened on `0.0.0.0:8788/8789`; direct origin access bypassed Nginx.

### Execute / smallest safe fixes

| Problem | Fix | Evidence |
|---|---|---|
| Anonymous mutation/list/download | Configured bearer token + short-lived HttpOnly session; user ownership checks; safe filename/path validation; no anonymous fallback | `gateway/app/backend/main.py:205-303,356,1200-1320`; API 401/200 artifacts |
| Output schema mismatch | `/api/outputs` returns `files`, `outputs`, pagination, safe `path`; download proxy uses job ownership and worker output route | `gateway/app/backend/main.py:1200-1320`; `api/TC_AUTH_API_ONLY_NOT_EVALUATED_001` |
| Health leaked internals / malformed encoders | Sanitized health/cluster fields; enabled/disabled counts; encoder names only; no worker URL/GPU/system payload | `gateway/app/backend/main.py:263-276,652-680,1015-1044`; public health response |
| Generic gallery Error on normal 401 | UI now displays `Token required` and `ตั้งค่า API token ก่อนดู output` | `/tmp/green-cutdee-fix.eaCmhR/index.html:2609`; `screenshots/01_open_page.png` |
| Unsafe bearer persistence | Browser exchanges token directly for same-origin HttpOnly cookie; no `localStorage.setItem(AUTH_TOKEN_KEY, ...)` remains | `/tmp/green-cutdee-fix.eaCmhR/index.html:1463-1500`; deployed SHA in `summary.json` |
| TC05/TC06 gallery omission | Added `data-dir=tc05` and `data-dir=tc06`; visible UI now exposes All + TC01–TC06 | `/tmp/green-cutdee-fix.eaCmhR/index.html:901-908`; `test_matrix.json` |
| Nginx target conflicts / public legacy proxy | Moved seven backup files plus one backup symlink outside `sites-enabled`; target vhost has security headers; `/old/` returns 410 | `/tmp/green-cutdee-fix.eaCmhR/green.cutdee.com.conf:9-12,125-136`; Browser 410 screenshot/API |
| Legacy checksum crash loop | DB backup first; exact raw legacy hash is accepted and upgraded transactionally to normalized hash; other mismatches still fail | `/tmp/green-cutdee-fix.eaCmhR/legacy-schema.py:604-625`; SQLite integrity and systemd active evidence |
| Duplicate root legacy process | Stopped exact PIDs `2321696/2321697` after `ss` showed no active connections; systemd now owns 21002 as `www-data` | `logs/prod_runtime.log` |
| Unreachable worker | Disabled only `sj88ai-rtx2050-01`; health explicitly reports configured 5 / enabled 4 / healthy 4 / disabled 1 | `workers.json` SHA and gateway health response |
| Origin bypass | systemd drop-ins bind gateway/worker to 127.0.0.1; public site remains through Nginx | `logs/api_probes.log`, `logs/prod_runtime.log` |

### Test / paired evidence

All three gated testcases have complete Browser screenshot lists, API request/response/timing files, and binding files. Pair completeness and consistency are both **100%** for the gated set.

| Pair | Browser evidence | API evidence | Verdict |
|---|---|---|---|
| `TC_HEALTH_UI_001` | Page non-blank; `v1.1.1`, `h264_nvenc`, `402 GB free`; TC01–TC06 visible | `/api/health` HTTP 200; `status=ok`, 4/4 enabled healthy, encoder and disk match | PASS |
| `TC_AUTH_GUARD_UI_001` | Gallery says token required; API token button visible; Render disabled with empty files; idle state | `/api/outputs` without token HTTP 401 | PASS |
| `TC_MODE_MATRIX_UI_001` | TC02–TC06 each becomes active; empty input keeps Render disabled; TC06 audio count 0 | `/api/render/tc05` and `/api/render/tc06` without token each HTTP 401 | PASS_WITH_SCOPE_NOTE |
| `TC_LEGACY_ROUTE_SUPPLEMENTAL_001` | Browser heading `410 Gone` | `/old/` HTTP 410 | PASS (supplemental) |

The exact binding is in `pairs/*.json`. The minimum required state screenshots are:

- `01_open_page.png`
- `02_input_ready.png`
- `03_click_generate.png`
- `04_result_state.png`
- `05_audio_ready_or_error.png`

The `03`/`04` screenshots are intentionally unchanged: with no required input files, the Render control is disabled and no false job/result is created. This is an observed guard, not a fake render pass.

### Evaluate / production runtime

Final live checks at `2026-08-18T07:46:13Z` UTC:

- `v3-cursor-api-gateway.service`: active/running, `NRestarts=0`.
- `v3-cursor-api-worker.service`: active/running, `NRestarts=0`.
- `sj88-green-cutdee.service`: active/running as `www-data`, `NRestarts=0`.
- `nginx.service`: active/running, `NRestarts=0`.
- Listeners are loopback-only: `127.0.0.1:21002`, `127.0.0.1:8788`, `127.0.0.1:8789`.
- Public page and `/api/health` return HTTP 200.
- Direct external probes to ports 8788/8789 return HTTP 000 after loopback binding.
- `/old/` returns HTTP 410 and security headers are present.
- SQLite `PRAGMA integrity_check` returns `ok`.
- Nginx target warnings for `green.cutdee.com`/`green1.cutdee.com` are gone. Remaining protocol-option warnings are other co-hosts and were left untouched.

## Deploy hashes

See `summary.json` for the complete list. Key deployed hashes:

```text
gateway main.py  28176a999c0bd676b12d233a99ef848e2e71fb661d9acebbabf1f9f7dac30044
frontend index   15730f4dbb9d423425210670e74b194e99a3d0f707485887f1799550455b237d
frontend api.js  c1f49d325f6019d7e990468db0f26aec384b20d73a35aafdc2c0a53713b5961c
nginx vhost      cdf28a50f0917bf8b97279c7083a8c7b05fa9d34b59a433c20467633357c3ec0
legacy schema    470ca8bb461e2b4e7614d975d8f30a614a591a297dc34930e4d3ac8ded7b469d
workers.json     af1f820e3806b2ec9afc312607af0cdcec8c1ba73258cc61883071cecc29583c
```

Remote backups were timestamped and retained: gateway source, frontend index, Nginx vhost, legacy schema, worker registry, systemd drop-ins if pre-existing, and `/opt/green.cutdee.com/storage/backups/sj88_user_system_20260818T073037Z_pre_checksum_repair.db` with SHA256 `f8248c48c823d550360076a8aa2f85b08735950151457d21d5114e11d10c0267`.

## Explicit non-claims / next gate

- **NOT_EVALUATED:** real media upload and render through Browser + API. No output hash/ffprobe/decode claim is made.
- **NOT_EVALUATED:** authenticated gallery UI. The API contract was checked with redacted credentials, but the secret was not typed into Browser.
- **SCOPE NOTE:** one remote RTX2050 worker is disabled because its `/health` endpoint was unreachable; fixing that remote machine requires access/owner coordination for `103.253.73.29:55522`.
- **SCOPE NOTE:** host-wide firewall and co-host Nginx SSL warning cleanup require an explicit port/owner allowlist; changing them blindly could break unrelated services.

Next safe gate: provide/approve a synthetic test media pair and authorize browser auth entry if a full UI+API render acceptance is required. Then rerun the same artifact structure with real output hash, ffprobe, full decode, and paired UI job/result evidence.

## AI Full Dev logging

- Preflight activity: `12067`, HTTP 201.
- During activity: `12069`, HTTP 201, `ok=true`.
- Two failed payload attempts were not marked as success; details are in `logs/ai_full_dev.log`.
- Closeout activity: `12070`, HTTP 201, `ok=true`; activity `12069` status patched to `done`, HTTP 200.
