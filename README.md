# VibeVoice ASR Server

Secure, queue-based ASR server wrapping Microsoft's [VibeVoice-ASR-7B](https://github.com/microsoft/VibeVoice) model. Single-request processing via an async queue, SSE streaming, zero data storage, TLS encryption, JWT bearer auth (ES256).

## Architecture

```
Internet (HTTPS :42862) -> vvv_proxy (self-signed TLS) -> FastAPI (:54912 127.0.0.1) -> vLLM (:37845 127.0.0.1)
```

## Setup

All steps run from `~/Desktop/vibe-voice-vendor` on the `rtx5090` machine as user `user`.

### 1. Clone VibeVoice and start vLLM in Docker

Following [Microsoft's official instructions](https://github.com/microsoft/VibeVoice/blob/main/docs/vibevoice-vllm-asr.md):

```bash
cd ~/Desktop/vibe-voice-vendor
git clone https://github.com/microsoft/VibeVoice.git --recurse-submodules
git -C VibeVoice checkout 1807b858

docker run -d --gpus all --name vibevoice-vllm \
  --ipc=host \
  --restart unless-stopped \
  -p 127.0.0.1:37845:8000 \
  -e VIBEVOICE_FFMPEG_MAX_CONCURRENCY=64 \
  -e PYTORCH_ALLOC_CONF=expandable_segments:True \
  -v ~/Desktop/vibe-voice-vendor/VibeVoice:/app \
  -w /app \
  --entrypoint bash \
  vllm/vllm-openai:v0.14.1 \
  -c '\
    apt-get update && apt-get install -y ffmpeg libsndfile1 && \
    pip install -e /app[vllm] && \
    MODEL=$(python3 -c "from huggingface_hub import snapshot_download; print(snapshot_download(\"microsoft/VibeVoice-ASR\"))") && \
    python3 -m vllm_plugin.tools.generate_tokenizer_files --output "$MODEL" && \
    vllm serve "$MODEL" \
      --served-model-name vibevoice \
      --trust-remote-code \
      --dtype bfloat16 \
      --max-num-seqs 64 \
      --max-model-len 65536 \
      --gpu-memory-utilization 0.98 \
      --no-enable-prefix-caching \
      --enable-chunked-prefill \
      --chat-template-content-format openai \
      --tensor-parallel-size 1 \
      --allowed-local-media-path /app \
      --port 8000'

# Watch startup progress (model download + tokenizer generation)
docker logs -f vibevoice-vllm
```

We replicate the steps from VibeVoice's `start_server.py` but override `--gpu-memory-utilization` from `0.8` to `0.98` — the script's default leaves insufficient memory for KV cache blocks after the model (18.22 GiB) and 61-minute audio profiling are loaded.

Pinned versions: VibeVoice at `1807b858`, vLLM at `v0.14.1`. The VibeVoice plugin requires specific vLLM multimodal APIs (`PromptUpdateDetails`, `MultiModalKwargsItems`, `AudioMediaIO`) that only exist in `v0.11.1`–`v0.14.1`. The `VibeVoice/` directory is in `.gitignore`.

### 2. Install dependencies and generate a token

```bash
cd ~/Desktop/vibe-voice-vendor
uv sync --no-dev

# Generate key pair and token
uv run python -m scripts.generate_token --keys-dir keys --subject user

# Create an empty revocation file
touch revoked_tokens.txt
```

The token is saved to `keys/token.txt` for you to copy to your client machine. The `keys/` directory and `revoked_tokens.txt` are in `.gitignore`.

### 3. Build the TLS reverse proxy

```bash
cd ~/Desktop/vibe-voice-vendor/rust_proxy
cargo build --release
```

The binary is at `rust_proxy/target/release/vvv_proxy`. Self-signed certificates are auto-generated on first run at `certs/self-signed/`.

### 4. Install user systemd services

Both services run as user-level systemd units (same pattern as ollama). They start automatically on login.

```bash
cp deploy/vibevoice-server.service ~/.config/systemd/user/
cp deploy/vibevoice-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vibevoice-server
systemctl --user enable --now vibevoice-proxy
```

Verify both are running:

```bash
systemctl --user status vibevoice-server
systemctl --user status vibevoice-proxy
```

## Client Usage

### CLI

```bash
# Transcribe a file
vvv --server https://rtx5090:42862 --token YOUR_TOKEN --insecure transcribe sample/recording_with_hebrew.wav

# With hotwords
vvv --server https://rtx5090:42862 --token YOUR_TOKEN --insecure transcribe sample/recording_with_hebrew.wav --hotwords "VibeVoice,ASR"

# Save to file
vvv --server https://rtx5090:42862 --token YOUR_TOKEN --insecure transcribe sample/recording_with_hebrew.wav --output transcript.txt

# Check queue status
vvv --server https://rtx5090:42862 --token YOUR_TOKEN --insecure status
```

`--insecure` skips TLS verification for the self-signed certificate. Alternatively, use `--ca-cert certs/self-signed/fullchain.pem` to pin the cert.

### Python Library

```python
import asyncio
from client.client import VibevoiceClient
from client.models import EventType

async def main():
    client = VibevoiceClient(
        base_url="https://rtx5090:42862",
        token="YOUR_TOKEN",
        verify="certs/self-signed/fullchain.pem",
    )

    async for event in client.transcribe("sample/recording_with_hebrew.wav"):
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

### curl

```bash
# Health check (no auth required)
curl -sk https://rtx5090:42862/health
# {"status":"ok","vllm":"ok"}

# Queue status
curl -sk -H "Authorization: Bearer $TOKEN" https://rtx5090:42862/v1/queue/status
# {"your_jobs":[],"total_queued":0}

# Transcribe (streams SSE events)
curl -sk -N -H "Authorization: Bearer $TOKEN" \
  -F "audio=@sample/recording_with_hebrew.wav" \
  https://rtx5090:42862/v1/transcribe

# Transcribe with hotwords
curl -sk -N -H "Authorization: Bearer $TOKEN" \
  -F "audio=@sample/recording_with_hebrew.wav" \
  -F "hotwords=VibeVoice,ASR" \
  https://rtx5090:42862/v1/transcribe
```

`-s` silences progress, `-k` skips TLS verification for the self-signed certificate, `-N` disables output buffering for streaming.

## Configuration

All server arguments are required and passed via CLI flags. See `deploy/env.example` for the full reference.

## Token Management

```bash
# Generate a key pair and token (first run creates keys/ directory)
uv run python -m scripts.generate_token --keys-dir keys --subject user

# Token is saved to keys/token.txt — copy it to your client machine

# To revoke a token, decode its JTI and add it to the revocation file:
python -c "import jwt; print(jwt.decode('TOKEN', options={'verify_signature': False})['jti'])"
echo "JTI_VALUE" >> revoked_tokens.txt
```

## Service Management

```bash
# View logs
journalctl --user -u vibevoice-server -f
journalctl --user -u vibevoice-proxy -f

# Restart
systemctl --user restart vibevoice-server
systemctl --user restart vibevoice-proxy

# Stop
systemctl --user stop vibevoice-server
systemctl --user stop vibevoice-proxy
```
