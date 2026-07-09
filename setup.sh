#!/usr/bin/env bash
set -euo pipefail

SERVER_SERVICE="vibevoice-server"
PROXY_SERVICE="vibevoice-proxy"
BACKEND_NETNS_CONTAINER="vibevoice-backend-netns"
SERVER_CONTAINER="vibevoice-server-container"
VLLM_CONTAINER="vibevoice-vllm"
VLLM_IMAGE="vibevoice-vllm:latest"
VIBEVOICE_COMMIT="1807b858d4f7dffdd286249a01616c243e488c9e"
VIBEVOICE_MODEL_REVISION="d0c9efdb8d614685062c04425d91e01b6f37d944"
VLLM_IMAGE_SECURITY_PROFILE="vvv-2026-07-09-no-vllm-local-media-ffmpeg-bounded-v3"

PROXY_PORT=42862
MAX_AUDIO_BYTES=524288000
MAX_MULTIPART_OVERHEAD_BYTES=1048576
MAX_REQUEST_BYTES=$((MAX_AUDIO_BYTES + MAX_MULTIPART_OVERHEAD_BYTES))
MAX_QUEUE_SIZE=50

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"
INSTALL_DIR="$SCRIPT_DIR"
USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
APP_CONFIG_DIR="$HOME/.config/vibevoice-vendor"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
BACKEND_SOCKET_DIR="/tmp/vibevoice-vendor-$HOST_UID"
BACKEND_SOCKET_HOST="$BACKEND_SOCKET_DIR/server.sock"
BACKEND_SOCKET_CONTAINER="/run/vibevoice/server.sock"
CERT_DIR="$INSTALL_DIR/certs/self-signed"
SERVER_SPKI_PIN="$CERT_DIR/server-spki-pin.txt"
CLIENT_CA_CERT="$CERT_DIR/client-ca.pem"
CLIENT_CERT="$INSTALL_DIR/keys/client-cert.pem"
CLIENT_KEY="$INSTALL_DIR/keys/client-key.pem"

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
        exit 1
    fi
}

validate_environment() {
    [[ -f "$INSTALL_DIR/Dockerfile" ]] \
        || die "Dockerfile not found in $INSTALL_DIR; run setup from the project checkout"
    [[ -d "$INSTALL_DIR/server" && -d "$INSTALL_DIR/rust_proxy" && -d "$INSTALL_DIR/deploy" ]] \
        || die "$INSTALL_DIR does not look like the vibe-voice-vendor project root"
    validate_simple_systemd_path "$INSTALL_DIR"
    validate_simple_systemd_path "$APP_CONFIG_DIR"
    validate_simple_systemd_path "$BACKEND_SOCKET_HOST"

    if [[ "$BACKEND" == "vibevoice" ]]; then
        for cmd in docker uv cargo git curl ffprobe; do
            require_cmd "$cmd"
        done
    else
        for cmd in uv cargo curl ffprobe ffmpeg; do
            require_cmd "$cmd"
        done
        validate_env_value "GROQ_API_KEY" "$GROQ_API_KEY"
        validate_env_value "GROQ_MODEL_NAME" "$GROQ_MODEL_NAME"
    fi
}

verify_docker_runtime() {
    docker info >/dev/null 2>&1 || {
        echo "Cannot connect to the Docker daemon as user $(id -un) (uid=$(id -u))" >&2
        echo "Groups: $(id -Gn)" >&2
        echo "This setup expects Docker access to already work for the current user." >&2
        exit 1
    }

    if ! docker info --format '{{.Runtimes}}' 2>/dev/null | grep -qw nvidia; then
        echo "Registered Docker runtimes: $(docker info --format '{{.Runtimes}}' 2>/dev/null || true)" >&2
        die "Docker does not have the 'nvidia' runtime registered"
    fi
}

