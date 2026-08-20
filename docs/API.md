# API & Authentication Reference

> **Audience:** Frontend devs, API users, end users
> **Base URL:** `https://green.cutdee.com`
> **Last updated:** 2026-08-20

This document covers all public + admin API endpoints of the V3 Gateway.

---

## 1. Quick Reference

### Endpoints by Audience

| Audience | Base URL | Auth |
|---|---|---|
| **Public users** | `/api/app/*` | Cookie (signup/login) or Bearer (API) |
| **Admin** | `/v3api/admin/*`, `/api/cluster/*` | `X-Cutdee-Internal` header |
| **Public stats** | `/v3api/api/cluster/public`, `/v3api/status` | None |
| **Workers (RPC)** | `/v1/jobs/{id}/upload/{role}`, `/v1/jobs/{id}/render` | `X-Cutdee-Internal` header |

### Curl Examples

```bash
# Public (no auth)
curl https://green.cutdee.com/v3api/api/cluster/public

# Cookie (browser)
curl https://green.cutdee.com/api/app/jobs -b "cutdee_session=$TOKEN"

# Bearer (API)
curl https://green.cutdee.com/api/v1/users/me/jobs -H "Authorization: Bearer cutdee_vdo_xxx"

# Internal (admin)
curl https://green.cutdee.com/v3api/api/cluster/dashboard -H "X-Cutdee-Internal: v3-api-internal-token-2026"
```

---

## 2. Authentication

### 2.1 Sign Up (Public)

```http
POST /api/v1/auth/signup
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "secretpass123",
  "display_name": "User Name"  // optional
}

→ 200 OK
{
  "ok": true,
  "user_id": "u_xxx",
  "email": "user@example.com",
  "api_key": "cutdee_vdo_xxx",  // SAVED ONCE, never shown again
  "session_set": true,
  "quota_per_month": 100
}
```

**Side effect:** Sets `cutdee_session` cookie (30d, HttpOnly)

**Errors:** 400 (invalid email/short password), 409 (email exists)

### 2.2 Login (Public)

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "secretpass123"
}

→ 200 OK
{
  "ok": true,
  "user_id": "u_xxx",
  "email": "user@example.com",
  "api_key": "cutdee_vdo_xxx",  // NEW key (old one invalidated)
  "role": "user",
  "quota_per_month": 100,
  "quota_used": 5,
  "display_name": "User Name"
}
```

**Side effects:**
- Old API key invalidated
- New API key issued
- Sets `cutdee_session` cookie

### 2.3 Logout

```http
POST /api/v1/auth/logout
Cookie: cutdee_session=...

→ 200 OK {"ok": true}
```

**Side effect:** Revokes session token in `_SESSION_KEYS` cache

### 2.4 Get Current User (Me)

```http
GET /api/v1/auth/me
Cookie: cutdee_session=...
  OR
Authorization: Bearer cutdee_vdo_xxx

→ 200 OK
{
  "ok": true,
  "user": {
    "user_id": "u_xxx",
    "email": "user@example.com",
    "display_name": "User Name",
    "role": "user",
    "tier": "free",       // free | pro | enterprise
    "monthly_quota": 100,
    "monthly_used": 5,
    "monthly_quota_paid": 0,
    "api_key_prefix": "cutdee_vdo...",
    "created_at": 1787200000.0,
    "last_seen_at": 1787300000.0,
    "last_login_at": 1787300000.0
  }
}
```

### 2.5 Update Profile

```http
PATCH /api/v1/auth/me
Content-Type: application/json
Cookie: cutdee_session=...

{
  "display_name": "New Name",  // optional
  "email": "new@email.com"     // optional
}

→ 200 OK (returns updated user info)
```

### 2.6 Change Password

```http
POST /api/v1/auth/change-password
Content-Type: application/json
Cookie: cutdee_session=...

{
  "old_password": "secretpass123",
  "new_password": "newpass456"
}

