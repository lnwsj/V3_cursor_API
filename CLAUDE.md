# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

V3_cursor_API is a green-screen (chroma key) video processing **API cluster**, forked from the Tk UI at [lnwsj/V3_cursor](https://github.com/lnwsj/V3_cursor). The Tk UI has been replaced with two FastAPI services:

- **Gateway** (`gateway/app/backend/main.py`) — port 8788. Receives uploads, queues jobs in PostgreSQL, dispatches to workers over HTTP.
- **Worker(s)** (`worker/app/backend/main.py`) — port 8789. Run the ffmpeg pipelines and return the rendered MP4.

The worker ships a copy of the original V3 `core/` module — do not edit it as if it were independent; the upstream `V3_cursor` is the source of truth, and this copy is meant to track it.

Live deployment lives behind nginx at `https://green.cutdee.com/v3api/...` (path-based proxy on `127.0.0.1:8788`).

## Commands

The project is small enough to drive with `uvicorn` directly. There is a unit test suite (`pytest`) and `ruff` for the two entrypoint files; they run in CI but are not the main loop.

### Tests + lint

```bash
# Install test/lint deps (adds pytest, pytest-cov, pytest-asyncio, ruff, mypy, httpx)
pip install -r requirements-dev.txt

# Run the unit tests
pytest tests/unit -v
# Run a single test file
pytest tests/unit/test_planner.py -v
# Run a single test
pytest tests/unit/test_planner.py::test_tc02_plans_lens_matrix -v

# Lint the two entrypoint files (matches CI)
ruff check gateway/app/backend/main.py worker/app/backend/main.py
ruff format --check gateway/app/backend/main.py worker/app/backend/main.py
```

Test config: `pytest.ini` (testpaths=tests, asyncio_mode=auto, `slow`/`integration` markers).
CI: `.github/workflows/ci.yml` (Python 3.12, pytest + ruff on the two main entrypoints only).

### Install / deploy (systemd, single host dev)

```bash
bash deploy/install.sh all       # install gateway + worker on same host
bash deploy/install.sh gateway   # install gateway only
bash deploy/install.sh worker    # install worker only
```

The installer writes `gateway/.venv` and `worker/.venv`, creates `/etc/v3-cursor-api/{gateway,worker}.env`, generates systemd units, and seeds `/var/lib/v3-cursor-api/gateway/workers.json` with one local worker. Re-running is safe; env files and `workers.json` are only created if missing.

### Run from the repo without systemd

```bash
# Gateway
cd gateway
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
CUTDEE_INTERNAL_TOKEN=dev-internal-token-change-me \
GATEWAY_DATA_DIR=$PWD/data \
.venv/bin/python -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8788

# Worker (separate shell)
cd worker
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
CUTDEE_INTERNAL_TOKEN=dev-internal-token-change-me \
WORKER_ID=dev-1 \
WORKER_DATA_DIR=$PWD/data \
.venv/bin/python -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8789
```

To point a local gateway at a remote worker, edit `data/gateway/workers.json` (or set `CUTDEE_WORKERS_FILE`) and POST `/api/cluster/workers/reload` with the `X-Cutdee-Internal` header.

### Sanity check

```bash
curl http://127.0.0.1:8788/healthz
curl http://127.0.0.1:8789/health
curl http://127.0.0.1:8788/api/cluster/health
```

### PostgreSQL

The gateway auto-creates the `v3_jobs` table in schema `v3_jobs` on startup (via `_init_schema` in `gateway/app/backend/main.py`) and applies idempotent migrations. Database is reached through PgBouncer (transaction mode, SCRAM-SHA-256) on the standard `v3_cursor_api` database. Configured by `CUTDEE_PG_*` env vars; defaults assume PgBouncer on `127.0.0.1:6432`.

### Logs

```bash
tail -f /var/log/v3-cursor-api/gateway.log
tail -f /var/log/v3-cursor-api/worker.log
```

Per-job data is at `/var/lib/v3-cursor-api/worker/jobs/{job_id}/`; cleanup is `/v1/admin/cleanup?days=N` (internal only).

## Architecture

### Gateway layout

The gateway has been refactored (Phase 1–3) out of one ~2k-line `main.py` into a layout you should default to:

- `gateway/app/backend/main.py` — wiring only: FastAPI app construction, static-file mount, lifespan startup (`_init_schema`, etc.), and the `if __name__ == "__main__": uvicorn.run(...)` block.
- `gateway/app/backend/deps.py` — shared `Depends(...)` factories (DB pool, internal-token verify, current user, etc.).
- `gateway/app/backend/services/` — side-effecting modules: `db.py` (PG pool + queries), `jobs.py` (job-state reads/writes), `users.py`, `workers.py` (worker registry + dispatch), `metrics.py`. Pure functions and persistence belong here.
- `gateway/app/backend/routers/` — one file per resource, all wired from `main.py`: `auth.py`, `cluster.py`, `jobs.py`, `pages.py`, `system.py`, `uploads.py`, `users.py`, `ws.py`. New endpoints should land in a router here, not in `main.py`.
- `gateway/app/backend/templates/` — extracted HTML for the page routes (formerly inline in `main.py`).
- `gateway/app/backend/planner.py` — `plan_tc(...)` / `composition_count(...)` for TC02-TC06 expansion into individual renders.
- `gateway/app/backend/users/`, `gateway/app/backend/workers/` — file-backed registries (operators edit `data/gateway/workers.json`).

When asked to "add an endpoint to the gateway", reach for `routers/<topic>.py` + a service in `services/` first; only touch `main.py` for app wiring or new lifespan hooks.

### Worker layout

The worker is still a single `worker/app/backend/main.py` (~39KB FastAPI app) on top of the vendored `core/`. New pipeline code goes in `core/pipelines/` (see `worker/app/backend/core/pipelines/tc0N_*.py` and `_common.py`); new settings factories go in `core/contract.py`.

### Request flow

1. Client uploads files to gateway (`POST /api/v1/uploads/{role}` or V3-compatible `POST /api/jobs/upload`). Files land in `GATEWAY_DATA_DIR/uploads/` with a `file_id`.
2. Client posts a job (`POST /api/v1/jobs` or `POST /api/{tc}/render`). Gateway:
   - Inserts a row in `v3_jobs` with status `queued`.
   - Picks the best healthy worker (lowest `active` count, under its `max_concurrent`) — see `_pick_worker`.
   - Forwards uploaded bytes to the worker at `POST /v1/jobs/{job_id}/upload/{role}` with the `X-Cutdee-Internal` header.
   - Posts the render payload to either `POST /v1/jobs/{job_id}/render` (legacy TC01) or `POST /v1/{tc}/render/{job_id}` (TC01-TC06 pipeline form).
   - Updates the PG row with the worker's `status` / `output_file` / `output_size` / `started_at` / `finished_at`.
3. Client polls `GET /api/v1/jobs/{id}` (or the V3-compatible `GET /api/jobs/{id}` which returns `{progress, current_step, files, logs, result, ...}` via `_v3_job_dict`).
4. Client downloads the MP4 via `GET /api/v1/jobs/{id}/download/{file}` — gateway proxies from the worker and caches under `OUTPUTS_DIR`.

### Auth

Two distinct headers:

- **`Authorization: Bearer cutdee_vdo_<43 chars>`** for public endpoints. For v1.0 the prefix check is the only validation; the first 12 chars after the prefix become the user id (`u_<...>`).
- **`X-Cutdee-Internal: <token>`** for gateway↔worker RPC and cluster admin endpoints (`/api/cluster/workers`, `/v1/jobs/...`). Token comes from `CUTDEE_INTERNAL_TOKEN` and **must match** between gateway and every worker.

Public liveness (`/healthz`, `/health`) is unauthenticated.

### API surfaces

The gateway exposes three overlapping endpoint families:

| Family | Example | Audience |
|---|---|---|
| Legacy v1 | `/api/v1/uploads/{role}`, `/api/v1/jobs`, `/api/v1/jobs/{id}` | Original curl examples in README |
| V3 WebApp-compatible | `/api/render/{tc}`, `/api/job/{id}`, `/api/jobs/history`, `/api/job/{id}/thumbnails` | Tk UI frontend (when proxied through the V3 frontend) |
| Cluster admin | `/api/cluster/health`, `/api/cluster/workers[/...]` | Operators (internal-token gated) |

Worker endpoints (`/v1/jobs/.../upload/{role}`, `/v1/{tc}/render/{job_id}`, `/v1/jobs/{id}/status`, `/v1/jobs/{id}/output`) are internal-only.

### Worker: pipeline layer

`worker/app/backend/core/pipelines/` is the actual render layer. Each `tc0N_*.py` exposes a single `render(inputs, callbacks) -> PipelineResult` function. They share infrastructure via `pipelines/_common.py`:

- **`PipelineInputs`** — `output_dir`, `values` dict, and lists of file paths for `products` / `backgrounds` / `audios` / `covers` (TC05 adds `sources`, TC06 adds `product_roots`). `run_stamp` is threaded through TC06 so the inner chroma intermediate and the audio-master final share a timestamp prefix on disk.
- **`PipelineCallbacks`** — `log_fn`, `stop_check`, `progress_fn`, `file_fn`, `pause_check`, `step_fn`. Always wrap log/progress callbacks through `safe_log` / `safe_progress` / `safe_file` because the worker passes no-op `lambda pct, msg: None` for `progress_fn` and `file_fn=None`.
- **`PipelineResult`** / **`StageResult`** — fail-closed result types with `_finalize_common`. A result is `is_success` only when `_finalized=True`, `status == SUCCEEDED`, and `invariant_errors` is empty. Counts (`expected`, `succeeded`, `failed`, `cancelled`, `validated_resumed`) must reconcile via `accounted_count == expected` before SUCCEEDED is allowed.
- `combined_stop_check(stop, pause)` collapses "user pressed Stop" and "user pressed Pause" into a single graceful-exit predicate.
- `normalize_run_seed` / `resolve_run_seed` enforce the seed contract (0/blank → auto, non-integer or out-of-range → `ValueError`).

Pipelines never import tkinter — they are pure Python and are unit-testable as such.

### Worker: ffmpeg layer

`green_render.render_green(...)` is what TC01 calls directly; TC02 calls it after a reframe stage. `GreenSettings` is the dataclass the gateway forwards via `RenderRequest.settings` (or `TC01_VIDEO_DEFAULTS` from `core/contract.py`). Settings are mapped via `core/contract.py` factories — use those, do not re-inline the constructor in a new pipeline (the comment at the top of `contract.py` explains why).

Encoder selection lives in `core/gpu_detector.py`. Order: macOS VideoToolbox → `h264_nvenc` → `av1_nvenc` → `hevc_nvenc` → `h264_qsv` → `h264_amf` → `libx264`. Each candidate is smoke-tested (1-frame encode) before promotion, and the result is LRU-cached. `core/encoder_recovery.py` allows one CPU retry on a real hardware-encoder failure (`should_retry_with_cpu`).

`ffmpeg_runner.FfmpegRunner` runs the subprocess, parses `-progress` key=value lines, and enforces two watchdog limits: wall-clock `> max_factor × video_duration` (default 3.0; TC04 uses 10.0 via `DEFAULT_TC_FACTOR_OVERRIDES`) and `idle_timeout` (default 120s with no output). On darwin / Windows, `NO_WINDOW_FLAGS` is set so subprocesses don't pop console windows.

`core/ai_reframe.py` provides the lens presets used by TC02/TC05 (the hard-coded list returned by `GET /api/lens` on the gateway mirrors `LENS_PRESETS`).

### Persistence

- **Postgres** (`v3_jobs` table, auto-created) — the single source of truth for job state. Gateway is stateless; any number can be run in parallel.
- **Worker job dirs** (`/var/lib/v3-cursor-api/worker/jobs/{job_id}/`) — hold uploaded inputs and rendered outputs, plus `.render_checkpoint.json` (schema v2 in `render_checkpoint.py`) for resume.
- **Gateway uploads cache** (`GATEWAY_DATA_DIR/uploads/`) — kept until a `POST /api/cluster/workers/reload` style GC is added; not auto-cleaned.
- **`workers.json`** (`GATEWAY_DATA_DIR/workers.json`) — operator-edited, reloaded on demand.

## Conventions / things to know

- **Do not edit `worker/app/backend/core/`** as if it were this repo's source. It is a vendored copy of `V3_cursor/core/`. New pipeline code goes in `worker/app/backend/main.py` or a new sibling module, and settings factories go in `core/contract.py`.
- **`_legacy/`** under `core/` is the V2 code kept for reference; the active UI/worker never imports from it. Don't add new callers.
- **Pipeline result truth contract** — every TC pipeline's `PipelineResult` must be finalized (`.finalize(...)`) and pass `_finalize_common`'s invariant checks before it can become `SUCCEEDED`. If you write a new pipeline, copy the shape of `tc01_chroma.py` and call `finalize_pipeline_result(result, ...)` at the end.
- **Fail-closed media checks** — `media_probe.MediaStreamState.ERROR` is reserved for probe failures (timeout, missing file, decode failure). `ABSENT` means a successful probe that confirms no stream. Never collapse the two; downstream chroma/audio code depends on the distinction.
- **Worker concurrency** is governed by `WORKER_MAX_CONCURRENT` (defaults to 2) per the capabilities endpoint, plus per-pipeline parallel knobs: `V3_TC02_PARALLEL` (chroma stage parallelism, default 1) and `V3_TC0N_PARALLEL` overrides.
- **Auth header** for gateway↔worker RPC is exactly `X-Cutdee-Internal`, value must match `CUTDEE_INTERNAL_TOKEN` env on both sides. The header is checked by `_verify_internal` in both `main.py` files.
- **DB column types** are explicit in the `_init_schema` migration block; columns like `settings`, `output_files`, `log`, `result` are JSONB and are stored as `json.dumps(...)`. Reads in `_v3_job_dict` defensively re-parse strings in case a row was written before JSONB conversion.
- **Gateway upload filenames** are minted as `{role}_{int(time.time())}_{secrets.token_hex(8)}.mp4` (the `.mp4` suffix was added in `90ece6b` so the worker's `file_type` checks pass). The worker saves them with whatever filename the gateway puts in `Content-Disposition: attachment; filename=...`; the fix in `f3ef05e` is that workers strip those files out of the `output_files` list before returning.
- **Job ids** are `v3_{int(time.time())}_{secrets.token_hex(6)}` on the gateway and `v3_{int(time.time()*1000)}_{secrets.token_hex(4)}` on the V3-render path — do not assume one format.

### Adding a worker

Operators edit `GATEWAY_DATA_DIR/workers.json` (or wherever `CUTDEE_WORKERS_FILE` points) and reload — there is no auto-discovery:

```json
{
  "workers": [
    {"id": "i9-64gb-cpu-01", "url": "http://127.0.0.1:8789", "tier": "high", "max_concurrent": 4},
    {"id": "5060ti-01",      "url": "http://110.164.146.205:8789", "tier": "mid", "max_concurrent": 2},
    {"id": "hub-rtx3050",    "url": "http://157.85.107.40:8789",   "tier": "low", "max_concurrent": 1}
  ]
}
```

Then `POST /api/cluster/workers/reload` with the `X-Cutdee-Internal` header. `_pick_worker` will then consider the new entry; `max_concurrent` is per-worker (independent of `WORKER_MAX_CONCURRENT` on the worker side, which is the process-wide ceiling it advertises in `/v1/capabilities`).

## Common pitfalls

- `CUTDEE_INTERNAL_TOKEN` defaults to `dev-internal-token-change-me` in **both** processes; if you forget to set it on either side, internal RPC silently 401s.
- `Output_files` filtering — workers prefix uploaded files with `background_`, `product_`, `source_`, `cover_`, `audio_`. The output discovery loop in `_run_tc_pipeline` excludes them unless they contain `__lens*__tc*__` (TC02/TC05 reframe markers), which is how it separates inputs from outputs.
- The `api_render_tc` handler at `/api/render/{tc}` and the dynamic `/api/{tc}/render` endpoints both upload files to the worker under the role name. Don't confuse the gateway's filename minting with the worker's — the gateway always uses `.mp4`; the worker uses whatever the gateway sends in `Content-Disposition`.

## Docs

- `README.md` — public-facing quick start, prod setup, nginx vhost, E2E curl, env vars.
- `docs/README_TH.md` — index of the Thai role-based guides (user / pipeline / ops / dev / deep-dive).
- `docs/reports/` — dated deep-dive reports (`<topic>_<YYYYMMDD_HHMMSS>/`); each folder is a self-contained investigation, not a permanent doc.
