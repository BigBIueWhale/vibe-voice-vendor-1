# VibeVoice ASR Server

Secure, queue-based ASR server wrapping Microsoft's [VibeVoice-ASR-7B](https://github.com/microsoft/VibeVoice) model. Single-request processing via an async queue, SSE streaming, zero data storage, TLS encryption, bearer token auth.

## Architecture

```
Internet (HTTPS :443) -> Caddy (auto-TLS) -> FastAPI (:8080 localhost) -> vLLM (:8000 localhost)
```

## Quick Start (Development)

```bash
# Install dependencies
uv sync

# Generate a token
uv run python -m scripts.generate_token

# Set environment variables
export VVV_TOKEN_HASHES_ENV='<hash from above>'
export VVV_VLLM_BASE_URL='http://localhost:8000'

# Start the server
uv run python -m server
```

## Deployment (Ubuntu 24.04 + RTX 5090)

### 1. Start vLLM in Docker

```bash
docker run --gpus all -p 8000:8000 \
  --name vibevoice-vllm \
  --restart unless-stopped \
  vllm/vllm-openai:latest \
  --model microsoft/VibeVoice-ASR-7B \
  --tensor-parallel-size 1 \
  --max-model-len 32768 \
  --trust-remote-code
```

### 2. Install the ASR server

```bash
sudo useradd -r -s /bin/false vibevoice
sudo mkdir -p /opt/vibe-voice-vendor
sudo chown vibevoice:vibevoice /opt/vibe-voice-vendor

cd /opt/vibe-voice-vendor
git clone <repo-url> .
uv sync --no-dev

# Generate tokens
uv run python -m scripts.generate_token
# Copy the hash to .env

cp deploy/env.example .env
# Edit .env with your values
```

### 3. Install Caddy

```bash
sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
# Edit /etc/caddy/Caddyfile: set your domain and email
sudo systemctl enable --now caddy
```

### 4. Start the server via systemd

```bash
sudo cp deploy/vibevoice-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vibevoice-server
```

### 5. Open firewall

```bash
sudo ufw allow 443/tcp
sudo ufw allow 80/tcp  # For ACME challenge
```

## Client Usage

### CLI

```bash
# Transcribe a file
vvv --server https://asr.example.com --token YOUR_TOKEN transcribe recording.mp3

# With hotwords
vvv --server https://asr.example.com --token YOUR_TOKEN transcribe recording.mp3 --hotwords "VibeVoice,ASR"

# Save to file
vvv --server https://asr.example.com --token YOUR_TOKEN transcribe recording.mp3 --output transcript.txt

# Check queue status
vvv --server https://asr.example.com --token YOUR_TOKEN status
```

### Python Library

```python
import asyncio
from client.client import VibevoiceClient
from client.models import EventType

async def main():
    client = VibevoiceClient(
        base_url="https://asr.example.com",
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
# Generate a new token
uv run python -m scripts.generate_token

# Add the hash to VVV_TOKEN_HASHES_ENV (comma-separated for multiple tokens)
# Restart the server to pick up new tokens
```