verify_built_image_gpu() {
    echo "Verifying GPU passthrough inside pinned $VLLM_IMAGE with no Docker network..."

    local output
    if ! output="$(docker run --rm --pull=never --gpus all \
        --network none \
        --ipc=private \
        --user 65532:65532 \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --pids-limit 128 \
        --tmpfs /tmp:rw,exec,nosuid,nodev,size=512m \
        -e HOME=/tmp/gpu-smoke-home \
        -e USER=vibevoice \
        -e LOGNAME=vibevoice \
        -e XDG_CACHE_HOME=/tmp/gpu-smoke-cache \
        -e TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor-cache \
        -e TRITON_CACHE_DIR=/tmp/triton-cache \
        --entrypoint python3 \
        "$VLLM_IMAGE" \
        -c 'import torch; assert torch.cuda.is_available(), "CUDA is not available"; import vllm_plugin; vllm_plugin.register_vibevoice(); print(torch.cuda.get_device_name(0))' 2>&1)"; then
        echo "$output" >&2
        if [[ "$output" == *"failed to fulfill mount request"* || "$output" == *"failed to fulfil mount request"* || "$output" == *"libnvidia"* ]]; then
            echo "" >&2
            echo "The NVIDIA Docker runtime is registered but its host mount spec appears broken." >&2
            echo "Fix the host Docker/NVIDIA runtime configuration, then rerun setup." >&2
        fi
        exit 1
    fi
}

compute_vllm_image_source_hash() {
    {
        printf 'VIBEVOICE_COMMIT=%s\n' "$VIBEVOICE_COMMIT"
        printf 'VIBEVOICE_MODEL_REVISION=%s\n' "$VIBEVOICE_MODEL_REVISION"
        printf 'VLLM_IMAGE_SECURITY_PROFILE=%s\n' "$VLLM_IMAGE_SECURITY_PROFILE"
        sha256sum Dockerfile pyproject.toml uv.lock
        find server -type f -print0 | sort -z | xargs -0 sha256sum
        find VibeVoice -type f \
            ! -path 'VibeVoice/.git/*' \
            ! -path 'VibeVoice/demo/*' \
            ! -path 'VibeVoice/docs/*' \
            ! -path 'VibeVoice/Figures/*' \
            ! -path 'VibeVoice/finetuning-asr/*' \
            -print0 | sort -z | xargs -0 sha256sum
    } | sha256sum | awk '{print $1}'
}

vllm_image_matches_current_sources() {
    local expected_hash="$1"
    local actual_hash
    local actual_profile
    local image_cmd

    docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1 || return 1
    actual_hash="$(docker image inspect -f '{{ index .Config.Labels "org.vvv.source-sha256" }}' "$VLLM_IMAGE")"
    actual_profile="$(docker image inspect -f '{{ index .Config.Labels "org.vvv.security-profile" }}' "$VLLM_IMAGE")"
    image_cmd="$(docker image inspect -f '{{json .Config.Cmd}}' "$VLLM_IMAGE")"

    [[ "$actual_hash" == "$expected_hash" ]] || return 1
    [[ "$actual_profile" == "$VLLM_IMAGE_SECURITY_PROFILE" ]] || return 1
    [[ "$image_cmd" != *"--allowed-local-media-path"* ]] || return 1
    [[ "$image_cmd" == *'"--host","127.0.0.1"'* ]] || return 1
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
        for container in "$SERVER_CONTAINER" "$VLLM_CONTAINER" "$BACKEND_NETNS_CONTAINER"; do
            if docker container inspect "$container" >/dev/null 2>&1; then
                echo "Removing existing $container container..."
                docker stop "$container" >/dev/null 2>&1 || true
                docker rm "$container"
            fi
        done
    fi
    rm -f "$BACKEND_SOCKET_HOST"
}

ensure_private_dir() {
    local dir="$1"
    if [[ -e "$dir" ]]; then
        [[ -d "$dir" && ! -L "$dir" ]] \
            || die "$dir must be a real directory"
        [[ "$(stat -c '%u:%g' "$dir")" == "$HOST_UID:$HOST_GID" ]] \
            || die "$dir is not owned by $HOST_UID:$HOST_GID"
        [[ "$(stat -c '%a' "$dir")" == "700" ]] \
            || die "$dir must have mode 700"
    else
        mkdir -p "$dir"
        chmod 700 "$dir"
        [[ "$(stat -c '%u:%g' "$dir")" == "$HOST_UID:$HOST_GID" ]] \
            || die "$dir is not owned by $HOST_UID:$HOST_GID"
        [[ "$(stat -c '%a' "$dir")" == "700" ]] \
            || die "$dir must have mode 700"
    fi
}

