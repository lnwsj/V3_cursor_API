# Deployment & Infrastructure Guide

> **Audience:** DevOps / SRE / User (sj88)
> **Last updated:** 2026-08-20

This document covers the **operational side** of the V3 Gateway:
production deployment, infrastructure topology, security, monitoring,
and operational procedures.

---

## 1. Infrastructure Topology

```
                          Public Internet
                                │
                                ▼
                  ┌──────────────────────────┐
                  │  Cloudflare CDN (HTTPS)   │
                  │  green.cutdee.com         │
                  └────────────┬─────────────┘
                                │
                                ▼
                  ┌──────────────────────────┐
                  │  nginx (systemd)           │
                  │  103.253.75.161:443        │
                  │  Tailscale IP              │
                  │  serves /var/www/         │
                  │  (V3 WebApp frontend)      │
                  └────────────┬─────────────┘
                                │ FastAPI (uvicorn) via uvicorn
                                │ on 127.0.0.1:8788
                                ▼
                  ┌──────────────────────────┐
                  │  V3 Gateway                │
                  │  (refactor-base: b9fc8b8)  │
                  │  21 modules · 31-line main│
                  └────────────┬─────────────┘
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
       ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
       │   i9-64gb     │ │   m4-mlx      │ │ sj88-rtx2050-01│
       │   (Linux)     │ │   (Mac mini)  │ │   (Linux)     │
       │   f6299fa     │ │   f6299fa     │ │   f6299fa     │
       │   nvenc=off   │ │   vtenc=on    │ │   nvenc=on    │
       │   port 8789   │ │   port 55520  │ │   port 55522  │
       │   2 slots     │ │   2 slots     │ │   1 slot      │
       └───────────────┘ └───────────────┘ └───────────────┘
                ▲               ▲               ▲
                │               │               │
                └──── Tailscale tunnels ──────┘
                    (via sj88-voice-hub 100.102.13.0)
```

---

## 2. Servers

| Server | Tailscale IP | Public IP | OS | CPU | RAM | Access |
|---|---|---|---|---|---|---|
| **prod gateway** | 103.253.75.161 | same | Linux 24.04 | 32 cores | 62GB | `Dse54fg8*@@2026` (root) |
| hub (sj88-voice-hub) | 100.102.13.0 | 103.253.73.29 | Linux 26.04 | 8 cores | 8GB | key (sj99) |
| license (sj88-voice-primary) | 100.69.123.5 | 103.22.183.111 | Linux 24.04 | — | — | password `2$3Z7Gf1#9hv` (root) |
| local Mac | 100.126.135.95 | — | macOS 26.3 | Apple M4 | 16GB | key (sj88) |
| sjnb3050ti (LAN) | 192.168.1.41 | — | Linux Mint | 4 cores | 8GB | `SJja0238@@2026` (sj55) |

---

## 3. Services (systemd)

| Service | Port | Status | Logs | Restart |
|---|---|---|---|---|
| `v3-cursor-api-gateway.service` | 127.0.0.1:8788 (internal) | active | `/var/log/v3-cursor-api/gateway.log` | `Restart=always` |
| `v3-cursor-api-worker.service` | (varies per worker) | active | `/var/log/v3-cursor-api/worker.log` | `Restart=always` |
| `nginx.service` | 443, 80 | active | `/var/log/nginx/` | `Restart=always` |
| `postgresql.service` | 5432 (direct) | active | — | — |
| `pgbouncer.service` | 6432 (pool) | active | — | — |

### systemd unit files

| File | Path |
|---|---|
| Gateway unit | `/etc/systemd/system/v3-cursor-api-gateway.service` |
| Worker unit | `/etc/systemd/system/v3-cursor-api-worker.service` |
| Drop-ins | `/etc/systemd/system/v3-cursor-api-gateway.service.d/90-loopback-remediation.conf` |

### Gateway unit (current)

