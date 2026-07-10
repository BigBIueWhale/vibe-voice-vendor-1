# VibeVoice ASR Server

Secure, queue-based ASR server wrapping Microsoft's [VibeVoice-ASR-7B](https://github.com/microsoft/VibeVoice) model. Single-request processing via a bounded async queue, SSE streaming, transient private upload spooling with no persistent audio retention, TLS 1.3 encryption, exact server SPKI pinning, and mandatory mTLS client authentication.

## Architecture

```
Internet (HTTPS :42862) -> vvv_proxy (TLS 1.3 + pinned server key + mandatory mTLS) -> FastAPI (Unix socket) -> ASR backend
```

The public proxy accepts TLS 1.3 only. Its certificate is a self-owned server identity container; clients authenticate it by the exported `sha256/...` SPKI pin, not by DNS, WebPKI, certificate SANs, or router behavior. The proxy requires a valid client TLS certificate during the TLS handshake. The authenticated client identity is the SHA-256 hash of the client certificate SPKI, injected into the private upstream request by the proxy. A peer without the local client certificate cannot reach HTTP routing, request-body reads, FastAPI, or vLLM. Public `/health` is proxy-local after mTLS and does not touch FastAPI or vLLM. Any `Authorization` header on the public surface is rejected.

FastAPI is reached by the Rust proxy over a private Unix domain socket in both supported setup modes; it does not bind a host TCP port. The proxy refuses symlinked or non-socket upstream paths, requires the socket directory to be `0700`, requires the socket itself to be `0600`, and verifies the connected Unix peer UID/GID before sending upstream HTTP bytes. With the local `vibevoice` backend, vLLM runs in a Docker network namespace created with `--network none`, FastAPI joins that namespace and talks to vLLM over namespace-local loopback (`127.0.0.1:8000` inside that namespace only), and the only host-facing backend path is the Unix socket consumed by the Rust proxy. The vLLM container does not get the socket mount or client credentials.

Streaming is true end-to-end for authenticated transcription, token-by-token — no buffering at any proxy or response layer. vLLM emits SSE deltas → `httpx.stream()` / `aiter_lines()` yields each line as it arrives → `vllm_client` parses and yields each token → worker puts it into a per-job `asyncio.Queue` → route handler's async generator pulls and yields SSE events → FastAPI `StreamingResponse` (with `X-Accel-Buffering: no`) sends each chunk to the client immediately. Uploads and parser spill files are written under the private runtime directory `/tmp/vibevoice-vendor-UID/tmp`, kept out of the container's small RAM-backed `/tmp`, and deleted on completion, failure, cancellation, setup, or teardown.

## Setup

Prerequisites: `docker` already usable by the current user (with NVIDIA GPU support), `uv`, `cargo`, `git`, `curl`, and `ffprobe`.

```bash
./setup.sh
```

The script handles everything inside the project boundary: cloning the pinned VibeVoice source, building the Docker image (~14 GB pinned model snapshot on first build), validating GPU passthrough inside that built image with Docker networking disabled, installing Python dependencies, generating or validating local mTLS client artifacts, building the Rust TLS proxy, creating and checking a private upload temp directory with enough free space for the configured upload size and queue depth, starting the local backend containers without Docker networking, rendering the systemd user service from the current checkout path, exporting `certs/self-signed/server-spki-pin.txt`, and waiting for strict pinned-key/mTLS health checks to pass.

The public service is IPv4-only by construction: the proxy listens on `0.0.0.0:42862`, not `[::]`, and examples use an IPv4-reachable host or IPv4 address. If you use DNS, publish an A record for this service and do not rely on an AAAA path. The Android app is configured by one import bundle:

```bash
uv run python -m scripts.generate_client_bundle \
  --server-url https://HOST:42862 \
  --output keys/client-bundle.vvv.json
```