prepare_backend_socket_dir() {
    ensure_private_dir "$APP_CONFIG_DIR"
    ensure_private_dir "$BACKEND_SOCKET_DIR"
    rm -f "$BACKEND_SOCKET_HOST"
}

prepare_cert_dir() {
    ensure_private_dir "$CERT_DIR"
}

build_and_start_vibevoice_backend() {
    local image_source_hash
    image_source_hash="$(compute_vllm_image_source_hash)"

    if $FORCE_REBUILD || ! vllm_image_matches_current_sources "$image_source_hash"; then
        echo "Building $VLLM_IMAGE (downloads pinned ~14 GB model snapshot on first build)..."
        docker build \
            --build-arg "VIBEVOICE_MODEL_REVISION=$VIBEVOICE_MODEL_REVISION" \
            --label "org.vvv.source-sha256=$image_source_hash" \
            --label "org.vvv.security-profile=$VLLM_IMAGE_SECURITY_PROFILE" \
            -t "$VLLM_IMAGE" .
    else
        echo "Docker image $VLLM_IMAGE matches current pinned sources, skipping build"
    fi

    docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1 \
        || die "docker build completed but image $VLLM_IMAGE is not present"
    vllm_image_matches_current_sources "$image_source_hash" \
        || die "docker image $VLLM_IMAGE does not match current pinned sources/security profile"
    verify_built_image_gpu

    prepare_backend_socket_dir

    echo "Starting $BACKEND_NETNS_CONTAINER container with no Docker network..."
    docker run -d --pull=never --name "$BACKEND_NETNS_CONTAINER" \
        --network none \
        --restart unless-stopped \
        --user 65532:65532 \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --pids-limit 64 \
        --entrypoint python3 \
        "$VLLM_IMAGE" \
        -c 'import time; time.sleep(10**9)'

    echo "Starting $VLLM_CONTAINER inside the no-network backend namespace..."
    docker run -d --pull=never --gpus all --name "$VLLM_CONTAINER" \
        --network "container:$BACKEND_NETNS_CONTAINER" \
        --ipc=private \
        --shm-size 16g \
        --restart unless-stopped \
        --user 65532:65532 \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --read-only \
        --pids-limit 2048 \
        --tmpfs /tmp:rw,exec,nosuid,nodev,size=4g \
        --tmpfs /var/tmp:rw,nosuid,nodev,size=1g \
        -e HOME=/tmp/vllm-home \
        -e USER=vibevoice \
        -e LOGNAME=vibevoice \
        -e XDG_CACHE_HOME=/tmp/vllm-cache \
        -e TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor-cache \
        -e TRITON_CACHE_DIR=/tmp/triton-cache \
        -e PYTHONDONTWRITEBYTECODE=1 \
        -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
        "$VLLM_IMAGE"

    echo "Starting $SERVER_CONTAINER with UDS-only host access..."
    docker run -d --pull=never --name "$SERVER_CONTAINER" \
        --network "container:$BACKEND_NETNS_CONTAINER" \
        --restart unless-stopped \
        --user "$HOST_UID:$HOST_GID" \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --read-only \
        --pids-limit 512 \
        --tmpfs /tmp:rw,nosuid,nodev,size=2g \
        --tmpfs /var/tmp:rw,nosuid,nodev,size=1g \
        -e HOME=/tmp/vvv-server-home \
        -e USER=vvv-server \
        -e LOGNAME=vvv-server \
        -e XDG_CACHE_HOME=/tmp/vvv-server-cache \
        -e PYTHONDONTWRITEBYTECODE=1 \
        -e PYTHONPATH=/opt/vvv-server \
        -v "$BACKEND_SOCKET_DIR:/run/vibevoice" \
        -v "$INSTALL_DIR/keys/public.pem:/run/vibevoice-auth/public.pem:ro" \
        -v "$INSTALL_DIR/revoked_tokens.txt:/run/vibevoice-auth/revoked_tokens.txt:ro" \
        --entrypoint /opt/vvv-server-venv/bin/python \
        "$VLLM_IMAGE" \
        -m server \
        --asr-backend vibevoice \
        --vllm-base-url http://127.0.0.1:8000 \
        --uds "$BACKEND_SOCKET_CONTAINER" \
        --max-audio-bytes "$MAX_AUDIO_BYTES" \
        --max-queue-size "$MAX_QUEUE_SIZE" \
        --jwt-public-key-file /run/vibevoice-auth/public.pem \
        --revoked-tokens-file /run/vibevoice-auth/revoked_tokens.txt \
        --require-https true \
        --vllm-model-name vibevoice \
        --vllm-temperature 0.0 \
        --vllm-top-p 1.0

    sleep 2
    for container in "$BACKEND_NETNS_CONTAINER" "$VLLM_CONTAINER" "$SERVER_CONTAINER"; do
        local status
        status="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "not found")"
        if [[ "$status" != "running" ]]; then
            echo "Container $container is not running (status: $status)" >&2
            docker logs --tail 30 "$container" 2>&1 || true
            exit 1
        fi
    done
}

