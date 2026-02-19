#!/usr/bin/env bash
set -euo pipefail

# Parse flags
FORCE_REBUILD=false
for arg in "$@"; do
    case "$arg" in
        --force-rebuild) FORCE_REBUILD=true ;;
        *) echo "Unknown flag: $arg"; echo "Usage: ./setup.sh [--force-rebuild]"; exit 1 ;;
    esac
done

# Step 1: Validate environment
if [[ ! -f Dockerfile ]]; then
    echo "ERROR: Dockerfile not found in $(pwd)"
    echo "This script must be run from the vibe-voice-vendor project root."
    echo "  Expected: directory containing Dockerfile, VibeVoice/, rust_proxy/, deploy/"
    echo "  Got: $(ls -la)"
    exit 1
fi

# Step 2: Check prerequisites
for cmd in docker uv cargo git curl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found in PATH"
        echo "  PATH=$PATH"
        echo "  Install instructions:"
        echo "    docker: https://docs.docker.com/engine/install/ubuntu/"
        echo "    uv:     curl -LsSf https://astral.sh/uv/install.sh | sh"
        echo "    cargo:  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
        echo "    git:    sudo apt-get install git"
        echo "    curl:   sudo apt-get install curl"
        exit 1
    fi
done

if ! docker info 2>/dev/null | grep -qi nvidia; then
    echo "ERROR: Docker does not appear to have NVIDIA GPU support"
    echo "  'docker info' output (GPU-related):"
    docker info 2>&1 | grep -i -E 'runtime|nvidia|gpu' || echo "  (none found)"
    echo "  Install nvidia-container-toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
    exit 1
fi

# Step 3: Clone VibeVoice if missing
if [[ ! -d VibeVoice ]]; then
    echo "Cloning VibeVoice..."
    git clone https://github.com/microsoft/VibeVoice.git --recurse-submodules
    git -C VibeVoice checkout 1807b858
elif [[ ! -f VibeVoice/pyproject.toml ]]; then
    echo "ERROR: VibeVoice/ directory exists but looks incomplete (no pyproject.toml)"
    echo "  Contents: $(ls VibeVoice/)"
    echo "  Delete it and re-run: rm -rf VibeVoice && ./setup.sh"
    exit 1
else
    echo "VibeVoice/ already exists, skipping clone"
fi

EXPECTED_COMMIT="1807b858"
ACTUAL_COMMIT=$(git -C VibeVoice rev-parse --short HEAD)
if [[ "$ACTUAL_COMMIT" != "$EXPECTED_COMMIT"* ]]; then
    echo "ERROR: VibeVoice is at commit $ACTUAL_COMMIT, expected $EXPECTED_COMMIT"
    echo "  Fix: git -C VibeVoice checkout $EXPECTED_COMMIT"
    exit 1
fi

# Step 4: Stop existing services (if running)
echo "Stopping existing services..."
systemctl --user stop vibevoice-proxy 2>/dev/null || true
systemctl --user stop vibevoice-server 2>/dev/null || true

# Step 5: Stop and remove existing Docker container
if docker container inspect vibevoice-vllm &>/dev/null; then
    echo "Removing existing vibevoice-vllm container..."
    docker stop vibevoice-vllm
    docker rm vibevoice-vllm
fi

# Step 6: Build Docker image if needed
if $FORCE_REBUILD || ! docker image inspect vibevoice-vllm &>/dev/null; then
    echo "Building vibevoice-vllm Docker image (downloads ~14 GB model on first build)..."
    docker build -t vibevoice-vllm .
    if ! docker image inspect vibevoice-vllm &>/dev/null; then
        echo "ERROR: docker build appeared to succeed but image 'vibevoice-vllm' not found"
        echo "  Docker images: $(docker images --format '{{.Repository}}:{{.Tag}}' | head -10)"
        exit 1
    fi
else
    echo "Docker image vibevoice-vllm already exists, skipping build (use --force-rebuild to override)"
fi

# Step 7: Start Docker container
echo "Starting vibevoice-vllm container..."
docker run -d --gpus all --name vibevoice-vllm \
    --ipc=host --restart unless-stopped \
    -p 127.0.0.1:37845:8000 \
    vibevoice-vllm:latest

