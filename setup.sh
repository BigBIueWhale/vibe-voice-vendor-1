#!/usr/bin/env bash
set -euo pipefail

# Parse flags
FORCE_REBUILD=false
BACKEND="vibevoice"
GROQ_API_KEY=""
GROQ_MODEL_NAME="whisper-large-v3"

print_usage() {
    echo "Usage: ./setup.sh [--force-rebuild] [--backend vibevoice|groq] [--groq-api-key KEY] [--groq-model-name MODEL]"
    echo ""
    echo "Backends:"
    echo "  vibevoice  Local vLLM + VibeVoice-ASR-7B on GPU (default)"
    echo "  groq       Groq cloud API running Whisper large-v3 (no GPU needed)"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force-rebuild) FORCE_REBUILD=true; shift ;;
        --backend)
            BACKEND="$2"
            if [[ "$BACKEND" != "vibevoice" && "$BACKEND" != "groq" ]]; then
                echo "ERROR: --backend must be 'vibevoice' or 'groq', got '$BACKEND'"
                exit 1
            fi
            shift 2 ;;
        --groq-api-key) GROQ_API_KEY="$2"; shift 2 ;;
        --groq-model-name) GROQ_MODEL_NAME="$2"; shift 2 ;;
        --help|-h) print_usage; exit 0 ;;
        *) echo "Unknown flag: $1"; print_usage; exit 1 ;;
    esac
done

if [[ "$BACKEND" == "groq" && -z "$GROQ_API_KEY" ]]; then
    echo "ERROR: --groq-api-key is required when --backend is groq"
    exit 1
fi

echo "ASR backend: $BACKEND"

# Step 1: Validate environment
if [[ ! -f Dockerfile ]]; then
    echo "ERROR: Dockerfile not found in $(pwd)"
    echo "This script must be run from the vibe-voice-vendor project root."
    echo "  Expected: directory containing Dockerfile, VibeVoice/, rust_proxy/, deploy/"
    echo "  Got: $(ls -la)"
    exit 1
fi

# Step 2: Check prerequisites
# ffprobe is used by server/audio.py:probe_duration on every transcribe request
# (both backends). ffmpeg is only used by compress_to_opus (groq backend).
if [[ "$BACKEND" == "vibevoice" ]]; then
    REQUIRED_CMDS=(docker uv cargo git curl ffprobe)
else
    REQUIRED_CMDS=(uv cargo git curl ffprobe ffmpeg)
fi

for cmd in "${REQUIRED_CMDS[@]}"; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found in PATH"
        echo "  PATH=$PATH"
        echo "  Install instructions:"
        echo "    docker:           https://docs.docker.com/engine/install/ubuntu/"
        echo "    uv:               curl -LsSf https://astral.sh/uv/install.sh | sh"
        echo "    cargo:            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
        echo "    git:              sudo apt-get install git"
        echo "    curl:             sudo apt-get install curl"
        echo "    ffprobe/ffmpeg:   sudo apt-get install ffmpeg"
        exit 1
    fi
done

if [[ "$BACKEND" == "vibevoice" ]]; then
    if ! docker info >/dev/null 2>&1; then
        echo "ERROR: Cannot connect to the Docker daemon as the current user (uid=$(id -u))"
        echo "  Groups: $(id -Gn)"
        echo "  Most likely cause: this user is not in the 'docker' group."
        echo "  Fix:"
        echo "    sudo usermod -aG docker \$USER"
        echo "    newgrp docker   # or log out and back in"
        echo "    ./setup.sh"
        echo "  If the daemon itself is down, check: systemctl status docker"
        exit 1
    fi
    # Scope the nvidia check to .Runtimes so unrelated 'nvidia' substrings
    # (image names, labels, contexts) elsewhere in `docker info` can't false-positive.
    if ! docker info --format '{{.Runtimes}}' 2>/dev/null | grep -qw nvidia; then
        echo "ERROR: Docker does not have the 'nvidia' runtime registered"
        echo "  Registered runtimes: $(docker info --format '{{.Runtimes}}' 2>/dev/null || echo '(docker info failed)')"
        echo "  Install nvidia-container-toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
        exit 1
    fi
fi