ensure_auth_artifacts() {
    local private="keys/private.pem"
    local public="keys/public.pem"
    local token="keys/token.txt"
    local jwt_existing=0

    [[ -e "$private" ]] && (( ++jwt_existing ))
    [[ -e "$public" ]] && (( ++jwt_existing ))
    [[ -e "$token" ]] && (( ++jwt_existing ))
    if (( jwt_existing == 0 )); then
        echo "Generating JWT key/token artifacts from a clean state..."
        uv run python -m scripts.generate_token --keys-dir keys --subject user
    elif (( jwt_existing == 3 )); then
        echo "Validating existing JWT key/token artifacts..."
        uv run python -m scripts.validate_auth_artifacts --keys-dir keys
    else
        die "Partial JWT artifact state exists; expected all or none of $private, $public, and $token"
    fi

    if [[ ! -e revoked_tokens.txt ]]; then
        local old_umask
        old_umask="$(umask)"
        umask 077
        : > revoked_tokens.txt
        umask "$old_umask"
    fi
    [[ -f revoked_tokens.txt && ! -L revoked_tokens.txt ]] \
        || die "revoked_tokens.txt must be a regular file"
    [[ "$(stat -c '%a' revoked_tokens.txt)" == "600" ]] \
        || die "revoked_tokens.txt must have mode 600"

    local client_existing=0
    [[ -e "$CLIENT_CA_CERT" ]] && (( ++client_existing ))
    [[ -e "$CLIENT_CERT" ]] && (( ++client_existing ))
    [[ -e "$CLIENT_KEY" ]] && (( ++client_existing ))
    if (( client_existing == 0 )); then
        echo "Generating mTLS client-auth artifacts from a clean state..."
        uv run python -m scripts.generate_client_cert \
            --certs-dir "$CERT_DIR" \
            --keys-dir keys \
            --subject user \
            --days 3650
    elif (( client_existing == 3 )); then
        echo "Validating existing mTLS client-auth artifacts..."
        uv run python -m scripts.validate_client_cert \
            --certs-dir "$CERT_DIR" \
            --keys-dir keys
    else
        die "Partial mTLS client-auth artifact state exists; expected all or none of $CLIENT_CA_CERT, $CLIENT_CERT, and $CLIENT_KEY"
    fi
}

read_server_pin_for_curl() {
    [[ -f "$SERVER_SPKI_PIN" && ! -L "$SERVER_SPKI_PIN" ]] \
        || die "$SERVER_SPKI_PIN does not exist; the proxy did not export its server identity pin"
    local pin
    pin="$(tr -d '\r\n' < "$SERVER_SPKI_PIN")"
    [[ "$pin" =~ ^sha256/[A-Za-z0-9+/]+=*$ ]] \
        || die "$SERVER_SPKI_PIN must contain a sha256/<base64> SPKI pin"
    printf 'sha256//%s\n' "${pin#sha256/}"
}

