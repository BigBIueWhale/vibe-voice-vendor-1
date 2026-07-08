#!/usr/bin/env bash
set -euo pipefail

SERVER_SERVICE="vibevoice-server"
PROXY_SERVICE="vibevoice-proxy"
VLLM_CONTAINER="vibevoice-vllm"
VLLM_IMAGE="vibevoice-vllm:latest"
VIBEVOICE_COMMIT="1807b858d4f7dffdd286249a01616c243e488c9e"
VIBEVOICE_MODEL_REVISION="d0c9efdb8d614685062c04425d91e01b6f37d944"
GPU_SMOKE_IMAGE="nvidia/cuda:12.8.0-base-ubuntu24.04"

VLLM_PORT=37845
SERVER_PORT=54912
PROXY_PORT=42862
MAX_AUDIO_BYTES=524288000
MAX_QUEUE_SIZE=50

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"
INSTALL_DIR="$SCRIPT_DIR"
USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
APP_CONFIG_DIR="$HOME/.config/vibevoice-vendor"

FORCE_REBUILD=false
BACKEND="vibevoice"
GROQ_API_KEY=""
GROQ_MODEL_NAME="whisper-large-v3"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

print_usage() {
    echo "Usage: ./setup.sh [--force-rebuild] [--backend vibevoice|groq] [--groq-api-key KEY] [--groq-model-name MODEL]"
    echo ""
    echo "Backends:"
    echo "  vibevoice  Local vLLM + VibeVoice-ASR-7B on GPU (default)"
    echo "  groq       Groq cloud API running Whisper large-v3 (no GPU needed)"
}

require_value() {
    local flag="$1"
    local value="${2:-}"
    [[ -n "$value" ]] || die "$flag requires a value"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force-rebuild)
            FORCE_REBUILD=true
            shift
            ;;
        --backend)
            require_value "$1" "${2:-}"
            BACKEND="$2"
            [[ "$BACKEND" == "vibevoice" || "$BACKEND" == "groq" ]] \
                || die "--backend must be 'vibevoice' or 'groq', got '$BACKEND'"
            shift 2
            ;;
        --groq-api-key)
            require_value "$1" "${2:-}"
            GROQ_API_KEY="$2"
            shift 2
            ;;
        --groq-model-name)
            require_value "$1" "${2:-}"
            GROQ_MODEL_NAME="$2"
            shift 2
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        *)
            print_usage
            die "Unknown flag: $1"
            ;;
    esac
done

[[ "$BACKEND" != "groq" || -n "$GROQ_API_KEY" ]] \
    || die "--groq-api-key is required when --backend is groq"

