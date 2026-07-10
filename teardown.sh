#!/usr/bin/env bash
set -euo pipefail

SERVER_SERVICE="vibevoice-server"
PROXY_SERVICE="vibevoice-proxy"
BACKEND_NETNS_CONTAINER="vibevoice-backend-netns"
SERVER_CONTAINER="vibevoice-server-container"
VLLM_CONTAINER="vibevoice-vllm"
VLLM_REPOSITORY="vibevoice-vllm"
VLLM_IMAGE="vibevoice-vllm:latest"

USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
APP_CONFIG_DIR="$HOME/.config/vibevoice-vendor"
BACKEND_SOCKET_DIR="/tmp/vibevoice-vendor-$(id -u)"
BACKEND_TMP_DIR="$BACKEND_SOCKET_DIR/tmp"

remove_vibevoice_vllm_images() {
    local image_ids=()
    mapfile -t image_ids < <(
        {
            docker image ls --all --quiet "$VLLM_IMAGE" 2>/dev/null || true
            docker image ls --all --quiet --filter "reference=${VLLM_REPOSITORY}:*" 2>/dev/null || true
            docker image ls --all --quiet --filter "label=org.vvv.source-sha256" 2>/dev/null || true
            docker image ls --all --quiet --filter "label=org.vvv.security-profile" 2>/dev/null || true
        } | awk 'NF && !seen[$0]++'
    )

    if (( ${#image_ids[@]} == 0 )); then
        return
    fi

    echo "Removing local VibeVoice Docker images..."
    docker image rm --force "${image_ids[@]}"
}

echo "Stopping installed user services..."
systemctl --user disable --now "$PROXY_SERVICE" 2>/dev/null || true
systemctl --user disable --now "$SERVER_SERVICE" 2>/dev/null || true

echo "Removing installed user service units..."
rm -f \
    "$USER_SYSTEMD_DIR/${PROXY_SERVICE}.service" \
    "$USER_SYSTEMD_DIR/${SERVER_SERVICE}.service"
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed "$PROXY_SERVICE" "$SERVER_SERVICE" 2>/dev/null || true

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    for container in "$SERVER_CONTAINER" "$VLLM_CONTAINER" "$BACKEND_NETNS_CONTAINER"; do
        if docker container inspect "$container" >/dev/null 2>&1; then
            echo "Removing $container container..."
            docker rm -f "$container"
        fi
    done

    remove_vibevoice_vllm_images
else
    echo "Docker is unavailable; skipped Docker container and image removal."
fi

echo "Removing runtime sockets and backend configuration..."
rm -f "$BACKEND_SOCKET_DIR/server.sock"
if [[ "$BACKEND_TMP_DIR" == /tmp/vibevoice-vendor-$(id -u)/tmp && -d "$BACKEND_TMP_DIR" && ! -L "$BACKEND_TMP_DIR" ]]; then
    rm -rf -- "$BACKEND_TMP_DIR"
fi
rmdir "$BACKEND_SOCKET_DIR" 2>/dev/null || true
rm -f "$APP_CONFIG_DIR/run/server.sock"
rmdir "$APP_CONFIG_DIR/run" 2>/dev/null || true
rm -f "$APP_CONFIG_DIR/groq.env"
rmdir "$APP_CONFIG_DIR" 2>/dev/null || true

echo "Teardown complete. Installed user services, runtime containers, runtime sockets, Groq env, and local VibeVoice Docker images were removed."