build_proxy() {
    echo "Building Rust TLS proxy..."
    (cd rust_proxy && cargo build --release)
    [[ -x rust_proxy/target/release/vvv_proxy ]] \
        || die "cargo build completed but rust_proxy/target/release/vvv_proxy is missing"
}

write_server_unit() {
    mkdir -p "$USER_SYSTEMD_DIR"

    [[ "$BACKEND" == "groq" ]] \
        || die "write_server_unit is only valid for the groq backend"

    ensure_private_dir "$APP_CONFIG_DIR"
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
    --uds $BACKEND_SOCKET_HOST \\
    --max-audio-bytes $MAX_AUDIO_BYTES \\
    --max-queue-size $MAX_QUEUE_SIZE \\
    --jwt-public-key-file $INSTALL_DIR/keys/public.pem \\
    --revoked-tokens-file $INSTALL_DIR/revoked_tokens.txt \\
    --require-https true
Restart=always
RestartSec=5
UMask=077
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_UNIX
RestrictSUIDSGID=true
LockPersonality=true
RestrictNamespaces=true
RestrictRealtime=true
SystemCallArchitectures=native
LimitNOFILE=1024
TasksMax=512

[Install]
WantedBy=default.target
SERVICEEOF
}

write_proxy_unit() {
    mkdir -p "$USER_SYSTEMD_DIR"
    if [[ "$BACKEND" == "vibevoice" ]]; then
        cat > "$USER_SYSTEMD_DIR/${PROXY_SERVICE}.service" <<SERVICEEOF
[Unit]
Description=VibeVoice TLS Reverse Proxy
After=network-online.target default.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/rust_proxy
ExecStart=$INSTALL_DIR/rust_proxy/target/release/vvv_proxy \\
    --upstream-uds $BACKEND_SOCKET_HOST \\
    --upstream-peer-uid $HOST_UID \\
    --upstream-peer-gid $HOST_GID \\
    --listen-host 0.0.0.0 \\
    --listen-port $PROXY_PORT \\
    --max-body-size $MAX_REQUEST_BYTES \\
    --jwt-public-key-file $INSTALL_DIR/keys/public.pem \\
    --revoked-tokens-file $INSTALL_DIR/revoked_tokens.txt \\
    --cert-path $CERT_DIR/fullchain.pem \\
    --key-path $CERT_DIR/privkey.pem \\
    --server-spki-pin-path $SERVER_SPKI_PIN \\
    --client-ca-cert-path $CLIENT_CA_CERT \\
    --cert-validity-days 3650 \\
    --cert-check-interval-secs 3600
Restart=always
RestartSec=5
UMask=077
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_UNIX
RestrictSUIDSGID=true
LockPersonality=true
RestrictNamespaces=true
RestrictRealtime=true
SystemCallArchitectures=native
LimitNOFILE=512
TasksMax=256

[Install]
WantedBy=default.target
SERVICEEOF
    else
        cat > "$USER_SYSTEMD_DIR/${PROXY_SERVICE}.service" <<SERVICEEOF
[Unit]
Description=VibeVoice TLS Reverse Proxy
After=network-online.target default.target ${SERVER_SERVICE}.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/rust_proxy
ExecStart=$INSTALL_DIR/rust_proxy/target/release/vvv_proxy \\
    --upstream-uds $BACKEND_SOCKET_HOST \\
    --upstream-peer-uid $HOST_UID \\
    --upstream-peer-gid $HOST_GID \\
    --listen-host 0.0.0.0 \\
    --listen-port $PROXY_PORT \\
    --max-body-size $MAX_REQUEST_BYTES \\
    --jwt-public-key-file $INSTALL_DIR/keys/public.pem \\
    --revoked-tokens-file $INSTALL_DIR/revoked_tokens.txt \\
    --cert-path $CERT_DIR/fullchain.pem \\
    --key-path $CERT_DIR/privkey.pem \\
    --server-spki-pin-path $SERVER_SPKI_PIN \\
    --client-ca-cert-path $CLIENT_CA_CERT \\
    --cert-validity-days 3650 \\
    --cert-check-interval-secs 3600
Restart=always
RestartSec=5
UMask=077
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_UNIX
RestrictSUIDSGID=true
LockPersonality=true
RestrictNamespaces=true
RestrictRealtime=true
SystemCallArchitectures=native
LimitNOFILE=512
TasksMax=256

[Install]
WantedBy=default.target
SERVICEEOF
    fi
}

