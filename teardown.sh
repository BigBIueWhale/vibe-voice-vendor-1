#!/usr/bin/env bash
set -euo pipefail

echo "Stopping services..."
systemctl --user disable --now vibevoice-proxy 2>/dev/null || true
systemctl --user disable --now vibevoice-server 2>/dev/null || true

for container in vibevoice-server-container vibevoice-vllm vibevoice-backend-netns; do
    if docker container inspect "$container" &>/dev/null; then
        echo "Stopping $container container..."
        docker stop "$container"
        docker rm "$container"
    fi
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
rm -f "$script_dir/run/server.sock"
rmdir "$script_dir/run" 2>/dev/null || true
runtime_dir="/tmp/vibevoice-vendor-$(id -u)"
rm -f "$runtime_dir/server.sock"
rmdir "$runtime_dir" 2>/dev/null || true
rm -f "$HOME/.config/vibevoice-vendor/run/server.sock"

echo "All services stopped. Run ./setup.sh to start again."