```ini
[Unit]
Description=V3_cursor_API Gateway (Green Screen Cluster)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=v3api
Group=v3api
WorkingDirectory=/opt/v3-cursor-api/gateway
EnvironmentFile=/etc/v3-cursor-api/gateway.env
ExecStart=/opt/v3-cursor-api/gateway/.venv/bin/python3 -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8788 --ws-origin=* --ws=wsproto
Restart=always
RestartSec=5
MemoryHigh=2G
MemoryMax=4G
StandardOutput=append:/var/log/v3-cursor-api/gateway.log
StandardError=append:/var/log/v3-cursor-api/gateway.log

[Install]
```

### Drop-in (90-loopback-remediation.conf)

```ini
[Service]
ExecStart=
ExecStart=/opt/v3-cursor-api/gateway/.venv/bin/python3 -m uvicorn app.backend.main:app --host 127.0.0.1 --port 8788
```

---

## 4. Environment (prod)

### `/etc/v3-cursor-api/gateway.env`

```bash
CUTDEE_INTERNAL_TOKEN=v3-api-internal-token-2026
GATEWAY_PORT=8788
GATEWAY_DATA_DIR=/var/lib/v3-cursor-api/gateway
CUTDEE_PG_HOST=127.0.0.1
CUTDEE_PG_PORT=6432
CUTDEE_PG_NAME=v3_cursor_api
CUTDEE_PG_USER=v3_cursor_api            # FIX 2026-08-20 (was postgres)
CUTDEE_PG_PASSWORD=tts_saas_pwd_2026
CUTDEE_API_KEYS=cutdee_vdo_fba2f9962613c9f9ccece75b542e1c34d406ef4ebae
```

### `/etc/v3-cursor-api/worker.env` (template)

```bash
WORKER_ID=sjnb3050ti-rtx3050
WORKER_PORT=8789
WORKER_DATA_DIR=/var/lib/v3-cursor-api/worker
WORKER_LOG_LEVEL=INFO
CUTDEE_INTERNAL_TOKEN=v3-api-internal-token-2026
CUTDEE_FASTAPI_PUBLIC=true
```

---

## 5. nginx

### `/etc/nginx/sites-enabled/green.cutdee.com.conf`

```nginx
# /v3api/cluster-status → V3 dashboard (specific route, before catch-all)
location = /v3api/cluster-status {
    root /var/www;
    try_files /v3-dashboard.html =404;
}

# /api/* → V3 API (cleaner alias for /v3api/*)
location /api/ {
    proxy_pass http://127.0.0.1:8788/api/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 600s;
}

# /v3api/* → V3 API (legacy path, still works)
location /v3api/ {
    proxy_pass http://127.0.0.1:8788/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 600s;
}
```

### Important notes

- `/v3api/*` strips `/v3api` prefix → `/api/*` at gateway
- `/api/*` preserves `/api` prefix → `/api/*` at gateway
- WebSocket upgrade headers required for `/ws/jobs/{id}`

---

## 6. PostgreSQL

### Users

| User | Password | DB | Privileges | Notes |
|---|---|---|---|---|
| `postgres` | (server-managed) | `v3_cursor_api` | superuser | (only direct to 5432) |
| `v3_cursor_api` | `tts_saas_pwd_2026` | `v3_cursor_api` | ALL | (via PgBouncer 6432) |

### Create v3_cursor_api user (if missing)

```sql
-- run as postgres user
CREATE USER v3_cursor_api WITH PASSWORD 'tts_saas_pwd_2026';
GRANT ALL ON DATABASE v3_cursor_api TO v3_cursor_api;
```

### Test connection

```bash
PGPASSWORD="tts_saas_pwd_2026" psql -h 127.0.0.1 -p 6432 -U v3_cursor_api -d v3_cursor_api -c "SELECT 1"
```

---

## 7. Workers

### Registry (`/var/lib/v3-cursor-api/gateway/workers.json`)