Replace `HOST` with this server's IPv4-reachable DNS name or IPv4 address. The generator validates the server URL, exact server public-key pin, client CA, client certificate, and client private key; refuses to overwrite an existing bundle; and writes the bundle as `0600` because it contains private key material. The URL is only routing. The server SPKI pin is the server identity. The client cert/key are the client identity.

`setup.sh` installs services for the checkout you run it from. The checkout path must use plain systemd-safe characters (`A-Z`, `a-z`, `0-9`, `.`, `_`, `/`, `@`, `+`, `-`); paths containing spaces, quotes, `$`, `%`, or other punctuation are rejected. Do not move the repository after setup; rerun `./setup.sh` from the new path instead.

On subsequent runs, `setup.sh` only reuses a Docker image whose source fingerprint and security profile match the current checkout. Existing client-auth artifacts must validate exactly; partial or inconsistent local auth state is refused. Use `--force-rebuild` to force a Docker image rebuild.

If you don't have 32 GiB of VRAM free, you can use OpenAI Whisper large-v3 served via [Groq](https://console.groq.com) instead — no GPU needed:

```bash
./setup.sh --backend groq --groq-api-key gsk_YOUR_KEY_HERE
```

This is useful for private voice typing workflows without sacrificing a GPU to be idle (with VRAM full) 99.9% of the time. No known cloud inference provider supports VibeVoice-ASR as of today — if you're a provider interested in hosting it, see [this discussion](https://huggingface.co/microsoft/VibeVoice-ASR/discussions/21).

## vLLM Tuning

All vLLM flags are set in the Dockerfile `CMD` and can be overridden at runtime:

```bash
docker run -d --pull=never --gpus all --name vibevoice-vllm \
  --network container:vibevoice-backend-netns \
  --ipc=private --shm-size 16g \
  --restart unless-stopped \
  --user 65532:65532 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=4g \
  --tmpfs /var/tmp:rw,nosuid,nodev,size=1g \
  vibevoice-vllm:latest \
  --served-model-name vibevoice \
  --host 127.0.0.1 \
  --trust-remote-code \
  --dtype bfloat16 \
  --max-num-seqs 64 \
  --max-model-len 48000 \
  --gpu-memory-utilization 0.90 \
  --no-enable-prefix-caching \
  --enable-chunked-prefill \
  --chat-template-content-format openai \
  --tensor-parallel-size 1 \
  --port 8000
```

We override two flags from VibeVoice's `start_server.py`:

- **`--gpu-memory-utilization 0.90`** (upstream default `0.8`): The model weights take 18.22 GiB. vLLM pre-allocates KV cache from whatever VRAM remains within the utilization budget, and anything outside the budget stays free for the audio encoder's forward pass (~700 MiB peak for long audio). At `0.98` the KV cache consumed nearly all remaining VRAM, causing OOM on files longer than ~1 minute. At `0.90` roughly 3 GiB stays free for the encoder.

- **`--max-model-len 48000`** (upstream default `65536`): With `0.90` utilization only ~2.6 GiB is available for KV cache, enough for ~48K tokens but not 65K. This is still sufficient for 60-minute audio: 60min × 60s × 24kHz / 3200 compression ratio = ~27K audio tokens, plus ~16K output tokens = ~43K total.

**Startup time (~85 seconds)**: The container makes zero network requests — everything is baked into the image. The time is spent on GPU initialization:

| Phase | Duration |
|-------|----------|
| Load model weights (18.22 GiB from disk) | ~14s |
| `torch.compile` | ~7s |
| CUDA graph capture (decode, FULL) | ~63s |

CUDA graph capture dominates: vLLM pre-records optimized GPU execution graphs for different batch sizes so it can replay them during inference instead of launching individual kernels. This is a one-time cost per container start, not per request. Disabling it (`--enforce-eager`) would make every inference request slower.

**Known issue — repetition loop on long audio**: On a 7-minute test file (`sample/letter_factory_leap_frog.wav`), the model transcribed correctly up to ~4m20s then degenerated into an infinite repetition loop ("wop wop wop...") on a segment that likely contains music or sound effects. The loop continued until the 48K token limit was exhausted, inflating wall-clock time to 8m31s (most of it spent generating junk tokens). This is a known LLM degeneration pattern, not a server bug — the model lacks a built-in repetition penalty. Short speech-only files transcribe without issue.