install_services() {
    echo "Installing systemd user services..."
    if [[ "$BACKEND" == "groq" ]]; then
        write_server_unit
    else
        rm -f "$USER_SYSTEMD_DIR/${SERVER_SERVICE}.service"
        systemctl --user disable --now "$SERVER_SERVICE" 2>/dev/null || true
    fi
    write_proxy_unit
    systemctl --user daemon-reload
    if [[ "$BACKEND" == "groq" ]]; then
        systemctl --user enable --now "$SERVER_SERVICE"
        wait_for_backend_socket
    fi
    systemctl --user enable --now "$PROXY_SERVICE"

    sleep 2
    local services=("$PROXY_SERVICE")
    if [[ "$BACKEND" == "groq" ]]; then
        services=("$SERVER_SERVICE" "$PROXY_SERVICE")
    fi
    for svc in "${services[@]}"; do
        local status
        status="$(systemctl --user is-active "$svc" 2>/dev/null || echo "unknown")"
        if [[ "$status" != "active" ]]; then
            echo "$svc is not active (status: $status)" >&2
            journalctl --user -u "$svc" --no-pager -n 30 2>&1 || true
            exit 1
        fi
    done
}

wait_for_backend_socket() {
    echo "Waiting for UDS backend to become healthy..."
    local tries=0
    local max_tries=72
    while (( tries < max_tries )); do
        if backend_socket_is_private \
            && curl --unix-socket "$BACKEND_SOCKET_HOST" -fsS "http://vvv/health" >/dev/null 2>&1; then
            return
        fi
        (( ++tries ))
        sleep 5
    done

    echo "UDS backend did not become healthy within $((max_tries * 5)) seconds" >&2
    print_backend_debug
    exit 1
}

backend_socket_is_private() {
    [[ -S "$BACKEND_SOCKET_HOST" && ! -L "$BACKEND_SOCKET_HOST" ]] || return 1
    [[ "$(stat -c '%u:%g' "$BACKEND_SOCKET_HOST")" == "$HOST_UID:$HOST_GID" ]] || return 1
    [[ "$(stat -c '%a' "$BACKEND_SOCKET_HOST")" == "600" ]] || return 1
}

backend_health_check() {
    local body_file="$1"
    if ! backend_socket_is_private; then
        echo "000"
        return
    fi
    curl --unix-socket "$BACKEND_SOCKET_HOST" -s -o "$body_file" -w '%{http_code}' \
        "http://vvv/health" 2>/dev/null || echo "000"
}

print_backend_debug() {
    if [[ "$BACKEND" == "vibevoice" ]]; then
        for container in "$BACKEND_NETNS_CONTAINER" "$VLLM_CONTAINER" "$SERVER_CONTAINER"; do
            echo "$container status: $(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo 'not found')" >&2
            docker logs --tail 20 "$container" 2>&1 || true
        done
    else
        echo "$SERVER_SERVICE status: $(systemctl --user is-active "$SERVER_SERVICE" 2>/dev/null || true)" >&2
        echo "Server direct: $(curl --unix-socket "$BACKEND_SOCKET_HOST" -s "http://vvv/health" 2>/dev/null || echo 'FAILED')" >&2
    fi
}