→ 200 OK {"ok": true, "message": "password changed"}
```

**Errors:** 400 (short password), 401 (wrong old), 404 (user not found)

### 2.7 Bearer Token (API)

```http
GET /api/v1/users/me
Authorization: Bearer cutdee_vdo_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Token format:** `cutdee_vdo_<8 chars user_id>_<24 hex chars>`

**Total length:** ~43 chars

**Storage:** `api_key_hash` (sha256) stored in `v3_users` table

**Never share tokens in public logs**

---

## 3. End-User Portal (HTML Pages)

| Path | Purpose | Auth |
|---|---|---|
| `/api/app` | Landing (signup/login/dashboard) | None (shows login if not) |
| `/api/app/jobs` | My Jobs list (with filters) | Required |
| `/api/app/job/{id}` | Job detail (live progress + worker + download) | Required |
| `/api/app/submit` | Submit new job form (TC picker + file drop + settings) | Required |
| `/api/app/profile` | Profile (edit display_name, email, password) | Required |

All pages are HTML (use `Cookie: cutdee_session=...`)

---

## 4. Public Endpoints (No Auth)

### 4.1 Public Status

```http
GET /v3api/status
→ 200 OK
<Content-Type: text/html>
<!DOCTYPE html>
<html>
...
<!-- Public status page with cluster health, throughput, charts -->
</html>
```

### 4.2 API Cluster Public

```http
GET /v3api/api/cluster/public
→ 200 OK
{
  "ok": true,
  "service": "V3 Cluster",
  "summary": {
    "total_nodes": 4,
    "enabled_nodes": 4,
    "online_nodes": 3,
    "offline_nodes": 1,
    "total_capacity": 6,
    "active_jobs": 0,
    "window_hours": 24
  },
  "nodes": [
    {
      "name": "Node-1",           // anonymized
      "tier": "Compute+GPU",      // free | Standard | Performance | Compute+GPU
      "tier_tone": "high",
      "active_jobs": 0,
      "max_concurrent": 2,
      "encoder_kind": "CPU",       // GPU | CPU
      "last_seen_ago": 12
    },
    ...
  ],
  "metrics": {
    "window_hours": 24,
    "totals": {
      "total": 15,
      "ok": 9,
      "failed": 0,
      "invalid": 6,
      "success_rate": 60.0
    },
    "by_tc": [...],
    "by_node": [...]
  }
}
```

**Note:** No IP addresses, hostnames, or internal URLs exposed.

### 4.3 OpenAPI / Swagger

```http
GET /docs        # Swagger UI
GET /redoc       # ReDoc UI
GET /openapi.json # OpenAPI 3.0 schema
```

---

## 5. Users Endpoints

### 5.1 Get Current User (Lighter)

```http
GET /api/v1/users/me
Authorization: Bearer ...  OR  Cookie: cutdee_session=...

→ 200 OK
{
  "ok": true,
  "user": {
    "user_id": "u_xxx",
    "display_name": "...",
    "tier": "free",
    "monthly_quota": 100,
    "monthly_used": 5
  }
}
```

### 5.2 Get My Jobs

```http
GET /api/v1/users/me/jobs?limit=50
Authorization: Bearer ...

→ 200 OK
{
  "ok": true,
  "jobs": [
    {
      "job_id": "v3_...",
      "tc": "tc02",
      "status": "succeeded",
      "progress": 100,
      "worker_id": "i9-64gb-cpu-01",
      "created_at": 1787200000.0,
      "output_size": 12345678,
      "output_files": ["output_xxx.mp4"]
    }
  ]
}
```

### 5.3 Get My Stats

```http
GET /api/v1/users/me/stats
→ 200 OK
{
  "ok": true,
  "user": {...},
  "active_jobs": 0
}
```

### 5.4 Lightweight Dashboard (FIX 2026-08-18)

```http
GET /api/v1/dashboard?limit=20
Authorization: Bearer ...  OR  Cookie: cutdee_session=...

→ 200 OK
{
  "user": {...},
  "recent_jobs": [...]
}
```

---

## 6. Jobs Endpoints