```json
{
  "workers": [
    {
      "id": "i9-64gb-cpu-01",
      "name": "sj88-i9-64gb (i9-12900K 24C 62GB, CPU)",
      "url": "http://127.0.0.1:8789",
      "tier": "high",
      "max_concurrent": 2,
      "enabled": true
    },
    {
      "id": "sj88-rtx5060ti-01",
      "name": "sj88-rtx5060ti-01 (RTX 5060 Ti 16GB + AMD 5950X 32C)",
      "url": "http://110.164.146.205:8789",
      "tier": "mid",
      "max_concurrent": 2,
      "enabled": true
    },
    {
      "id": "m4-mlx",
      "name": "m4-mlx (Mac mini, hevc_videotoolbox, via hub:55520)",
      "url": "http://103.253.73.29:55520",
      "tier": "mid",
      "max_concurrent": 2,
      "enabled": true
    },
    {
      "id": "sj88ai-rtx2050-01",
      "name": "sj88ai (RTX 2050 4GB, NVENC, via hub:55522)",
      "url": "http://103.253.73.29:55522",
      "tier": "low",
      "max_concurrent": 1,
      "enabled": true
    },
    {
      "id": "sjnb3050ti-rtx3050",
      "name": "sjnb3050ti (RTX 3050 4GB, Linux Mint, Tailscale tunnel)",
      "url": "http://103.253.73.29:55523",
      "tier": "low",
      "max_concurrent": 1,
      "enabled": false
    },
    {
      "id": "64gb-windows-gtx1060",
      "name": "64gb Windows (X99 28C/64GB, GTX 1060 3GB, Tailscale)",
      "url": "http://100.88.10.14:8789",
      "tier": "low",
      "max_concurrent": 1,
      "enabled": false
    }
  ]
}
```

### Tailscale reverse tunnels

| Worker | Hub port | Tailscale IP | Notes |
|---|---|---|---|
| `m4-mlx` | 55520 | 100.69.123.5 | hub:55520 → 192.168.1.42:8789 (mac-mini M4) |
| `sj88ai-rtx2050-01` | 55522 | 100.69.123.5 | hub:55522 → 192.168.1.41:8789 (sjnb3050ti) |
| `sjnb3050ti-rtx3050` | 55523 | 100.69.123.5 | (port down — DISABLED) |

### Reverse tunnel setup

```bash
# On each remote worker (e.g., M4 Mac):
ssh -N -T -R 55520:127.0.0.1:8789 sj99@100.102.13.0
# Then gateway can reach via http://103.253.73.29:55520
```

---

## 8. Deployment Procedure

### Quick Deploy (Phase 1-4 refactor)

```bash
# 1. SSH to prod
ssh root@103.253.75.161

# 2. Backup current
cd /opt/v3-cursor-api
cp -p gateway/app/backend/main.py{,.bak.$(date +%Y%m%d_%H%M%S)}

# 3. Fetch latest refactor-base
chown -R v3api:v3api .
sudo -u v3api git fetch origin refactor-base
sudo -u v3api git reset --hard origin/refactor-base
sudo -u v3api git clean -fd

# 4. (First time) Create Python venv
sudo -u v3api python3 -m venv gateway/.venv
sudo -u v3api gateway/.venv/bin/pip install -r gateway/requirements.txt

# 5. (First time) Create v3_cursor_api PG user
sudo -u postgres psql -c "CREATE USER v3_cursor_api WITH PASSWORD 'tts_saas_pwd_2026';"
sudo -u postgres psql -c "GRANT ALL ON DATABASE v3_cursor_api TO v3_cursor_api;"

# 6. (First time) Update env
sed -i 's/^CUTDEE_PG_USER=.*/CUTDEE_PG_USER=v3_cursor_api/' /etc/v3-cursor-api/gateway.env

# 7. Restart services
sudo systemctl restart v3-cursor-api-gateway
sudo systemctl restart v3-cursor-api-worker

# 8. Verify
sleep 5
curl -s https://green.cutdee.com/api/health
curl -s https://green.cutdee.com/v3api/api/cluster/public
```

### Full Deploy (with venv rebuild)

If venv is broken or Python version mismatch:

```bash
# On prod
rm -rf /opt/v3-cursor-api/gateway/.venv
cd /opt/v3-cursor-api
chown -R v3api:v3api .
sudo -u v3api python3 -m venv gateway/.venv
sudo -u v3api gateway/.venv/bin/pip install -r gateway/requirements.txt
sudo systemctl restart v3-cursor-api-gateway
```

