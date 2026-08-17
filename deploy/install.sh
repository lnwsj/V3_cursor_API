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
INSTALL_DIR="/opt/v3-cursor-api"
SERVICE_USER="v3api"
INTERNAL_TOKEN="${CUTDEE_INTERNAL_TOKEN:-dev-internal-token-change-me}"

echo "==================================="
echo "V3_cursor_API installer (role=$ROLE)"
echo "==================================="

# 1. System user
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "[1/5] Creating system user $SERVICE_USER..."
    useradd -r -s /bin/false -d "$INSTALL_DIR" -m "$SERVICE_USER"
fi

# 2. Create dirs
echo "[2/5] Creating directories..."
mkdir -p "$INSTALL_DIR"/{gateway,worker,data,logs}
mkdir -p /var/lib/v3-cursor-api/{gateway,worker}
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" /var/lib/v3-cursor-api

# 3. Copy code
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

if [[ "$ROLE" == "gateway" || "$ROLE" == "all" ]]; then
    echo "[3/5] Copying gateway code..."
    cp -r "$REPO_DIR/gateway/app" "$INSTALL_DIR/gateway/"
    cp "$REPO_DIR/gateway/requirements.txt" "$INSTALL_DIR/gateway/"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/gateway"
fi

if [[ "$ROLE" == "worker" || "$ROLE" == "all" ]]; then
    echo "[3/5] Copying worker code..."
    cp -r "$REPO_DIR/worker/app" "$INSTALL_DIR/worker/"
    cp "$REPO_DIR/worker/requirements.txt" "$INSTALL_DIR/worker/"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/worker"
fi

# 4. Venv + deps
if [[ "$ROLE" == "gateway" || "$ROLE" == "all" ]]; then
    echo "[4/5] Gateway venv + deps..."
    if [ ! -d "$INSTALL_DIR/gateway/.venv" ]; then
        sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/gateway/.venv"
    fi
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/gateway/.venv/bin/pip" install -q -r "$INSTALL_DIR/gateway/requirements.txt"
fi

if [[ "$ROLE" == "worker" || "$ROLE" == "all" ]]; then
    echo "[4/5] Worker venv + deps..."
    if [ ! -d "$INSTALL_DIR/worker/.venv" ]; then
        sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/worker/.venv"
    fi
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/worker/.venv/bin/pip" install -q -r "$INSTALL_DIR/worker/requirements.txt"
fi

# 5. Systemd
echo "[5/5] Installing systemd services..."

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
WorkingDirectory=$INSTALL_DIR/gateway
EnvironmentFile=/etc/v3-cursor-api/gateway.env
ExecStart=$INSTALL_DIR/gateway/.venv/bin/python3 -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8788
Restart=always
RestartSec=5
MemoryHigh=2G
MemoryMax=4G
StandardOutput=append:/var/log/v3-cursor-api/gateway.log
StandardError=append:/var/log/v3-cursor-api/gateway.log

[Install]
WantedBy=multi-user.target
EOF
    mkdir -p /etc/v3-cursor-api /var/log/v3-cursor-api
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
WorkingDirectory=$INSTALL_DIR/worker
EnvironmentFile=/etc/v3-cursor-api/worker.env
ExecStart=$INSTALL_DIR/worker/.venv/bin/python3 -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8789
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
    fi
    systemctl daemon-reload
    systemctl enable v3-cursor-api-worker.service
    systemctl restart v3-cursor-api-worker.service
fi

# 6. Default workers.json (only if gateway)
if [[ "$ROLE" == "gateway" || "$ROLE" == "all" ]]; then
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
fi

echo ""
echo "==================================="
echo "✅ Install complete"
echo "==================================="
echo "Gateway:  http://127.0.0.1:8788/healthz"
echo "Worker:   http://127.0.0.1:8789/health"
echo "Logs:     /var/log/v3-cursor-api/"
echo ""
echo "Add workers to /var/lib/v3-cursor-api/gateway/workers.json"
echo "Then: curl -X POST http://127.0.0.1:8788/api/cluster/workers/reload -H 'X-Cutdee-Internal: $INTERNAL_TOKEN'"