### 6.1 Create + Dispatch Job (V3 JSON Payload)

```http
POST /api/v1/jobs
Content-Type: application/json
Authorization: Bearer ...  OR  Cookie: cutdee_session=...

{
  "mode": "tc02",
  "files": {
    "product": ["product_xxx.mp4"],
    "background": ["background_xxx.mp4"]
  },
  "settings": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "key_color": "#00FF00",
    "similarity": 0.29,
    "blend": 0.04,
    "despill": 0.32,
    "encoder": "h264_nvenc",
    "bitrate": "6000k"
  },
  "priority": 0,           // optional (auto-derived from user.tier)
  "max_retries": 0         // optional
}

→ 200 OK
{
  "ok": true,
  "job_id": "v3_...",
  "worker": "i9-64gb-cpu-01",
  "tier": "free",
  "priority": 0,
  "submitted_at": 1787200000.0
}
```

### 6.2 Get Job

```http
GET /api/v1/jobs/{job_id}
Authorization: Bearer ...

→ 200 OK
{
  "job_id": "v3_...",
  "user_id": "u_xxx",
  "tc": "tc02",
  "status": "running",
  "progress": 45,
  ...
}
```

### 6.3 Live Status (with worker info)

```http
GET /api/v1/jobs/{job_id}/live
Authorization: Bearer ...

→ 200 OK
{
  "job_id": "v3_...",
  "status": "running",
  "progress": 45,
  "worker": {
    "node": "Node-1",      // anonymized
    "tier": "high",
    "max_concurrent": 2
  },
  "worker_load": {
    "active_jobs": 1,
    "max_concurrent": 2,
    "encoder": "h264_nvenc"
  },
  "eta_seconds": 30,        // estimated
  "avg_seconds": 45,       // historical avg for tc
  "output_files": []
}
```

### 6.4 Cancel Job

```http
POST /api/v1/jobs/{job_id}/cancel
Authorization: Bearer ...

→ 200 OK
{"ok": true, "job_id": "...", "cancelled": true}
```

**Errors:** 404 (not found / not owned)

### 6.5 Retry (instructions)

```http
POST /api/v1/jobs/{job_id}/retry
Authorization: Bearer ...

→ 200 OK
{
  "ok": true,
  "original_job_id": "v3_...",
  "new_job_id": null,
  "tc": "tc02",
  "message": "Retry requires re-upload via /api/tc*/render"
}
```

### 6.6 Soft-Delete

```http
DELETE /api/v1/jobs/{job_id}
Authorization: Bearer ...

→ 200 OK {"ok": true, "job_id": "...", "deleted": true}
```

### 6.7 Download Output File

```http
GET /api/v1/jobs/{job_id}/download/{filename}
Authorization: Bearer ...

→ 200 OK
<Content-Type: video/mp4>
<binary MP4 data>
```

---

## 7. Uploads Endpoints

### 7.1 Upload File (multipart)

```http
POST /api/v1/uploads/{role}
Content-Type: multipart/form-data
Authorization: Bearer ...  OR  Cookie: cutdee_session=...

{role in: product, background, cover, audio, source, product_root}

Body:
  file: <binary>

→ 200 OK
{
  "ok": true,
  "file_id": "product_xxx.mp4",
  "role": "product",
  "size": 12345678,
  "suffix": ".mp4"
}
```

**Max size:** 200MB (`MAX_UPLOAD_BYTES`)

---

## 8. Cluster Endpoints (Admin)

### 8.1 Full Dashboard

```http
GET /v3api/api/cluster/dashboard
X-Cutdee-Internal: <token>

→ 200 OK
{
  "ok": true,
  "summary": {
    "total_workers": 4,
    "enabled_workers": 4,
    "healthy_workers": 3,
    "down_workers": 1,
    "disabled_workers": 0,
    "total_capacity": 6,
    "active_jobs": 0,
    "live_jobs_in_db": 0
  },
  "cluster": [
    {
      "id": "i9-64gb-cpu-01",
      "url": "http://127.0.0.1:8789",
      "enabled": true,
      "healthy": true,
      "tier": "high",
      "max_concurrent": 2,
      "active_jobs": 0,
      "in_flight_jobs": [...],
      "encoder": "libx264",
      "version": "1.2.0",
      "commit": "b9fc8b8",
      "data_dir": "/var/lib/v3-cursor-api/worker",
      ...
    }
  ],
  "live_jobs": [...],
  "metrics": {
    "window_hours": 24,
    "totals": {...},
    "by_tc": [...],
    "by_worker": [...]
  }
}
```

