# V3_cursor_API

Green screen (chroma key) video processing **API cluster** based on [lnwsj/V3_cursor](https://github.com/lnwsj/V3_cursor).

## Architecture

```
1 machine (Gateway)         →  receives uploads, queues jobs
                                →  load-balances to N workers
N machines (Workers)        →  run ffmpeg green screen render
                                →  return MP4

Live: https://green.cutdee.com/v3api/...
Path-based: /v3api/* → gateway:8788 → worker(s)
```

## Components

| Path | Role | Port |
|---|---|---|
| `gateway/app/backend/main.py` | Gateway bootstrap, OpenAPI and router registration | 8788 |
| `gateway/app/backend/routers/` | Auth, jobs, cluster, uploads, users, system and WebSocket routes | — |
| `gateway/app/backend/services/` | DB, users, jobs, workers and metrics services | — |
| `worker/app/backend/main.py` | Runs ffmpeg green screen render | 8789 |
| `worker/app/backend/core/` | V3_cursor `core/` (copied, renders green screen) | — |
| `shared/` | Shared Pydantic models (planned; not present in current tree) | — |
| `deploy/install.sh` | Systemd installer | — |
| `deploy/systemd/` | Generated service destination (units are created by `deploy/install.sh`) | — |

## Documentation

คู่มือภาษาไทยแยกตามบทบาทอยู่ที่ [`docs/README_TH.md`](docs/README_TH.md)

- API usage: [`docs/V3_CURSOR_API_USER_GUIDE_TH.md`](docs/V3_CURSOR_API_USER_GUIDE_TH.md)
- Pipeline/settings: [`docs/V3_CURSOR_API_PIPELINE_GUIDE_TH.md`](docs/V3_CURSOR_API_PIPELINE_GUIDE_TH.md)
- Operations runbook: [`docs/V3_CURSOR_API_OPERATIONS_RUNBOOK_TH.md`](docs/V3_CURSOR_API_OPERATIONS_RUNBOOK_TH.md)
- Developer guide: [`docs/V3_CURSOR_API_DEVELOPMENT_GUIDE_TH.md`](docs/V3_CURSOR_API_DEVELOPMENT_GUIDE_TH.md)
- Architecture deep dive: [`docs/V3_CURSOR_API_DEEP_DIVE_TH.md`](docs/V3_CURSOR_API_DEEP_DIVE_TH.md)
- Current source audit: [`docs/V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md`](docs/V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md)

> **Current source warning:** `refactor-base / 25e1032` is not a production-ready Gateway release. The extracted Gateway routers still have startup, auth, upload and worker-dispatch blockers. Production currently runs a separate `1.2.0 / f6299fa` snapshot. See the current-state audit before using the examples below as an operational contract.

## Public endpoint prefixes

The deployed site has two surfaces that must not be conflated:

- `https://green.cutdee.com/` — frontend UI. The UI JavaScript calls `/api/...` routes.
- `https://green.cutdee.com/v3api/` — public API proxy. The deployed OpenAPI schema is `/v3api/openapi.json` and JSON liveness is `/v3api/healthz`.
- Root `/healthz` and `/openapi.json` are frontend HTML catch-all responses on the public host; `/api/openapi.json` is not the schema URL and currently returns 404.

## Endpoints (legacy/API contract)

The endpoint list below describes the intended/legacy contract. Verify the current route table and release marker before treating any render route as operational; the current `refactor-base` Gateway is release-blocked.

### Public (Bearer `<API_KEY>`)

- `GET  /healthz` — gateway liveness
- `GET  /api/cluster/health` — all workers status
- `POST /api/v1/uploads/{role}` — upload product/background/cover/audio
- `POST /api/v1/jobs` — create render job
- `GET  /api/v1/jobs/{id}` — get job status
- `GET  /api/v1/jobs/{id}/download/{file}` — download output MP4

### Internal (X-Cutdee-Internal: <token>, gateway↔worker only)

- `GET  /health` — worker liveness
- `GET  /v1/capabilities` — worker GPU/encoder
- `POST /v1/jobs/{id}/upload/{role}` — receive file
- `POST /v1/jobs/{id}/render` — start render
- `GET  /v1/jobs/{id}/status` — status
- `GET  /v1/jobs/{id}/output?filename=...` — download
- `POST /api/cluster/workers/reload` — reload workers.json

## Quick start (development)

```bash
# 1. Clone + install
git clone https://github.com/lnwsj/V3_cursor_API.git
cd V3_cursor_API
bash deploy/install.sh all     # install gateway + worker on same host

# 2. Verify
curl http://127.0.0.1:8788/healthz
curl http://127.0.0.1:8789/health
curl http://127.0.0.1:8788/api/cluster/health
```

## Production setup

```bash
# On gateway host (1 machine)
bash deploy/install.sh gateway

# On each worker host (N machines)
bash deploy/install.sh worker
# Edit /etc/v3-cursor-api/worker.env to set WORKER_ID
# Add to gateway's /var/lib/v3-cursor-api/gateway/workers.json
curl -X POST http://127.0.0.1:8788/api/cluster/workers/reload \
  -H "X-Cutdee-Internal: $CUTDEE_INTERNAL_TOKEN"
```

## nginx vhost (path-based, preserves existing /)

Add to existing `green.cutdee.com.conf`:

```nginx
location /v3api/ {
    proxy_pass http://127.0.0.1:8788/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    client_max_body_size 200m;
}
```

## E2E test (curl, accepted release only)

Do not run a generic copy/paste E2E command against `refactor-base`. The current extracted Gateway does not yet provide a verified worker dispatch and download flow. Use [`docs/V3_CURSOR_API_USER_GUIDE_TH.md`](docs/V3_CURSOR_API_USER_GUIDE_TH.md) together with a release-specific acceptance report after the release gates pass.

Acceptance ต้องตรวจอย่างน้อย: authenticated upload, `202` enqueue, terminal status, `output_files`, codec/resolution/duration/audio ด้วย `ffprobe` และ download ด้วยชื่อไฟล์จริงจาก manifest

## Database

Database defaults differ between source, installer and historical environments. Set all `CUTDEE_PG_*` values explicitly from the deployment secret/config source and verify the generated service environment before starting Gateway.

PostgreSQL via PgBouncer (transaction mode, SCRAM-SHA-256).

- DB: `v3_cursor_api` (new database, create with `CREATE DATABASE v3_cursor_api;`)
- User: `postgres` (or dedicated `v3_cursor_api` user)
- Table: `v3_jobs` (auto-created on gateway startup; PostgreSQL schema remains the database default)

PgBouncer config: add database to `/etc/pgbouncer/pgbouncer.ini`:
```ini
[databases]
v3_cursor_api = host=127.0.0.1 port=5432 dbname=v3_cursor_api
v3_cursor_api_pool = host=127.0.0.1 port=5432 dbname=v3_cursor_api pool_size=10
```

## Environment

`/etc/v3-cursor-api/gateway.env`:
```bash
CUTDEE_INTERNAL_TOKEN=<from-secret-store>
CUTDEE_API_KEYS=<comma-separated-client-keys>
CUTDEE_ADMIN_API_KEY=<optional-admin-key>
CUTDEE_API_VERSION=1.2.0
GATEWAY_PORT=8788
GATEWAY_DATA_DIR=/var/lib/v3-cursor-api/gateway
CUTDEE_PG_HOST=127.0.0.1
CUTDEE_PG_PORT=6432
CUTDEE_PG_NAME=v3_cursor_api
CUTDEE_PG_USER=postgres
CUTDEE_PG_PASSWORD=<from pgbouncer>
```

`/etc/v3-cursor-api/worker.env`:
```bash
CUTDEE_INTERNAL_TOKEN=<must match gateway>
WORKER_PORT=8789
WORKER_ID=<unique-id-per-host>
WORKER_DATA_DIR=/var/lib/v3-cursor-api/worker
WORKER_MAX_CONCURRENT=2
WORKER_MAX_QUEUE=4
V3_API_VERSION=1.2.0
V3_BUILD_COMMIT=<release-commit>
```

## Adding workers

Edit `/var/lib/v3-cursor-api/gateway/workers.json`:
```json
{
  "workers": [
    {"id": "i9-64gb-cpu-01", "url": "http://127.0.0.1:8789", "tier": "high", "max_concurrent": 4},
    {"id": "5060ti-01", "url": "http://110.164.146.205:8789", "tier": "mid", "max_concurrent": 2},
    {"id": "hub-rtx3050", "url": "http://157.85.107.40:8789", "tier": "low", "max_concurrent": 1}
  ]
}
```

Then reload:
```bash
curl -X POST http://127.0.0.1:8788/api/cluster/workers/reload \
  -H "X-Cutdee-Internal: $CUTDEE_INTERNAL_TOKEN"
```

## Adding workers via Tailscale (direct, bypass hub)

Workers behind NAT can be reached directly via Tailscale instead of relaying through the hub. Both gateway and worker must be on the same Tailscale tailnet.

**On each worker** (one-time):
```bash
# Get a reusable auth key from https://login.tailscale.com/admin/settings/keys
TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx \
  TS_HOSTNAME=sjnb3050ti-worker \
  bash deploy/install.sh worker
```

The installer will auto-install Tailscale and authenticate. To skip Tailscale on a worker that already has it: `SKIP_TAILSCALE=1 bash deploy/install.sh worker`.

**On the gateway** (one-time):
```bash
TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx \
  TS_HOSTNAME=sj88-green-cursor \
  bash deploy/install.sh gateway
```

After both nodes are on the tailnet, edit `workers.json` to use the Tailscale IP instead of the hub URL:
```json
{
  "id": "sjnb3050ti-rtx3050",
  "url": "http://100.120.135.44:8789",
  "tier": "low",
  "max_concurrent": 1
}
```

This bypasses the hub's reverse SSH tunnel (port `55523`) and reduces latency by ~10ms. The hub tunnel can stay as a fallback if you append a secondary entry once the gateway supports multi-URL workers.

## Python version

The pinned `pydantic==2.9` requires Python **≤ 3.13** (pydantic-core 2.23 / pyo3 0.22 both fail on Python 3.14+). The installer detects the system Python and automatically falls back to Python 3.12 via `uv` on Ubuntu 26.04+ where the default `python3` is 3.14. No manual action needed.

## Green screen settings

`GreenSettings` from V3_cursor (49KB module) — supports:
- Resolution: width, height, fps
- Bitrate: 6000k default
- Encoder: auto-detect per platform (current M4 prefers `hevc_videotoolbox`; NVIDIA workers prefer NVENC; CPU fallback is `libx264`)
- Chroma key: key_color (hex), similarity, blend, despill
- Audio source: product | background | none
- Cover overlay (optional intro card)

## Roadmap

- [x] WebSocket route exists in source; production E2E acceptance is still pending
- [ ] Resume checkpoint (currently no resume on worker crash)
- [x] Multi-mode TC01-TC06 pipeline dispatch
- [ ] Full production media acceptance matrix for TC01-TC06
- [x] Portal/status/dashboard routes exist in source; production E2E acceptance is still pending
- [ ] Cloudflare R2 / S3 storage for outputs (currently local disk)
- [ ] Stripe topup + credit system (like api.cutdee.com)

## Architecture decisions

- **Path-based `/v3api/` proxy** — preserves existing `green.cutdee.com` AutoMix app on `/`
- **PG via PgBouncer** — same as cutdee-cluster (api.cutdee.com), reuses existing infrastructure
- **Bounded worker executor** — Worker accepts jobs quickly and runs FFmpeg outside the FastAPI event loop; Gateway monitors status asynchronously
- **Job state in PG** — gateway is stateless for HA (any number of gateways can share state)
- **Workers own their files** — gateway forwards uploaded bytes, worker stores locally (saves bandwidth on hub)
- **Cooperative control** — cancel/pause/resume signals are carried to the Worker; a worker restart still requires job recovery policy

## Logs

- Gateway: `tail -f /var/log/v3-cursor-api/gateway.log`
- Worker: `tail -f /var/log/v3-cursor-api/worker.log`
- Job data: `/var/lib/v3-cursor-api/worker/jobs/{job_id}/`
