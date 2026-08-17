#!/bin/bash
# ============================================
# V3_cursor_API Gateway + Worker installer
# ============================================
# Usage:
#   ./install.sh gateway    # install gateway only
#   ./install.sh worker     # install worker only
#   ./install.sh all        # install both (same host, for dev)
# ============================================
set -e

ROLE="${1:-all}"
SERVICE_USER="v3api"
INTERNAL_TOKEN="${CUTDEE_INTERNAL_TOKEN:-dev-internal-token-change-me}"

echo "==================================="
echo "V3_cursor_API installer (role=$ROLE)"
echo "==================================="

# 1. System user
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "[1/5] Creating system user $SERVICE_USER..."
    useradd -r -s /bin/false -m "$SERVICE_USER"
fi

# 2. Repo dir
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "[2/5] Repo: $REPO_DIR"

# 3. Venv + deps (in repo, not copy — repo IS install dir)
if [[ "$ROLE" == "gateway" || "$ROLE" == "all" ]]; then
    echo "[3/5] Gateway venv + deps..."
    if [ ! -d "$REPO_DIR/gateway/.venv" ]; then
        sudo -u "$SERVICE_USER" python3 -m venv "$REPO_DIR/gateway/.venv"
    fi
    sudo -u "$SERVICE_USER" "$REPO_DIR/gateway/.venv/bin/pip" install -q --upgrade pip
    sudo -u "$SERVICE_USER" "$REPO_DIR/gateway/.venv/bin/pip" install -q -r "$REPO_DIR/gateway/requirements.txt"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR/gateway"
fi

if [[ "$ROLE" == "worker" || "$ROLE" == "all" ]]; then
    echo "[3/5] Worker venv + deps..."
    if [ ! -d "$REPO_DIR/worker/.venv" ]; then
        sudo -u "$SERVICE_USER" python3 -m venv "$REPO_DIR/worker/.venv"
    fi
    sudo -u "$SERVICE_USER" "$REPO_DIR/worker/.venv/bin/pip" install -q --upgrade pip
    sudo -u "$SERVICE_USER" "$REPO_DIR/worker/.venv/bin/pip" install -q -r "$REPO_DIR/worker/requirements.txt"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR/worker"
fi

# 4. Data dirs
mkdir -p /var/lib/v3-cursor-api/{gateway,worker}/jobs
mkdir -p /var/log/v3-cursor-api
chown -R "$SERVICE_USER:$SERVICE_USER" /var/lib/v3-cursor-api /var/log/v3-cursor-api

# 5. Systemd services
echo "[5/5] Installing systemd services..."

mkdir -p /etc/v3-cursor-api

if [[ "$ROLE" == "gateway" || "$ROLE" == "all" ]]; then
    cat > /etc/systemd/system/v3-cursor-api-gateway.service << EOF
[Unit]
Description=V3_cursor_API Gateway (Green Screen Cluster)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$REPO_DIR/gateway
EnvironmentFile=/etc/v3-cursor-api/gateway.env
ExecStart=$REPO_DIR/gateway/.venv/bin/python3 -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8788
Restart=always
RestartSec=5
MemoryHigh=2G
MemoryMax=4G
StandardOutput=append:/var/log/v3-cursor-api/gateway.log
StandardError=append:/var/log/v3-cursor-api/gateway.log

[Install]
WantedBy=multi-user.target
EOF
    if [ ! -f /etc/v3-cursor-api/gateway.env ]; then
        cat > /etc/v3-cursor-api/gateway.env << EOF
CUTDEE_INTERNAL_TOKEN=$INTERNAL_TOKEN
GATEWAY_PORT=8788
GATEWAY_DATA_DIR=/var/lib/v3-cursor-api/gateway
CUTDEE_PG_HOST=127.0.0.1
CUTDEE_PG_PORT=6432
CUTDEE_PG_NAME=cutdee_cluster
CUTDEE_PG_USER=cutdee_cluster
CUTDEE_PG_PASSWORD=cutdee_cluster_pwd_2026
EOF
        chmod 600 /etc/v3-cursor-api/gateway.env
    fi

    if [ ! -f /var/lib/v3-cursor-api/gateway/workers.json ]; then
        cat > /var/lib/v3-cursor-api/gateway/workers.json << EOF
{
  "workers": [
    {
      "id": "i9-64gb-cpu-01",
      "name": "sj88-i9-64gb (i9-12900K 24C 62GB, CPU)",
      "url": "http://127.0.0.1:8789",
      "tier": "high",
      "max_concurrent": 4,
      "enabled": true
    }
  ]
}
EOF
        chown $SERVICE_USER:$SERVICE_USER /var/lib/v3-cursor-api/gateway/workers.json
    fi
    systemctl daemon-reload
    systemctl enable v3-cursor-api-gateway.service
    systemctl restart v3-cursor-api-gateway.service
fi

if [[ "$ROLE" == "worker" || "$ROLE" == "all" ]]; then
    cat > /etc/systemd/system/v3-cursor-api-worker.service << EOF
[Unit]
Description=V3_cursor_API Worker (Green Screen Render)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$REPO_DIR/worker
EnvironmentFile=/etc/v3-cursor-api/worker.env
ExecStart=$REPO_DIR/worker/.venv/bin/python3 -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8789
Restart=always
RestartSec=5
MemoryHigh=4G
MemoryMax=8G
StandardOutput=append:/var/log/v3-cursor-api/worker.log
StandardError=append:/var/log/v3-cursor-api/worker.log

[Install]
WantedBy=multi-user.target
EOF
    if [ ! -f /etc/v3-cursor-api/worker.env ]; then
        cat > /etc/v3-cursor-api/worker.env << EOF
CUTDEE_INTERNAL_TOKEN=$INTERNAL_TOKEN
WORKER_PORT=8789
WORKER_ID=$(hostname)-cpu-01
WORKER_DATA_DIR=/var/lib/v3-cursor-api/worker
EOF
        chmod 600 /etc/v3-cursor-api/worker.env
    fi
    systemctl daemon-reload
    systemctl enable v3-cursor-api-worker.service
    systemctl restart v3-cursor-api-worker.service
fi

echo ""
echo "==================================="
echo "✅ Install complete"
echo "==================================="
echo "Gateway:  http://127.0.0.1:8788/healthz"
echo "Worker:   http://127.0.0.1:8789/health"
echo "Logs:     /var/log/v3-cursor-api/"
echo ""
echo "Edit /var/lib/v3-cursor-api/gateway/workers.json to add more workers"
echo "Then: curl -X POST http://127.0.0.1:8788/api/cluster/workers/reload -H 'X-Cutdee-Internal: $INTERNAL_TOKEN'"