### 8.2 Live Jobs

```http
GET /api/cluster/jobs/live?limit=50
X-Cutdee-Internal: <token>

→ 200 OK
{
  "ok": true,
  "jobs": [
    {
      "job_id": "v3_...",
      "user_id": "u_xxx",
      "tc": "tc02",
      "status": "running",
      "progress": 45.0,
      "worker_id": "i9-64gb-cpu-01",
      "elapsed_sec": 12.3,
      "created_at": 1787200000.0
    }
  ]
}
```

### 8.3 Metrics

```http
GET /api/cluster/metrics?hours=24
X-Cutdee-Internal: <token>

→ 200 OK
{
  "window_hours": 24,
  "totals": {
    "total": 15,
    "ok": 9,
    "failed": 0,
    "invalid": 6,
    "success_rate": 60.0
  },
  "by_tc": [
    {
      "tc": "tc01",
      "total": 9,
      "ok": 8,
      "fail": 0,
      "invalid": 0,
      "avg_sec": 4,
      "p50_sec": 4,
      "p95_sec": 7,
      "avg_bytes": 1302791,
      "success_rate": 88.9
    },
    {
      "tc": "tc02",
      "total": 6,
      "ok": 0,
      "fail": 0,
      "invalid": 6,
      "avg_sec": 0,
      "p50_sec": 0,
      "p95_sec": 0,
      "avg_bytes": 0,
      "success_rate": 0.0
    }
  ],
  "by_worker": [...],
  "hourly_throughput": [
    {"hour": 1787150400, "total": 15, "ok": 9},
    ...
  ]
}
```

### 8.4 Worker Management

```http
GET    /api/cluster/workers/reload          # reload from workers.json
POST   /api/cluster/workers                  # add
PATCH  /api/cluster/workers/{id}             # update
DELETE /api/cluster/workers/{id}             # remove
POST   /api/cluster/workers/{id}/test        # test connection
```

---

## 9. System Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/healthz` | GET | Simple alive check |
| `/api/health` | GET | Service status + version + commit |
| `/api/version` | GET | API version only |
| `/api/ffmpeg` | GET | FFmpeg info |
| `/api/encoders` | GET | Supported encoders list |
| `/api/lens` | GET | 7 fixed lens presets |
| `/api/config` | GET | Current settings contract |
| `/api/outputs` | GET | Output file list (paginated) |
| `/api/download/{file_path:path}` | GET | Output file proxy (path-escape protected) |

---

## 10. WebSocket API

### 10.1 Real-Time Job Updates

```
ws[s]://green.cutdee.com/v3api/ws/jobs/{job_id}
```

**Auth:** Cookie `cutdee_session=...` (auto-included by browser) OR
`Sec-WebSocket-Protocol: bearer.<api_key>`

### Message Protocol

#### Server → Client

```json
// Initial state on connect
{
  "type": "hello",
  "job_id": "v3_...",
  "status": "running",
  "last_state": {...},
  "server_time": 1787207411.5
}

// During job
{
  "type": "progress",
  "status": "running",
  "progress": 45,
  "current_step": "encoding",
  "output_size": 1234567,
  "duration_sec": 12.3
}

// On completion
{
  "type": "done",
  "status": "succeeded",
  "progress": 100,
  "output_files": ["output_xxx.mp4"],
  "output_size": 12345678,
  "duration_sec": 45.6
}
```

#### Client → Server

- `ping` → server replies `pong` (keepalive)

### JavaScript Example