Pinned versions: VibeVoice at `1807b858d4f7dffdd286249a01616c243e488c9e`, VibeVoice-ASR model snapshot at `d0c9efdb8d614685062c04425d91e01b6f37d944`, and `vllm/vllm-openai:v0.14.1` by image digest. The Docker build installs the vendored VibeVoice plugin with `--no-deps --no-build-isolation`, so VibeVoice's broad Python dependency metadata cannot trigger unpinned package resolution during image builds. The Docker image also installs the explicitly pinned vLLM audio runtime packages required for `audio_url` transcription (`librosa`, `scipy`, `soundfile`, and their pinned runtime closure), then fails the build if vLLM still sees audio placeholders. The image does not install Ubuntu's broad `ffmpeg` package; it builds official FFmpeg `8.1.2` from a signed, SHA-256-pinned release tarball with network support disabled and only the local `file`/`pipe` protocols plus the current audio demuxers/decoders needed for accepted uploads. During the image build, VibeVoice's ffmpeg/ffprobe audio subprocesses are patched to run at one process, one ffmpeg thread, local file/pipe protocols only, and a 900-second subprocess timeout. `setup.sh` only reuses `vibevoice-vllm:latest` when image labels match the current Dockerfile, locked Python runtime, server files, pinned VibeVoice checkout, model revision, and security profile; stale images are rebuilt, and images whose vLLM command still contains `--allowed-local-media-path` are refused. The VibeVoice plugin requires specific vLLM multimodal APIs (`PromptUpdateDetails`, `MultiModalKwargsItems`, `AudioMediaIO`) that only exist in `v0.11.1`–`v0.14.1`. The `VibeVoice/` directory is in `.gitignore`.

See [doc/vibevoice-asr-quality-investigation.md](doc/vibevoice-asr-quality-investigation.md) for a deep-dive into every inference parameter, dtype, prompt template, and audio preprocessing step — verifying correctness against the official Microsoft reference code.

## Client Usage

### CLI

```bash
# Transcribe a file
vvv --server https://HOST:42862 \
  --server-pin "$(cat certs/self-signed/server-spki-pin.txt)" \
  --client-cert keys/client-cert.pem \
  --client-key keys/client-key.pem \
  transcribe sample/recording_with_hebrew.wav

# With hotwords
vvv --server https://HOST:42862 \
  --server-pin "$(cat certs/self-signed/server-spki-pin.txt)" \
  --client-cert keys/client-cert.pem \
  --client-key keys/client-key.pem \
  transcribe sample/recording_with_hebrew.wav --hotwords "VibeVoice,ASR"

# Save to file
vvv --server https://HOST:42862 \
  --server-pin "$(cat certs/self-signed/server-spki-pin.txt)" \
  --client-cert keys/client-cert.pem \
  --client-key keys/client-key.pem \
  transcribe sample/recording_with_hebrew.wav --output transcript.txt

# Check queue status
vvv --server https://HOST:42862 \
  --server-pin "$(cat certs/self-signed/server-spki-pin.txt)" \
  --client-cert keys/client-cert.pem \
  --client-key keys/client-key.pem \
  status
```

`--server-pin` is the server identity check. The URL is only where the client dials; DNS names and certificate SANs do not authorize the server. `--client-cert` and `--client-key` present the local mTLS credential required by the public proxy before any HTTP request is accepted.

### Python Library