validate_simple_systemd_path() {
    local path="$1"
    [[ "$path" == /* ]] || die "Install path must be absolute: $path"
    [[ "$path" =~ ^/[A-Za-z0-9._/@+-]+$ ]] \
        || die "Install path contains characters this systemd renderer refuses: $path"
}

validate_env_value() {
    local name="$1"
    local value="$2"
    [[ "$value" =~ ^[A-Za-z0-9._:/+=@-]+$ ]] \
        || die "$name contains unsupported characters for an EnvironmentFile value"
}

require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Required command '$cmd' not found in PATH=$PATH" >&2
        echo "Install instructions:" >&2
        echo "  docker:           https://docs.docker.com/engine/install/ubuntu/" >&2
        echo "  nvidia runtime:   nvidia-container-toolkit + nvidia-ctk runtime configure --runtime=docker" >&2
        echo "  uv:               curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
        echo "  cargo:            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh" >&2
        echo "  git:              sudo apt-get install git" >&2
        echo "  curl:             sudo apt-get install curl" >&2
        echo "  ffprobe/ffmpeg:   sudo apt-get install ffmpeg" >&2
        exit 1
    fi
}

validate_environment() {
    [[ -f "$INSTALL_DIR/Dockerfile" ]] \
        || die "Dockerfile not found in $INSTALL_DIR; run setup from the project checkout"
    [[ -d "$INSTALL_DIR/server" && -d "$INSTALL_DIR/rust_proxy" && -d "$INSTALL_DIR/deploy" ]] \
        || die "$INSTALL_DIR does not look like the vibe-voice-vendor project root"
    validate_simple_systemd_path "$INSTALL_DIR"

    if [[ "$BACKEND" == "vibevoice" ]]; then
        for cmd in docker uv cargo git curl ffprobe loginctl; do
            require_cmd "$cmd"
        done
    else
        for cmd in uv cargo curl ffprobe ffmpeg loginctl; do
            require_cmd "$cmd"
        done
        validate_env_value "GROQ_API_KEY" "$GROQ_API_KEY"
        validate_env_value "GROQ_MODEL_NAME" "$GROQ_MODEL_NAME"
    fi
}

verify_docker_gpu() {
    docker info >/dev/null 2>&1 || {
        echo "Cannot connect to the Docker daemon as user $(id -un) (uid=$(id -u))" >&2
        echo "Groups: $(id -Gn)" >&2
        echo "Fix Docker access, then rerun setup. If group membership changed, log out/in or run: newgrp docker" >&2
        exit 1
    }

    if ! docker info --format '{{.Runtimes}}' 2>/dev/null | grep -qw nvidia; then
        echo "Registered Docker runtimes: $(docker info --format '{{.Runtimes}}' 2>/dev/null || true)" >&2
        die "Docker does not have the 'nvidia' runtime registered"
    fi

    echo "Verifying Docker GPU passthrough with $GPU_SMOKE_IMAGE..."
    local output
    if ! output="$(docker run --rm --gpus all "$GPU_SMOKE_IMAGE" nvidia-smi 2>&1)"; then
        echo "$output" >&2
        if [[ "$output" == *"failed to fulfil mount request"* || "$output" == *"libnvidia"* ]]; then
            echo "" >&2
            echo "The NVIDIA Docker runtime is registered but its host mount spec is stale or broken." >&2
            echo "Regenerate it, then rerun setup:" >&2
            echo "  sudo systemctl start nvidia-cdi-refresh.service" >&2
            echo "  sudo systemctl restart docker" >&2
            echo "  docker run --rm --gpus all $GPU_SMOKE_IMAGE nvidia-smi" >&2
        fi
        exit 1
    fi
}

checkout_vibevoice() {
    if [[ ! -d VibeVoice ]]; then
        echo "Cloning VibeVoice..."
        git clone https://github.com/microsoft/VibeVoice.git VibeVoice
    elif [[ ! -d VibeVoice/.git || ! -f VibeVoice/pyproject.toml ]]; then
        die "VibeVoice/ exists but is not a complete git checkout; remove it and rerun setup"
    else
        echo "VibeVoice/ already exists"
    fi

    if [[ -n "$(git -C VibeVoice status --porcelain)" ]]; then
        die "VibeVoice/ has local changes; refusing to overwrite a non-pristine vendor checkout"
    fi

    git -C VibeVoice fetch --tags origin
    git -C VibeVoice cat-file -e "$VIBEVOICE_COMMIT^{commit}" 2>/dev/null \
        || die "VibeVoice commit $VIBEVOICE_COMMIT is not present after fetch"
    git -C VibeVoice checkout --detach "$VIBEVOICE_COMMIT"
    git -C VibeVoice submodule sync --recursive
    git -C VibeVoice submodule update --init --recursive

    local actual
    actual="$(git -C VibeVoice rev-parse HEAD)"
    [[ "$actual" == "$VIBEVOICE_COMMIT" ]] \
        || die "VibeVoice is at $actual, expected $VIBEVOICE_COMMIT"
}

stop_existing_services() {
    echo "Stopping existing services..."
    systemctl --user stop "$PROXY_SERVICE" 2>/dev/null || true
    systemctl --user stop "$SERVER_SERVICE" 2>/dev/null || true

    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        if docker container inspect "$VLLM_CONTAINER" >/dev/null 2>&1; then
            echo "Removing existing $VLLM_CONTAINER container..."
            docker stop "$VLLM_CONTAINER" >/dev/null 2>&1 || true
            docker rm "$VLLM_CONTAINER"
        fi
    fi
}

build_and_start_vllm() {
    if $FORCE_REBUILD || ! docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1; then
        echo "Building $VLLM_IMAGE (downloads pinned ~14 GB model snapshot on first build)..."
        docker build \
            --build-arg "VIBEVOICE_MODEL_REVISION=$VIBEVOICE_MODEL_REVISION" \
            -t "$VLLM_IMAGE" .
    else
        echo "Docker image $VLLM_IMAGE already exists, skipping build (use --force-rebuild to override)"
    fi

    docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1 \
        || die "docker build completed but image $VLLM_IMAGE is not present"

    echo "Starting $VLLM_CONTAINER container..."
    docker run -d --gpus all --name "$VLLM_CONTAINER" \
        --ipc=host --restart unless-stopped \
        -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
        -p "127.0.0.1:${VLLM_PORT}:8000" \
        "$VLLM_IMAGE"

    sleep 2
    local status
    status="$(docker inspect -f '{{.State.Status}}' "$VLLM_CONTAINER" 2>/dev/null || echo "not found")"
    if [[ "$status" != "running" ]]; then
        echo "Container $VLLM_CONTAINER is not running (status: $status)" >&2
        docker logs --tail 30 "$VLLM_CONTAINER" 2>&1 || true
        exit 1
    fi
}

ensure_auth_artifacts() {
    mkdir -p keys
    chmod 700 keys

    local private="keys/private.pem"
    local public="keys/public.pem"
    local token="keys/token.txt"

    if [[ -f "$public" && ! -f "$private" ]]; then
        die "$public exists but $private is missing; refusing to run with an unmanageable key state"
    fi

    if [[ ! -f "$private" || ! -f "$public" || ! -f "$token" ]]; then
        echo "Generating or repairing JWT key/token artifacts..."
        uv run python -m scripts.generate_token --keys-dir keys --subject user
    else
        echo "JWT key/token artifacts already exist"
    fi

    [[ -f "$private" && -f "$public" && -f "$token" ]] \
        || die "JWT artifact generation did not produce private.pem, public.pem, and token.txt"

    [[ -f revoked_tokens.txt ]] || : > revoked_tokens.txt
    chmod 700 keys
    chmod 600 "$private" "$token" revoked_tokens.txt
    chmod 644 "$public"
}

build_proxy() {
    echo "Building Rust TLS proxy..."
    (cd rust_proxy && cargo build --release)
    [[ -x rust_proxy/target/release/vvv_proxy ]] \
        || die "cargo build completed but rust_proxy/target/release/vvv_proxy is missing"
}

ensure_user_linger() {
    local linger
    linger="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo "no")"
    if [[ "$linger" == "yes" ]]; then
        return
    fi
    echo "Enabling user lingering so services can start at boot..."
    loginctl enable-linger "$USER" 2>/dev/null \
        || die "Could not enable linger for $USER. Run: sudo loginctl enable-linger $USER"
}

write_server_unit() {
    mkdir -p "$USER_SYSTEMD_DIR"

    if [[ "$BACKEND" == "groq" ]]; then
        mkdir -p "$APP_CONFIG_DIR"
        chmod 700 "$APP_CONFIG_DIR"
        local old_umask
        old_umask="$(umask)"
        umask 077
        {
            echo "GROQ_API_KEY=$GROQ_API_KEY"
            echo "GROQ_MODEL_NAME=$GROQ_MODEL_NAME"
        } > "$APP_CONFIG_DIR/groq.env"
        umask "$old_umask"

        cat > "$USER_SYSTEMD_DIR/${SERVER_SERVICE}.service" <<SERVICEEOF
[Unit]
Description=VibeVoice ASR Server (Groq Whisper backend)
After=network-online.target default.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$APP_CONFIG_DIR/groq.env
ExecStart=$INSTALL_DIR/.venv/bin/python -m server \\
    --asr-backend groq \\
    --groq-api-key \${GROQ_API_KEY} \\
    --groq-model-name \${GROQ_MODEL_NAME} \\
    --host 127.0.0.1 \\
    --port $SERVER_PORT \\
    --max-audio-bytes $MAX_AUDIO_BYTES \\
    --max-queue-size $MAX_QUEUE_SIZE \\
    --jwt-public-key-file $INSTALL_DIR/keys/public.pem \\
    --revoked-tokens-file $INSTALL_DIR/revoked_tokens.txt \\
    --require-https true
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SERVICEEOF
    else
        cat > "$USER_SYSTEMD_DIR/${SERVER_SERVICE}.service" <<SERVICEEOF
[Unit]
Description=VibeVoice ASR Server
After=network-online.target default.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m server \\
    --asr-backend vibevoice \\
    --vllm-base-url http://127.0.0.1:$VLLM_PORT \\
    --host 127.0.0.1 \\
    --port $SERVER_PORT \\
    --max-audio-bytes $MAX_AUDIO_BYTES \\
    --max-queue-size $MAX_QUEUE_SIZE \\
    --jwt-public-key-file $INSTALL_DIR/keys/public.pem \\
    --revoked-tokens-file $INSTALL_DIR/revoked_tokens.txt \\
    --require-https true \\
    --vllm-model-name vibevoice \\
    --vllm-temperature 0.0 \\
    --vllm-top-p 1.0
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SERVICEEOF
    fi
}

write_proxy_unit() {
    mkdir -p "$USER_SYSTEMD_DIR"
    cat > "$USER_SYSTEMD_DIR/${PROXY_SERVICE}.service" <<SERVICEEOF
[Unit]
Description=VibeVoice TLS Reverse Proxy
After=network-online.target default.target ${SERVER_SERVICE}.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/rust_proxy
ExecStart=$INSTALL_DIR/rust_proxy/target/release/vvv_proxy \\
    --upstream-host 127.0.0.1 \\
    --upstream-port $SERVER_PORT \\
    --listen-host 0.0.0.0 \\
    --listen-port $PROXY_PORT \\
    --max-body-size $MAX_AUDIO_BYTES \\
    --cert-path $INSTALL_DIR/certs/self-signed/fullchain.pem \\
    --key-path $INSTALL_DIR/certs/self-signed/privkey.pem \\
    --cert-validity-days 3650 \\
    --cert-check-interval-secs 3600
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SERVICEEOF
}

install_services() {
    echo "Installing systemd user services..."
    ensure_user_linger
    write_server_unit
    write_proxy_unit
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVER_SERVICE"
    systemctl --user enable --now "$PROXY_SERVICE"

    sleep 2
    for svc in "$SERVER_SERVICE" "$PROXY_SERVICE"; do
        local status
        status="$(systemctl --user is-active "$svc" 2>/dev/null || echo "unknown")"
        if [[ "$status" != "active" ]]; then
            echo "$svc is not active (status: $status)" >&2
            journalctl --user -u "$svc" --no-pager -n 30 2>&1 || true
            exit 1
        fi
    done
}

wait_for_vllm() {
    echo "Waiting for vLLM to become healthy..."
    local tries=0
    local max_tries=36
    while (( tries < max_tries )); do
        if curl -fsS "http://127.0.0.1:${VLLM_PORT}/health" >/dev/null 2>&1; then
            return
        fi
        (( ++tries ))
        sleep 5
    done

    echo "vLLM did not become healthy within $((max_tries * 5)) seconds" >&2
    echo "Container status: $(docker inspect -f '{{.State.Status}}' "$VLLM_CONTAINER" 2>/dev/null || echo 'not found')" >&2
    docker logs --tail 30 "$VLLM_CONTAINER" 2>&1 || true
    exit 1
}

verify_full_stack() {
    local body_file
    body_file="$(mktemp)"
    local code
    code="$(curl -sk -o "$body_file" -w '%{http_code}' "https://127.0.0.1:${PROXY_PORT}/health" 2>/dev/null || echo "000")"
    local body
    body="$(cat "$body_file")"
    rm -f "$body_file"

    if [[ "$code" != "200" || ! "$body" =~ \"status\"[[:space:]]*:[[:space:]]*\"ok\" ]]; then
        echo "Full stack health check failed: HTTP $code $body" >&2
        if [[ "$BACKEND" == "vibevoice" ]]; then
            echo "vLLM direct:  $(curl -s "http://127.0.0.1:${VLLM_PORT}/health" 2>/dev/null || echo 'FAILED')" >&2
        fi
        echo "Server direct: $(curl -s "http://127.0.0.1:${SERVER_PORT}/health" 2>/dev/null || echo 'FAILED')" >&2
        echo "$SERVER_SERVICE status: $(systemctl --user is-active "$SERVER_SERVICE" 2>/dev/null || true)" >&2
        echo "$PROXY_SERVICE status:  $(systemctl --user is-active "$PROXY_SERVICE" 2>/dev/null || true)" >&2
        exit 1
    fi
}

echo "ASR backend: $BACKEND"
validate_environment

if [[ "$BACKEND" == "vibevoice" ]]; then
    verify_docker_gpu
    checkout_vibevoice
fi

stop_existing_services

if [[ "$BACKEND" == "vibevoice" ]]; then
    build_and_start_vllm
fi

echo "Installing Python dependencies..."
uv sync --no-dev
ensure_auth_artifacts
build_proxy
install_services

if [[ "$BACKEND" == "vibevoice" ]]; then
    wait_for_vllm
fi
verify_full_stack

echo ""
echo "Setup complete. All services healthy."
echo "  Backend: $BACKEND"
if [[ "$BACKEND" == "vibevoice" ]]; then
    echo "  vLLM:   http://127.0.0.1:$VLLM_PORT"
fi
echo "  Server: http://127.0.0.1:$SERVER_PORT"
echo "  Proxy:  https://127.0.0.1:$PROXY_PORT"
echo "  Token:  keys/token.txt"