```javascript
const ws = new WebSocket(`wss://green.cutdee.com/v3api/wobs/${jobId}`);
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === 'progress') {
    updateProgressBar(msg.progress);
  } else if (msg.type === 'done') {
    showCompletion(msg);
  }
};
// Keepalive: send "ping" every 30s, server replies "pong"
```

---

## 11. Common Use Cases

### Use Case 1: Upload + Submit + Monitor + Download

```javascript
async function submitJob() {
  // 1. Login (if not already)
  await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password })
  });
  // → cutdee_session cookie set

  // 2. Upload product
  const productForm = new FormData();
  productForm.append('file', productFile);
  const productRes = await fetch('/api/v1/uploads/product', {
    method: 'POST',
    body: productForm
  });
  const { file_id: productId } = await productRes.json();

  // 3. Upload background
  const bgForm = new FormData();
  bgForm.append('file', bgFile);
  const bgRes = await fetch('/api/v1/uploads/background', {
    method: 'POST',
    body: bgForm
  });
  const { file_id: bgId } = await bgRes.json();

  // 4. Submit job
  const jobRes = await fetch('/api/v1/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'tc02',
      files: { product: [productId], background: [bgId] },
      settings: {
        width: 1080, height: 1920, fps: 30,
        key_color: '#00FF00', similarity: 0.29,
        blend: 0.04, despill: 0.32,
        encoder: 'h264_nvenc', bitrate: '6000k'
      }
    })
  });
  const { job_id } = await jobRes.json();

  // 5. Monitor via WebSocket
  const ws = new WebSocket(`wss://green.cutdee.com/v3api/ws/jobs/${job_id}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'progress') {
      updateProgress(msg.progress);
    } else if (msg.type === 'done') {
      showDownloadLink(msg);
    }
  };

  // 6. Download output (when done)
  const downloadRes = await fetch(
    `/api/v1/jobs/${job_id}/download/${msg.output_files[0]}`
  );
  const blob = await downloadRes.blob();
  saveAs(blob, msg.output_files[0]);
}
```

### Use Case 2: Admin Monitoring Dashboard

```javascript
async function monitorCluster() {
  // Internal auth required
  const res = await fetch('https://green.cutdee.com/v3api/api/cluster/dashboard', {
    headers: { 'X-Cutdee-Internal': 'v3-api-internal-token-2026' }
  });
  const data = await res.json();

  // summary
  console.log(`Workers: ${data.summary.healthy_workers}/${data.summary.enabled_workers} online`);
  console.log(`Capacity: ${data.summary.total_capacity} slots`);
  console.log(`Active jobs: ${data.summary.active_jobs}`);

  // metrics
  console.log(`Success rate: ${data.metrics.totals.success_rate}%`);
  console.log(`Total jobs: ${data.metrics.totals.total}`);
  console.log(`Failed: ${data.metrics.totals.failed}`);
}
```

### Use Case 3: Public Status (No Auth)

```bash
curl https://green.cutdee.com/v3api/api/cluster/public | jq
```

```json
{
  "ok": true,
  "summary": {
    "total_nodes": 4,
    "online_nodes": 3,
    "active_jobs": 0
  },
  "nodes": [
    {"name": "Node-1", "tier": "Compute+GPU", "encoder_kind": "CPU"},
    {"name": "Node-2", "tier": "Performance", "encoder_kind": "CPU"},
    {"name": "Node-3", "tier": "Performance", "encoder_kind": "CPU"},
    {"name": "Node-4", "tier": "Performance", "encoder_kind": "CPU"}
  ]
}
```

---

## 12. Error Codes

| Status | Meaning | When |
|---:|---|---|
| 200 | OK | Success |
| 201 | Created | Resource created (POST /api/v1/users) |
| 202 | Accepted | Job accepted (POST /api/render/{tc}) |
| 400 | Bad Request | Validation error, missing field |
| 401 | Unauthorized | Missing/bad auth token, wrong password |
| 403 | Forbidden | Admin only, user-only route |
| 404 | Not Found | job_id / worker_id / file not found |
| 405 | Method Not Allowed | Wrong HTTP method |
| 409 | Conflict | Email already exists |
| 413 | Payload Too Large | File > 200MB |
| 429 | Too Many Requests | (future) Rate limit |
| 500 | Internal Server Error | Bug — check logs |
| 503 | Service Unavailable | No workers available |

### Error Body Shape

```json
{
  "detail": "human-readable error message",
  "error": "machine-readable code (sometimes)"
}
```

---

## 13. Rate Limits

**Currently:** None (FIX 2026-08-19 — `monthly_quota=100` is per-user, not per-second)

**Future plan:**
- Free tier: 100 jobs/month
- Pro tier: 1000 jobs/month
- Enterprise: unlimited

---

## 14. Data Model Reference

### `v3_users` (15 columns)

```
user_id              PK
api_key_hash         sha256 of api_key
role                 user | admin
display_name
monthly_quota        INT  (default 100)
monthly_used         INT  (default 0)
monthly_quota_paid   INT  (default 0, Phase 4)
api_key_prefix       first 11 chars
created_at
last_seen_at
last_reset_at
email                (Phase 4, unique lower(email))
password_hash        (Phase 4, PBKDF2)
last_login_at        (Phase 4)
tier                 free | pro | enterprise (default free)
```

### `v3_jobs` (28 columns)

```
job_id              PK
user_id
worker_id
tc
status              queued | running | succeeded | failed | cancelled | invalid_input
progress            INT  0-100
current_step
reserved_credits    INT
settled_credits      INT
product_path
background_path
cover_path
audio_path
settings            JSONB
output_file
output_size         BIGINT
output_files        JSONB (array)
log                 JSONB
result              JSONB
error
created_at
started_at
finished_at
priority            INT (default 0, Phase 4)
```

### Job Lifecycle

```
created_at → status=queued
            ↓ (assigned worker via /v3api/api/v1/jobs)
started_at → status=running
            ↓ (worker streams progress)
            ↓ (publishes via /api/v1/internal/jobs/{id}/publish)
finished_at → status=succeeded | failed | cancelled | invalid_input
```

### Status Transitions

- `queued` → `running` (worker starts)
- `running` → `succeeded` (worker done, output exists)
- `running` → `failed` (worker error)
- `running` → `cancelled` (user cancel)
- `*` → `deleted` (user soft-delete)

---

## 15. Best Practices

### For API Users

1. **Save API key once** — never shown again
2. **Use cookies for browsers** — automatic via `Set-Cookie`
3. **Use Bearer for CLI/scripting** — easier to copy
4. **Poll `/api/v1/jobs/{id}/live`** every 5-10s OR use WebSocket
5. **Validate email** before sending requests (400 if invalid)
6. **Use HTTPS** — never HTTP (port 22 is not used for API)

### For Admins

1. **Use `X-Cutdee-Internal`** header for all admin endpoints
2. **Never log tokens** — use `X-Cutdee-Internal: ****` in logs
3. **Rotate tokens** when:
   - Worker is decommissioned
   - User leaves the team
4. **Monitor `/api/cluster/health`** for worker status
5. **Use `/api/cluster/metrics`** for SLA monitoring

### For WebSocket Clients

1. **Use cookies** for browser (no manual auth)
2. **Reconnect** on close (use backoff: 1s, 2s, 4s, 8s, max 30s)
3. **Send `ping`** every 25-30s to keep alive
4. **Cache last_state** from hello message (re-render on reconnect)

---

## 16. Versioning

| Version | Commit | Date | Features |
|---|---|---|---|
| `1.0.0` | `f6299fa` | 2026-08-18 | Initial: TC02 WebSocket added |
| `1.1.0` | `b7e4e6e` | 2026-08-20 | Refactor Phase 1-2: services + templates |
| `1.2.0` | `b9fc8b8` | 2026-08-20 | Refactor Phase 3-4: routers + app/ + auth + member portal + tier + public status + WebSocket |

API is stable — all versions return the same response shapes.

---