```python
import asyncio
from pathlib import Path
from client.client import VibevoiceClient
from client.models import EventType

async def main():
    client = VibevoiceClient(
        base_url="https://HOST:42862",
        server_pin=Path("certs/self-signed/server-spki-pin.txt").read_text().strip(),
        cert=("keys/client-cert.pem", "keys/client-key.pem"),
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
| POST | `/v1/transcribe` | mTLS | Upload audio + stream transcription via SSE |
| GET | `/v1/queue/status` | mTLS | Get your queue position and job status |
| GET | `/health` | mTLS | Proxy-local liveness check; does not reach FastAPI or vLLM |

### curl

```bash
# Health check
curl --insecure \
  --pinnedpubkey "$(sed 's#^sha256/#sha256//#' certs/self-signed/server-spki-pin.txt)" \
  --cert keys/client-cert.pem \
  --key keys/client-key.pem \
  -s https://HOST:42862/health
# {"status":"ok","proxy":"ok"}

# Queue status
curl --insecure \
  --pinnedpubkey "$(sed 's#^sha256/#sha256//#' certs/self-signed/server-spki-pin.txt)" \
  --cert keys/client-cert.pem \
  --key keys/client-key.pem \
  -s \
  https://HOST:42862/v1/queue/status
# {"your_jobs":[],"total_queued":0}

# Transcribe (streams SSE events)
curl --insecure \
  --pinnedpubkey "$(sed 's#^sha256/#sha256//#' certs/self-signed/server-spki-pin.txt)" \
  --cert keys/client-cert.pem \
  --key keys/client-key.pem \
  -s -N \
  -F "audio=@sample/recording_with_hebrew.wav" \
  https://HOST:42862/v1/transcribe

# Transcribe with hotwords
curl --insecure \
  --pinnedpubkey "$(sed 's#^sha256/#sha256//#' certs/self-signed/server-spki-pin.txt)" \
  --cert keys/client-cert.pem \
  --key keys/client-key.pem \
  -s -N \
  -F "audio=@sample/recording_with_hebrew.wav" \
  -F "hotwords=VibeVoice,ASR" \
  https://HOST:42862/v1/transcribe
```

`--pinnedpubkey` is mandatory in curl examples. `--insecure` disables curl's WebPKI/hostname authority so the SPKI pin is the sole server trust check; do not use it without `--pinnedpubkey`. `-s` silences progress and `-N` disables output buffering for streaming.

The public proxy `/health` endpoint is intentionally cheap and proxy-local, but it still requires the same mTLS client credential as the rest of the public surface. It only proves the public TLS proxy is alive and never reaches FastAPI or vLLM. The FastAPI server still exposes its internal `/health` endpoint for setup and local diagnostics over the backend Unix socket; with the local `vibevoice` backend it returns HTTP 503 with `{"status":"degraded",...}` if vLLM is unreachable or returns a non-200 health response.

## Configuration

`setup.sh` renders systemd units with explicit server and proxy arguments. See `deploy/env.example` for the direct-invocation reference.

## Client Credential Management

```bash
# Server identity pin is saved to certs/self-signed/server-spki-pin.txt.
# mTLS client credentials are saved to keys/client-cert.pem and keys/client-key.pem.
uv run python -m scripts.generate_client_cert --certs-dir certs/self-signed --keys-dir keys --subject user

# Validate existing artifacts without modifying them.
uv run python -m scripts.validate_client_cert --certs-dir certs/self-signed --keys-dir keys

# Create the one-file Android import bundle from validated artifacts.
uv run python -m scripts.generate_client_bundle \
  --server-url https://HOST:42862 \
  --output keys/client-bundle.vvv.json
```

## Service Management

```bash
# View logs
journalctl --user -u vibevoice-proxy -f
docker logs -f vibevoice-server-container
docker logs -f vibevoice-vllm

# Restart the public proxy only
systemctl --user restart vibevoice-proxy

# Reinstall from this checkout
./setup.sh

# Uninstall the installed runtime
./teardown.sh
```

`teardown.sh` removes the installed user systemd units, runtime containers, runtime sockets, the Groq backend environment file, and local VibeVoice Docker images, including `vibevoice-vllm:latest` and stale project-labelled builds. It does not delete the local client/server credential artifacts under `keys/` and `certs/`; rotate those explicitly when the trust material itself should change.
