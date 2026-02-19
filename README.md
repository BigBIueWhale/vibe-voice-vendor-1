# VibeVoice ASR Server

Secure, queue-based ASR server wrapping Microsoft's [VibeVoice-ASR-7B](https://github.com/microsoft/VibeVoice) model. Single-request processing via an async queue, SSE streaming, zero data storage, TLS encryption, JWT bearer auth (ES256).

## Architecture

```
Internet (HTTPS :42862) -> vvv_proxy (self-signed TLS) -> FastAPI (:54912 127.0.0.1) -> vLLM (:37845 127.0.0.1)
```

## Setup

Prerequisites: `docker` (with NVIDIA GPU support), `uv`, `cargo`, `git`, `curl`.

```bash
./setup.sh
```

The script handles everything: cloning VibeVoice, building the Docker image (~14 GB model download on first build), installing Python dependencies, generating JWT keys, building the Rust TLS proxy, installing systemd services, and waiting for all health checks to pass.

On subsequent runs it skips steps that are already done (existing image, existing keys, etc.). Use `--force-rebuild` to force a Docker image rebuild.

## vLLM Tuning

All vLLM flags are set in the Dockerfile `CMD` and can be overridden at runtime:

```bash
docker run -d --gpus all --name vibevoice-vllm \
  --ipc=host --restart unless-stopped \
  -p 127.0.0.1:37845:8000 \
  vibevoice-vllm:latest \
  --served-model-name vibevoice \
  --trust-remote-code \
  --dtype bfloat16 \
  --max-num-seqs 64 \
  --max-model-len 65536 \
  --gpu-memory-utilization 0.95 \
  --no-enable-prefix-caching \
  --enable-chunked-prefill \
  --chat-template-content-format openai \
  --tensor-parallel-size 1 \
  --allowed-local-media-path /tmp \
  --port 8000
```

We override two flags from VibeVoice's `start_server.py`:

- **`--gpu-memory-utilization 0.90`** (upstream default `0.8`): The model weights take 18.22 GiB. vLLM pre-allocates KV cache from whatever VRAM remains within the utilization budget, and anything outside the budget stays free for the audio encoder's forward pass (~700 MiB peak for long audio). At `0.98` the KV cache consumed nearly all remaining VRAM, causing OOM on files longer than ~1 minute. At `0.90` roughly 3 GiB stays free for the encoder.

- **`--max-model-len 48000`** (upstream default `65536`): With `0.90` utilization only ~2.6 GiB is available for KV cache, enough for ~48K tokens but not 65K. This is still sufficient for 60-minute audio: 60min × 60s × 24kHz / 3200 compression ratio = ~27K audio tokens, plus ~16K output tokens = ~43K total.

**Known issue — repetition loop on long audio**: On a 7-minute test file (`sample/letter_factory_leap_frog.wav`), the model transcribed correctly up to ~4m20s then degenerated into an infinite repetition loop ("wop wop wop...") on a segment that likely contains music or sound effects. The loop continued until the 48K token limit was exhausted, inflating wall-clock time to 8m31s (most of it spent generating junk tokens). This is a known LLM degeneration pattern, not a server bug — the model lacks a built-in repetition penalty. Short speech-only files transcribe without issue.

Pinned versions: VibeVoice at `1807b858`, vLLM at `v0.14.1`. The VibeVoice plugin requires specific vLLM multimodal APIs (`PromptUpdateDetails`, `MultiModalKwargsItems`, `AudioMediaIO`) that only exist in `v0.11.1`–`v0.14.1`. The `VibeVoice/` directory is in `.gitignore`.

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