### Rollback (if needed)

```bash
# Restore from backup
ls -la /opt/v3-cursor-api/gateway/app/backend/main.py.bak.*
# pick the most recent working one
cp -p /opt/v3-cursor-api/gateway/app/backend/main.py.bak.20260820_070344 \
   /opt/v3-cursor-api/gateway/app/backend/main.py
sudo systemctl restart v3-cursor-api-gateway
```

---

## 9. Monitoring

### Quick Health Check

```bash
# 1. Gateway active?
ssh root@103.253.75.161 'systemctl is-active v3-cursor-api-gateway'

# 2. Service responding?
curl -s https://green.cutdee.com/api/health | python3 -m json.tool

# 3. Cluster status?
curl -s https://green.cutdee.com/v3api/api/cluster/public | python3 -m json.tool

# 4. Active jobs?
curl -s https://green.cutdee.com/api/cluster/dashboard \
  -H "X-Cutdee-Internal: v3-api-internal-token-2026" | python3 -m json.tool

# 5. Per-worker health (introspect)
curl -s https://green.cutdee.com/v3api/api/cluster/public \
  | python3 -c "import sys, json; d=json.load(sys.stdin); print(f'  {d[\"summary\"][\"online_nodes\"]}/{d[\"summary\"][\"enabled_nodes\"]} workers online')"
```

### Worker Health Probe

```bash
# Test each worker
for url in "http://127.0.0.1:8789" \
           "http://110.164.146.205:8789" \
           "http://103.253.73.29:55520" \
           "http://103.253.73.29:55522" \
           "http://103.253.73.29:55523"; do
  echo "  $url: $(curl -s -m 5 "$url/health" 2>/dev/null | python3 -c 'import sys, json; d=json.load(sys.stdin); print(d.get("status", "?"))' 2>/dev/null || echo DOWN)"
done
```

### Service Logs

```bash
# Gateway log
ssh root@103.253.75.161 'tail -100 /var/log/v3-cursor-api/gateway.log | head -30'

# Worker log
ssh root@103.253.75.161 'tail -100 /var/log/v3-cursor-api/worker.log | head -30'

# Journald
ssh root@103.253.75.161 'journalctl -u v3-cursor-api-gateway -n 50 --no-pager'
```

---

## 10. Common Operations

### Restart Gateway

```bash
ssh root@103.253.75.161 'sudo systemctl restart v3-cursor-api-gateway'
ssh root@103.253.75.161 'sudo systemctl restart v3-cursor-api-worker'
```

### Check Worker Health (from gateway perspective)

```bash
curl -s https://green.cutdee.com/v3api/api/cluster/public | \
  python3 -c "
import sys, json
d = json.load(sys.stdin)
for n in d['nodes']:
    print(f'  {n[\"name\"]}: {\"ONLINE\" if n[\"healthy\"] else \"OFFLINE\"} {n[\"encoder_kind\"]} cap={n[\"max_concurrent\"]}')
"
```

### Add New Worker

```bash
# 1. SSH to new worker
ssh sj55@192.168.1.42

# 2. Setup worker
cd /opt/v3-cursor-api
git pull
cd worker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo tee /etc/systemd/system/v3-cursor-api-worker.service <<EOF
[Unit]
Description=V3 Worker
After=network-online.target
[Service]
Type=simple
User=v3api
WorkingDirectory=/opt/v3-cursor-api/worker
EnvironmentFile=/etc/v3-cursor-api/worker.env
ExecStart=/opt/v3-cursor-api/worker/.venv/bin/python3 -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8789
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now v3-cursor-api-worker

# 3. Setup reverse tunnel (to hub)
ssh -N -T -R 55524:127.0.0.1:8789 sj99@100.102.13.0 &

# 4. Add to workers.json on prod
ssh root@103.253.75.161
cat >> /var/lib/v3-cursor-api/gateway/workers.json <<EOF
,
  {
    "id": "new-worker",
    "name": "new-worker (description)",
    "url": "http://103.253.73.29:55524",
    "tier": "low",
    "max_concurrent": 1,
    "enabled": true
  }
EOF
```