# Step 3: Clone VibeVoice if missing (vibevoice backend only)
if [[ "$BACKEND" == "vibevoice" ]]; then
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
    if [[ "$EXPECTED_COMMIT" != "$ACTUAL_COMMIT"* ]]; then
        echo "ERROR: VibeVoice is at commit $ACTUAL_COMMIT, expected $EXPECTED_COMMIT"
        echo "  Fix: git -C VibeVoice checkout $EXPECTED_COMMIT"
        exit 1
    fi
fi

# Step 4: Stop existing services (if running)
echo "Stopping existing services..."
systemctl --user stop vibevoice-proxy 2>/dev/null || true
systemctl --user stop vibevoice-server 2>/dev/null || true

# Step 5: Stop and remove existing Docker container (if present)
if docker container inspect vibevoice-vllm &>/dev/null 2>&1; then
    echo "Removing existing vibevoice-vllm container..."
    docker stop vibevoice-vllm
    docker rm vibevoice-vllm
fi

if [[ "$BACKEND" == "vibevoice" ]]; then
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

if [[ "$BACKEND" == "groq" ]]; then
    # Generate Groq-mode service file
    cat > ~/.config/systemd/user/vibevoice-server.service <<SERVICEEOF
[Unit]
Description=VibeVoice ASR Server (Groq Whisper backend)
After=network-online.target default.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Desktop/vibe-voice-vendor
ExecStart=%h/Desktop/vibe-voice-vendor/.venv/bin/python -m server \\
    --asr-backend groq \\
    --groq-api-key ${GROQ_API_KEY} \\
    --groq-model-name ${GROQ_MODEL_NAME} \\
    --host 127.0.0.1 \\
    --port 54912 \\
    --max-audio-bytes 524288000 \\
    --max-queue-size 50 \\
    --jwt-public-key-file %h/Desktop/vibe-voice-vendor/keys/public.pem \\
    --revoked-tokens-file %h/Desktop/vibe-voice-vendor/revoked_tokens.txt \\
    --require-https true
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SERVICEEOF
else
    cp deploy/vibevoice-server.service ~/.config/systemd/user/
fi

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
if [[ "$BACKEND" == "vibevoice" ]]; then
    echo "Waiting for vLLM to become healthy (normally takes ~2 minutes)..."
    TRIES=0
    MAX_TRIES=36  # 36 * 5s = 3 minutes
    while (( TRIES < MAX_TRIES )); do
        if curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:37845/health 2>/dev/null | grep -q 200; then
            break
        fi
        (( ++TRIES ))
        sleep 5
    done

    if (( TRIES == MAX_TRIES )); then
        echo "ERROR: vLLM did not become healthy within 3 minutes"
        echo "  Container status: $(docker inspect -f '{{.State.Status}}' vibevoice-vllm 2>/dev/null || echo 'not found')"
        echo "  Last 30 lines of container logs:"
        docker logs --tail 30 vibevoice-vllm 2>&1 || true
        exit 1
    fi
fi

HEALTH=$(curl -sk https://127.0.0.1:42862/health 2>/dev/null || echo "FAILED")
if [[ "$HEALTH" != *'"status":"ok"'* ]]; then
    echo "ERROR: Full stack health check failed"
    if [[ "$BACKEND" == "vibevoice" ]]; then
        echo "  vLLM direct:  $(curl -s http://127.0.0.1:37845/health 2>/dev/null || echo 'FAILED')"
    fi
    echo "  Server direct: $(curl -s http://127.0.0.1:54912/health 2>/dev/null || echo 'FAILED')"
    echo "  Proxy (full):  $HEALTH"
    echo "  vibevoice-server status: $(systemctl --user is-active vibevoice-server 2>/dev/null)"
    echo "  vibevoice-proxy status:  $(systemctl --user is-active vibevoice-proxy 2>/dev/null)"
    exit 1
fi

echo ""
echo "Setup complete. All services healthy."
echo "  Backend: $BACKEND"
if [[ "$BACKEND" == "vibevoice" ]]; then
    echo "  vLLM:   http://127.0.0.1:37845"
fi
echo "  Server: http://127.0.0.1:54912"
echo "  Proxy:  https://127.0.0.1:42862"
if [[ -f keys/token.txt ]]; then
    echo "  Token:  keys/token.txt"
fi
