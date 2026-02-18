# VibeVoice ASR Server

Secure, queue-based ASR server wrapping Microsoft's [VibeVoice-ASR-7B](https://github.com/microsoft/VibeVoice) model. Single-request processing via an async queue, SSE streaming, zero data storage, TLS encryption, JWT bearer auth (ES256).

## Architecture

```
Internet (HTTPS :42862) -> vvv_proxy (self-signed TLS) -> FastAPI (:54912 127.0.0.1) -> vLLM (:37845 127.0.0.1)
```

## Quick Start (Development)

```bash
# Install dependencies
uv sync

# Generate a token
uv run python -m scripts.generate_token

# Set environment variables
export VVV_JWT_PUBLIC_KEY_FILE='keys/public.pem'
export VVV_VLLM_BASE_URL='http://localhost:37845'

# Start the server
uv run python -m server
```

## Deployment (Ubuntu 24.04 + RTX 5090)

### 1. Start vLLM in Docker

Following [Microsoft's official instructions](https://github.com/microsoft/VibeVoice/blob/main/docs/vibevoice-vllm-asr.md):

```bash
git clone https://github.com/microsoft/VibeVoice.git
cd VibeVoice

docker run -d --gpus all --name vibevoice-vllm \
  --ipc=host \
  --restart unless-stopped \
  -p 127.0.0.1:37845:8000 \
  -e VIBEVOICE_FFMPEG_MAX_CONCURRENCY=64 \
  -e PYTORCH_ALLOC_CONF=expandable_segments:True \
  -v $(pwd):/app \
  -w /app \
  --entrypoint bash \
  vllm/vllm-openai:v0.15.1 \
  -c "python3 /app/vllm_plugin/scripts/start_server.py"

# Watch startup progress (model download + tokenizer generation)
docker logs -f vibevoice-vllm
```

### 2. Install the ASR server

```bash
sudo mkdir -p /opt/vibe-voice-vendor

cd /opt/vibe-voice-vendor
git clone <repo-url> .
uv sync --no-dev

# Generate key pair and token
uv run python -m scripts.generate_token
# Note the public key path for .env

cp deploy/env.example .env
# Edit .env with your values (set VVV_JWT_PUBLIC_KEY_FILE)
```

### 3. Build the TLS reverse proxy

No global installs required. The proxy generates self-signed certificates automatically on first run.

```bash
cd /opt/vibe-voice-vendor/rust_proxy
cargo build --release
# Binary is at: target/release/vvv_proxy
```

### 4. Start the server via systemd

```bash
sudo cp deploy/vibevoice-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vibevoice-server
```

### 5. Start the TLS proxy

```bash
cd /opt/vibe-voice-vendor/rust_proxy
./target/release/vvv_proxy
# Listens on https://0.0.0.0:42862, proxies to http://127.0.0.1:54912
# Self-signed cert auto-generated at certs/self-signed/
```

## Client Installation

The `vvv` CLI is included in the project. Install it on any client machine with [uv](https://docs.astral.sh/uv/):

```bash
# Clone the repo
git clone <repo-url>
cd vibe-voice-vendor

# Install (creates the vvv command)
uv sync --no-dev

# Verify it works
uv run vvv --help
```

If you want `vvv` available globally without the `uv run` prefix, install the package into an isolated tool environment:

```bash
uv tool install .
vvv --help
```

## Client Usage

### CLI

```bash
# Transcribe a file
vvv --server https://your-server:42862 --token YOUR_TOKEN transcribe recording.mp3

# With hotwords
vvv --server https://your-server:42862 --token YOUR_TOKEN transcribe recording.mp3 --hotwords "VibeVoice,ASR"

# Save to file
vvv --server https://your-server:42862 --token YOUR_TOKEN transcribe recording.mp3 --output transcript.txt

# Check queue status
vvv --server https://your-server:42862 --token YOUR_TOKEN status
```

### Python Library

```python
import asyncio
from client.client import VibevoiceClient
from client.models import EventType

async def main():
    client = VibevoiceClient(
        base_url="https://your-server:42862",
        token="YOUR_TOKEN",
    )

    async for event in client.transcribe("recording.mp3"):
        if event.event_type == EventType.QUEUE:
            print(f"Queue position: {event.position}")
        elif event.event_type == EventType.DATA:
            print(event.text, end="")
        elif event.event_type == EventType.DONE:
            print("\nDone!")

asyncio.run(main())
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/v1/transcribe` | Yes | Upload audio + stream transcription via SSE |
| GET | `/v1/queue/status` | Yes | Get your queue position and job status |
| GET | `/health` | No | Server + vLLM health check |

## Configuration

All configuration via environment variables with `VVV_` prefix. See `deploy/env.example` for the full list.

## Token Management

```bash
# Generate a key pair and token (first run creates keys/ directory)
uv run python -m scripts.generate_token

# Generate a token for a specific user
uv run python -m scripts.generate_token --subject alice

# Point the server at the public key
export VVV_JWT_PUBLIC_KEY_FILE=keys/public.pem

# To revoke a token, decode its JTI and add it to a revocation file:
# python -c "import jwt; print(jwt.decode('TOKEN', options={'verify_signature': False})['jti'])"
# echo "JTI_VALUE" >> revoked.txt
# export VVV_REVOKED_TOKENS_FILE=revoked.txt
```