### Reload workers (hot-reload)

```bash
# Endpoint: /api/cluster/workers/reload
curl -X POST -H "X-Cutdee-Internal: v3-api-internal-token-2026" \
  https://green.cutdee.com/api/cluster/workers/reload
```

### Test specific worker

```bash
curl -X POST -H "X-Cutdee-Internal: v3-api-internal-token-2026" \
  https://green.cutdee.com/v3api/api/cluster/workers/i9-64gb-cpu-01/test
```

---

## 11. Backup & Recovery

### Files to backup

```bash
# Gateway
/opt/v3-cursor-api/gateway/
/etc/systemd/system/v3-cursor-api-gateway.service
/etc/systemd/system/v3-cursor-api-gateway.service.d/
/etc/v3-cursor-api/gateway.env

# Worker (on each machine)
/opt/v3-cursor-api/worker/
/etc/systemd/system/v3-cursor-api-worker.service
/etc/v3-cursor-api/worker.env

# Data
/var/lib/v3-cursor-api/gateway/
/var/lib/v3-cursor-api/worker/

# Config
/etc/nginx/sites-enabled/green.cutdee.com.conf
```

### Backup command

```bash
ssh root@103.253.75.161 '
# Daily backup
BACKUP_DIR=/backup/v3-cursor-api/$(date +%Y%m%d)
mkdir -p $BACKUP_DIR
tar -czf $BACKUP_DIR/gateway.tar.gz \
  /opt/v3-cursor-api/gateway/app/backend \
  /etc/systemd/system/v3-cursor-api-gateway.service* \
  /etc/v3-cursor-api/gateway.env
tar -czf $BACKUP_DIR/data.tar.gz /var/lib/v3-cursor-api/

# Postgres
PGPASSWORD=$(grep CUTDEE_PG_PASSWORD /etc/v3-cursor-api/gateway.env | cut -d= -f2 | tr -d "\"") \
pg_dump -U v3_cursor_api -h 127.0.0.1 -p 6432 v3_cursor_api | gzip > $BACKUP_DIR/db.sql.gz

echo "  ✓ backup: $BACKUP_DIR"
ls -la $BACKUP_DIR
'
```

---

## 12. Security

### Auth Tokens

- **`CUTDEE_INTERNAL_TOKEN`**: gateway ↔ worker RPC
  - Stored in `/etc/v3-cursor-api/gateway.env` (and `worker.env`)
  - Used in `X-Cutdee-Internal` header
  - **CHANGE this when rotating workers** to prevent unauthorized access

- **`CUTDEE_API_KEYS`**: public API keys (CSV of `cutdee_vdo_xxx`)
  - Currently: 1 admin key
  - Used in `Authorization: Bearer ...` header

### Password Hashing

- PBKDF2-SHA256 (120k iterations, 16-byte salt)
- Stored as `pbkdf2$120000$<salt_hex>$<key_hex>`

### Session Cookies

- `cutdee_session` (30d, HttpOnly, Secure, SameSite=lax)
- Re-set on every login (key rotation)

### SSH Access

| Server | Port | Method | Password |
|---|---|---|---|
| prod gateway | 22 | password | `Dse54fg8*@@2026` |
| prod gateway | 22022 | Tailscale key | (currently disabled) |
| hub | 22 | password | `SJJa0238@@2026` (sj99) |
| license | 22 | password | `2$3Z7Gf1#9hv` |
| Mac | 22 | key | (local) |

### SSL/TLS

- Cloudflare CDN → nginx (TLS termination)
- Self-signed cert: `greenstats.sj88ai.com`
- TLS 1.3 + HTTP/2

---

## 13. Troubleshooting

### Gateway crash loop (status 3/NOTIMPLEMENTED)

**Cause:** Deprecated `--ws-origin=*` flag in drop-in

**Fix:**
```bash
ssh root@103.253.75.161
sed -i 's/--ws-origin=*//; s/--ws=wsproto//' /etc/systemd/system/v3-cursor-api-gateway.service.d/90-loopback-remediation.conf
sudo systemctl daemon-reload
sudo systemctl restart v3-cursor-api-gateway
```