verify_full_stack() {
    local body_file
    body_file="$(mktemp)"
    local tls_config
    tls_config="$(mktemp)"
    chmod 600 "$tls_config"
    local auth_config
    auth_config="$(mktemp)"
    chmod 600 "$auth_config"
    local curl_pin
    curl_pin="$(read_server_pin_for_curl)"
    local token
    token="$(tr -d '\r\n' < keys/token.txt)"
    [[ -n "$token" ]] || die "keys/token.txt is empty"
    printf 'insecure\npinnedpubkey = "%s"\ncert = "%s"\nkey = "%s"\n' \
        "$curl_pin" "$CLIENT_CERT" "$CLIENT_KEY" > "$tls_config"
    printf 'header = "Authorization: Bearer %s"\n' "$token" > "$auth_config"

    local code
    code="$(curl --config "$tls_config" --config "$auth_config" -s -o "$body_file" -w '%{http_code}' "https://127.0.0.1:${PROXY_PORT}/health" 2>/dev/null || echo "000")"
    local body
    body="$(cat "$body_file")"

    if [[ "$code" != "200" || ! "$body" =~ \"proxy\"[[:space:]]*:[[:space:]]*\"ok\" ]]; then
        echo "Proxy health check failed: HTTP $code $body" >&2
        rm -f "$body_file" "$tls_config" "$auth_config"
        print_backend_debug
        echo "$PROXY_SERVICE status:  $(systemctl --user is-active "$PROXY_SERVICE" 2>/dev/null || true)" >&2
        exit 1
    fi

    code="$(curl --config "$tls_config" --config "$auth_config" -s -o "$body_file" -w '%{http_code}' "https://127.0.0.1:${PROXY_PORT}/v1/queue/status" 2>/dev/null || echo "000")"
    body="$(cat "$body_file")"
    if [[ "$code" != "200" || ! "$body" =~ \"your_jobs\" ]]; then
        echo "Authenticated proxy check failed: HTTP $code $body" >&2
        rm -f "$body_file" "$tls_config" "$auth_config"
        print_backend_debug
        echo "$PROXY_SERVICE status:  $(systemctl --user is-active "$PROXY_SERVICE" 2>/dev/null || true)" >&2
        exit 1
    fi

    code="$(backend_health_check "$body_file")"
    body="$(cat "$body_file")"
    rm -f "$body_file" "$tls_config" "$auth_config"

    if [[ "$code" != "200" || ! "$body" =~ \"status\"[[:space:]]*:[[:space:]]*\"ok\" ]]; then
        echo "Server/backend health check failed: HTTP $code $body" >&2
        print_backend_debug
        echo "$PROXY_SERVICE status:  $(systemctl --user is-active "$PROXY_SERVICE" 2>/dev/null || true)" >&2
        exit 1
    fi
}

echo "ASR backend: $BACKEND"
validate_environment

if [[ "$BACKEND" == "vibevoice" ]]; then
    verify_docker_runtime
    checkout_vibevoice
fi

stop_existing_services

echo "Installing Python dependencies..."
uv sync --no-dev
ensure_auth_artifacts
prepare_cert_dir
build_proxy

if [[ "$BACKEND" == "vibevoice" ]]; then
    build_and_start_vibevoice_backend
    wait_for_backend_socket
else
    prepare_backend_socket_dir
fi

install_services

verify_full_stack

echo ""
echo "Setup complete. All services healthy."
echo "  Backend: $BACKEND"
if [[ "$BACKEND" == "vibevoice" ]]; then
    echo "  vLLM:   no host TCP listener; loopback-only inside $BACKEND_NETNS_CONTAINER"
    echo "  Server: unix://$BACKEND_SOCKET_HOST"
else
    echo "  Server: unix://$BACKEND_SOCKET_HOST"
fi
echo "  Proxy:  https://127.0.0.1:$PROXY_PORT"
echo "  Android Server URL: https://HOST:$PROXY_PORT"
echo "  Android Server public key pin: $(tr -d '\r\n' < "$SERVER_SPKI_PIN")"
echo "  Token:  keys/token.txt"
echo "  Client cert/key: keys/client-cert.pem keys/client-key.pem"
