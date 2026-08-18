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
#
# FIX (2026-08-18): Ubuntu 26.04 ships only Python 3.14, but pydantic-core 2.23
# (pinned by pydantic==2.9 in requirements.txt) requires Python ≤ 3.13. pyo3 0.22
# build fails with "Python 3.14 is newer than PyO3's maximum supported version".
#
# Detect Python version and fall back to Python 3.12 via uv if 3.14 is installed.
# uv is fast (~3s to install Python 3.12) and avoids the rustc/pydantic-core
# build dance. uv is installed as the SERVICE_USER so venv ownership is correct.
_PYTHON_BIN="$(command -v python3 || true)"
_PY_VER="$(${_PYTHON_BIN:-python3} -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)"
_PY_OK="yes"
case "${_PY_VER}" in
    3.12|3.13) _PY_OK="yes" ;;
    3.14|3.15|3.16|3.17|3.18|3.19|3.20) _PY_OK="no" ;;
    *) _PY_OK="yes" ;;  # older or unknown — try the system python first
esac

if [[ "$_PY_OK" == "no" ]]; then
    echo "[3/5] Detected Python ${_PY_VER} (pydantic-core 2.23 needs ≤3.13) — installing uv + Python 3.12"
    # Ensure uv is installed for the SERVICE_USER
    if ! sudo -u "$SERVICE_USER" bash -lc 'command -v uv >/dev/null 2>&1'; then
        sudo -u "$SERVICE_USER" bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1'
    fi
    # Ensure Python 3.12 is installed (uv install is idempotent)
    sudo -u "$SERVICE_USER" bash -lc 'cd /tmp && HOME=$HOME /var/lib/$USER/.local/bin/uv --no-config python install 3.12 >/dev/null 2>&1' || true
    _PYTHON_BIN="/var/lib/$SERVICE_USER/.local/bin/uv"
    _PY_FIND=$(sudo -u "$SERVICE_USER" bash -lc 'cd /tmp && HOME=$HOME /var/lib/$USER/.local/bin/uv --no-config python find 3.12 2>/dev/null' || echo unknown)
    echo "Python 3.12 installed via uv (${_PY_FIND})"
fi

_venv_create() {
    local venv_dir="$1"
    if [ ! -d "$venv_dir" ]; then
        if [[ "$_PY_OK" == "no" ]]; then
            sudo -u "$SERVICE_USER" bash -lc "cd /tmp && HOME=\$HOME /var/lib/$SERVICE_USER/.local/bin/uv --no-config venv --python 3.12 '$venv_dir' 2>&1"
        else
            sudo -u "$SERVICE_USER" "$_PYTHON_BIN" -m venv "$venv_dir"
        fi
    fi
}

_venv_install_reqs() {
    local venv_dir="$1"
    local req_file="$2"
    if [[ "$_PY_OK" == "no" ]]; then
        sudo -u "$SERVICE_USER" bash -lc "HOME=\$HOME /var/lib/$SERVICE_USER/.local/bin/uv --no-config pip install --python '$venv_dir/bin/python' -r '$req_file' 2>&1"
    else
        sudo -u "$SERVICE_USER" "$venv_dir/bin/pip" install -q --upgrade pip
        sudo -u "$SERVICE_USER" "$venv_dir/bin/pip" install -q -r "$req_file"
    fi
}

if [[ "$ROLE" == "gateway" || "$ROLE" == "all" ]]; then
    echo "[3/5] Gateway venv + deps..."
    _venv_create "$REPO_DIR/gateway/.venv"
    _venv_install_reqs "$REPO_DIR/gateway/.venv" "$REPO_DIR/gateway/requirements.txt"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR/gateway"
fi

if [[ "$ROLE" == "worker" || "$ROLE" == "all" ]]; then
    echo "[3/5] Worker venv + deps..."
    _venv_create "$REPO_DIR/worker/.venv"
    _venv_install_reqs "$REPO_DIR/worker/.venv" "$REPO_DIR/worker/requirements.txt"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR/worker"
fi

# 3b. Optional: Tailscale setup (for direct worker-to-gateway connectivity, bypass hub)
# FIX (2026-08-18): on Ubuntu 26.04, the default tailscale-1.56.x package only supports
# kernel 5.15+; if running on a custom kernel, install from tailscale.com instead.
# This block is a no-op if Tailscale is already installed. Authentication requires
# TS_AUTHKEY env var (or run `tailscale up` manually).
if [[ "${SKIP_TAILSCALE:-0}" != "1" ]]; then
    if ! command -v tailscale >/dev/null 2>&1; then
        if [ -n "$TS_AUTHKEY" ]; then
            echo "[3b] Installing Tailscale (auth key provided)..."
            curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1
            tailscale up --authkey="$TS_AUTHKEY" --hostname="${TS_HOSTNAME:-v3-worker-$(hostname -s)}"
            echo "Tailscale authenticated: $(tailscale ip -4 2>/dev/null)"
        else
            echo "[3b] Tailscale not installed (no TS_AUTHKEY) — gateway uses hub tunnel"
            echo "      To enable direct worker-to-gateway: get auth key from https://login.tailscale.com/admin"
        fi
    fi
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