### venv corruption after Phase 4

**Cause:** Python version mismatch (3.9 venv, 3.12 system)

**Fix:**
```bash
ssh root@103.253.75.161
rm -rf /opt/v3-cursor-api/gateway/.venv
cd /opt/v3-cursor-api
chown -R v3api:v3api .
sudo -u v3api python3 -m venv gateway/.venv
sudo -u v3api gateway/.venv/bin/pip install -r gateway/requirements.txt
sudo systemctl restart v3-cursor-api-gateway
```

### v3_cursor_api PG user missing

**Symptom:** Gateway can't connect to DB (psycopg2 auth error)

**Fix:**
```bash
ssh root@103.253.75.161
PGPASSWORD="..." sudo -u postgres psql -c \
  "CREATE USER v3_cursor_api WITH PASSWORD 'tts_saas_pwd_2026'; \
   GRANT ALL ON DATABASE v3_cursor_api TO v3_cursor_api;"
sed -i 's/^CUTDEE_PG_USER=.*/CUTDEE_PG_USER=v3_cursor_api/' /etc/v3-cursor-api/gateway.env
sudo systemctl restart v3-cursor-api-gateway
```

### Worker can't reach gateway (port 22022)

**Fix:** add worker's public key to gateway's `~/.ssh/authorized_keys`

```bash
# On worker (192.168.1.42)
ssh-keygen -t ed25519 -f ~/.ssh/v3_deploy_key -N ''
# copy pub key to user
cat ~/.ssh/v3_deploy_key.pub
# On gateway (103.253.75.161)
echo "<pub_key>" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
systemctl restart sshd
# Enable port 22022 in /etc/ssh/sshd_config
grep -q "^Port 22022" /etc/ssh/sshd_config || echo "Port 22022" >> /etc/ssh/sshd_config
```

### Tailscale tunnel died

**Fix:**
```bash
# On the affected worker
ssh -N -T -R <port>:127.0.0.1:8789 sj99@100.102.13.0 &
# Add to /etc/rc.local for persistence
```

---

## 14. Tailscale Setup

### Tailscale MagicDNS

Workers are reachable via Tailscale IPs:

| Worker | Tailscale | Public IP |
|---|---|---|
| m4-mlx | 100.69.123.5 (hub) | 103.22.183.111 |
| hub | 100.102.13.0 | 103.253.73.29 |
| local Mac | 100.126.135.95 | — |
| 64gb | 100.88.10.14 | — |
| i7 | 100.126.179.46 | — |

### Tailscale ACL (auto-allowed for owner)

- All machines owned by `sj88` are auto-approved
- No special ACL rules needed

### Tailscale status

```bash
# On local Mac
/Applications/Tailscale.app/Contents/MacOS/Tailscale status
```

---

## 15. Cost Optimization

### Tailscale (free for personal use)

- 100 devices free
- Currently using ~20 devices
- Sufficient headroom

### PostgreSQL

- 1 vCPU, 2GB RAM
- ~50 connections (PgBouncer)
- Sufficient for current load

### nginx

- 1 vCPU, 1GB RAM
- Reverse proxy + SSL termination
- Minimal overhead

---

## 16. Future Improvements

### Short-term

- [ ] **Auto-deploy API endpoint** (catch-22 workaround)
  ```python
  POST /api/v1/internal/deploy
  Body: {"ref": "origin/refactor-base", "restart": true}
  → git pull + systemctl restart
  ```

- [ ] **Health check endpoint** with worker count + job stats
- [ ] **Worker auto-discovery** (register workers via heartbeat)
- [ ] **TLS cert renewal** (Cloudflare → 90 days)

### Long-term

- [ ] **Multi-region cluster** (Asia + Europe + US)
- [ ] **WebSocket cluster dashboard** (active jobs across all workers)
- [ ] **Per-job priority** (free vs paid tier)
- [ ] **Per-region routing** (closer worker)
- [ ] **Auto-scaling** (new worker on high load)

---