sleep 2
CONTAINER_STATUS=$(docker inspect -f '{{.State.Status}}' vibevoice-vllm 2>/dev/null || echo "not found")
if [[ "$CONTAINER_STATUS" != "running" ]]; then
    echo "ERROR: Container vibevoice-vllm is not running (status: $CONTAINER_STATUS)"
    echo "  Last 20 lines of logs:"
    docker logs --tail 20 vibevoice-vllm 2>&1 || true
    exit 1
fi

# Step 8: Install Python dependencies
echo "Installing Python dependencies..."
uv sync --no-dev

# Step 9: Generate JWT keys if missing
if [[ ! -f keys/public.pem ]]; then
    echo "Generating JWT key pair and token..."
    uv run python -m scripts.generate_token --keys-dir keys --subject user
    touch revoked_tokens.txt
    echo "Token saved to keys/token.txt"
else
    echo "JWT keys already exist at keys/, skipping generation"
fi

# Step 10: Build Rust TLS proxy
echo "Building Rust TLS proxy..."
(cd rust_proxy && cargo build --release)

if [[ ! -x rust_proxy/target/release/vvv_proxy ]]; then
    echo "ERROR: cargo build succeeded but binary not found at rust_proxy/target/release/vvv_proxy"
    echo "  Contents of rust_proxy/target/release/:"
    ls -la rust_proxy/target/release/ 2>/dev/null | head -10 || echo "  (directory not found)"
    exit 1
fi

# Step 11: Install and start systemd services
echo "Installing systemd services..."
mkdir -p ~/.config/systemd/user
cp deploy/vibevoice-server.service ~/.config/systemd/user/
cp deploy/vibevoice-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vibevoice-server
systemctl --user enable --now vibevoice-proxy

sleep 2
for svc in vibevoice-server vibevoice-proxy; do
    STATUS=$(systemctl --user is-active "$svc" 2>/dev/null || echo "unknown")
    if [[ "$STATUS" != "active" ]]; then
        echo "ERROR: $svc is not active (status: $STATUS)"
        echo "  Journal (last 20 lines):"
        journalctl --user -u "$svc" --no-pager -n 20 2>&1 || true
        exit 1
    fi
done

# Step 12: Wait for health
echo "Waiting for vLLM to become healthy (this takes ~90 seconds)..."
TRIES=0
MAX_TRIES=36  # 36 * 5s = 3 minutes
while (( TRIES < MAX_TRIES )); do
    if curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:37845/health 2>/dev/null | grep -q 200; then
        break
    fi
    (( TRIES++ ))
    sleep 5
done

if (( TRIES == MAX_TRIES )); then
    echo "ERROR: vLLM did not become healthy within 3 minutes"
    echo "  Container status: $(docker inspect -f '{{.State.Status}}' vibevoice-vllm 2>/dev/null || echo 'not found')"
    echo "  Last 30 lines of container logs:"
    docker logs --tail 30 vibevoice-vllm 2>&1 || true
    exit 1
fi

HEALTH=$(curl -sk https://127.0.0.1:42862/health 2>/dev/null || echo "FAILED")
if [[ "$HEALTH" != *'"status":"ok"'* ]]; then
    echo "ERROR: Full stack health check failed"
    echo "  vLLM direct:  $(curl -s http://127.0.0.1:37845/health 2>/dev/null || echo 'FAILED')"
    echo "  Server direct: $(curl -s http://127.0.0.1:54912/health 2>/dev/null || echo 'FAILED')"
    echo "  Proxy (full):  $HEALTH"
    echo "  vibevoice-server status: $(systemctl --user is-active vibevoice-server 2>/dev/null)"
    echo "  vibevoice-proxy status:  $(systemctl --user is-active vibevoice-proxy 2>/dev/null)"
    exit 1
fi

echo ""
echo "Setup complete. All services healthy."
echo "  vLLM:   http://127.0.0.1:37845"
echo "  Server: http://127.0.0.1:54912"
echo "  Proxy:  https://127.0.0.1:42862"
if [[ -f keys/token.txt ]]; then
    echo "  Token:  keys/token.txt"
fi
