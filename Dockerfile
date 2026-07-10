FROM vllm/vllm-openai:v0.14.1@sha256:6bf34e50e2387dc46dc87a9d6a945fdd616a022bccfddd949052f54063ebcb8c AS ffmpeg-builder

ARG FFMPEG_VERSION=8.1.2
ARG FFMPEG_SHA256=464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c
ARG FFMPEG_SIGNING_KEY=FCF986EA15E6E293A5644F10B4322F04D67658D8
ENV FFMPEG_PREFIX=/opt/vvv-ffmpeg

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        build-essential \
        gnupg \
        xz-utils && \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSLo /tmp/ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz"; \
    curl -fsSLo /tmp/ffmpeg.tar.xz.asc "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz.asc"; \
    curl -fsSLo /tmp/ffmpeg-devel.asc "https://ffmpeg.org/ffmpeg-devel.asc"; \
    export GNUPGHOME="$(mktemp -d)"; \
    gpg --batch --import /tmp/ffmpeg-devel.asc; \
    gpg --batch --fingerprint --with-colons "$FFMPEG_SIGNING_KEY" | grep -q "^fpr:::::::::${FFMPEG_SIGNING_KEY}:"; \
    gpg --batch --verify /tmp/ffmpeg.tar.xz.asc /tmp/ffmpeg.tar.xz; \
    echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c -; \
    rm -rf "$GNUPGHOME"; \
    mkdir -p /tmp/ffmpeg-src; \
    tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg-src --strip-components=1; \
    cd /tmp/ffmpeg-src; \
    ./configure \
        --prefix="$FFMPEG_PREFIX" \
        --disable-doc \
        --disable-debug \
        --disable-network \
        --disable-autodetect \
        --disable-everything \
        --disable-x86asm \
        --disable-ffplay \
        --disable-avdevice \
        --disable-swscale \
        --enable-ffmpeg \
        --enable-ffprobe \
        --enable-protocol=file,pipe \
        --enable-demuxer=wav,mp3,mov,flac,ogg,matroska,asf,aac \
        --enable-muxer=pcm_s16le,null \
        --enable-decoder=aac,aac_fixed,aac_latm,alac,flac,mp3,mp3float,opus,vorbis,speex,wmav1,wmav2,wmapro,wmalossless,wmavoice,pcm_alaw,pcm_mulaw,pcm_f32be,pcm_f32le,pcm_f64be,pcm_f64le,pcm_s16be,pcm_s16be_planar,pcm_s16le,pcm_s16le_planar,pcm_s24be,pcm_s24le,pcm_s24le_planar,pcm_s32be,pcm_s32le,pcm_s32le_planar,pcm_s8,pcm_u8,pcm_u16be,pcm_u16le,pcm_u24be,pcm_u24le,pcm_u32be,pcm_u32le,adpcm_ms,adpcm_ima_wav,adpcm_g722,adpcm_g726,adpcm_g726le,adpcm_yamaha \
        --enable-encoder=pcm_s16le \
        --enable-parser=aac,aac_latm,flac,mpegaudio,opus,vorbis \
        --enable-filter=aresample,aformat,anull,pan,channelmap; \
    make -j"$(nproc)"; \
    make install; \
    rm -rf /tmp/ffmpeg-src /tmp/ffmpeg.tar.xz /tmp/ffmpeg.tar.xz.asc /tmp/ffmpeg-devel.asc

RUN python3 - <<'PY'
import json
import math
import re
import struct
import subprocess
import wave
from pathlib import Path

BIN = Path("/opt/vvv-ffmpeg/bin")


def run(args: list[str]) -> str:
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)


def has_line(output: str, pattern: str) -> bool:
    return re.search(pattern, output, re.MULTILINE) is not None


buildconf = run([str(BIN / "ffmpeg"), "-hide_banner", "-buildconf"])
required_flags = [
    "--disable-network",
    "--disable-autodetect",
    "--disable-everything",
    "--disable-avdevice",
    "--disable-swscale",
    "--enable-protocol='file,pipe'",
]
for flag in required_flags:
    if flag not in buildconf:
        raise RuntimeError(f"minimal FFmpeg build is missing {flag}")

protocols = run([str(BIN / "ffmpeg"), "-hide_banner", "-protocols"])
for protocol in ("file", "pipe"):
    if not has_line(protocols, rf"^\s*{protocol}\s*$"):
        raise RuntimeError(f"minimal FFmpeg build is missing {protocol} protocol")
for protocol in ("http", "https", "tcp", "udp", "tls", "data", "concat", "subfile", "crypto"):
    if has_line(protocols, rf"^\s*{protocol}\s*$"):
        raise RuntimeError(f"minimal FFmpeg build unexpectedly enables {protocol} protocol")

demuxers = run([str(BIN / "ffmpeg"), "-hide_banner", "-demuxers"])
for demuxer in ("wav", "mp3", "mov", "flac", "ogg", "matroska", "asf", "aac"):
    if not has_line(demuxers, rf"^\s*D\s+{demuxer}\b"):
        raise RuntimeError(f"minimal FFmpeg build is missing {demuxer} demuxer")
for demuxer in ("hls", "concat", "dash", "rtsp", "image2", "mpegts"):
    if has_line(demuxers, rf"^\s*D\s+{demuxer}\b"):
        raise RuntimeError(f"minimal FFmpeg build unexpectedly enables {demuxer} demuxer")

decoders = run([str(BIN / "ffmpeg"), "-hide_banner", "-decoders"])
for decoder in ("aac", "flac", "mp3", "opus", "vorbis", "pcm_s16le"):
    if not has_line(decoders, rf"^ A.*D\s+{decoder}\b"):
        raise RuntimeError(f"minimal FFmpeg build is missing {decoder} decoder")
for decoder in ("jpeg2000", "pgm", "ppm", "pnm", "png", "gif", "webp", "h264", "hevc", "av1", "als"):
    if has_line(decoders, rf"^.{{6}}\s+{decoder}\b"):
        raise RuntimeError(f"minimal FFmpeg build unexpectedly enables {decoder} decoder")

wav_path = Path("/tmp/ffmpeg-smoke.wav")
with wave.open(str(wav_path), "wb") as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(16000)
    frames = bytearray()
    for i in range(16000):
        sample = int(math.sin(i / 16000 * 2 * math.pi * 440) * 8000)
        frames.extend(struct.pack("<h", sample))
    f.writeframes(bytes(frames))

pcm = subprocess.check_output([
    str(BIN / "ffmpeg"),
    "-hide_banner",
    "-loglevel",
    "error",
    "-protocol_whitelist",
    "file,pipe",
    "-i",
    str(wav_path),
    "-f",
    "s16le",
    "-ac",
    "1",
    "-acodec",
    "pcm_s16le",
    "-ar",
    "24000",
    "-",
])
if len(pcm) != 48000:
    raise RuntimeError(f"minimal FFmpeg decode smoke test returned {len(pcm)} bytes")

probe = run([
    str(BIN / "ffprobe"),
    "-v",
    "quiet",
    "-protocol_whitelist",
    "file",
    "-print_format",
    "json",
    "-show_format",
    str(wav_path),
])
duration = float(json.loads(probe)["format"]["duration"])
if not 0.99 <= duration <= 1.01:
    raise RuntimeError(f"minimal FFprobe smoke test returned duration {duration!r}")
PY

FROM vllm/vllm-openai:v0.14.1@sha256:6bf34e50e2387dc46dc87a9d6a945fdd616a022bccfddd949052f54063ebcb8c

ARG VIBEVOICE_MODEL_REVISION=d0c9efdb8d614685062c04425d91e01b6f37d944
ENV VIBEVOICE_MODEL_REVISION=${VIBEVOICE_MODEL_REVISION}
ENV PATH=/opt/vvv-ffmpeg/bin:${PATH}

# ── Layer 1: System packages ─────────────────────────────────────────
COPY --from=ffmpeg-builder /opt/vvv-ffmpeg /opt/vvv-ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends libsndfile1 && \
    rm -rf /var/lib/apt/lists/* && \
    test "$(command -v ffmpeg)" = "/opt/vvv-ffmpeg/bin/ffmpeg" && \
    test "$(command -v ffprobe)" = "/opt/vvv-ffmpeg/bin/ffprobe"

# ── Layer 2: Model weights (~14 GB, cached independently of source) ──
RUN python3 -c "\
import os; \
from huggingface_hub import snapshot_download; \
snapshot_download( \
    'microsoft/VibeVoice-ASR', \
    revision=os.environ['VIBEVOICE_MODEL_REVISION'], \
    local_dir='/models/VibeVoice-ASR')"

# ── Layer 3: VibeVoice package + tokenizer files ─────────────────────
COPY pyproject.toml uv.lock /build/vvv/
COPY VibeVoice/ /build/VibeVoice/
RUN python3 - <<'PY'
from pathlib import Path
import tomllib


def write_locked_direct_requirements(group_name: str, output_path: str) -> None:
    with open("/build/vvv/uv.lock", "rb") as f:
        lock = tomllib.load(f)

    packages = {package["name"]: package for package in lock["package"]}
    root = packages["vibe-voice-vendor"]
    metadata_group = root["metadata"]["requires-dev"][group_name]
    group = root["dev-dependencies"][group_name]

    pinned = {dep["name"]: dep["specifier"] for dep in metadata_group}
    lines = [
        "# Generated from uv.lock during the Docker build.",
        "# Only direct image-runtime packages are installed here; the pinned vLLM",
        "# base image already provides the rest of the import surface.",
    ]
    for dep in group:
        name = dep["name"]
        if set(dep) != {"name"}:
            raise RuntimeError(f"{group_name} dependency {dep!r} is not a simple direct pin")
        package = packages[name]
        if "registry" not in package.get("source", {}):
            raise RuntimeError(f"{group_name} package {name} is not registry-pinned")
        expected_specifier = f"=={package['version']}"
        if pinned.get(name) != expected_specifier:
            raise RuntimeError(
                f"{group_name} package {name} must be pinned as {expected_specifier}"
            )
        hashes = [wheel["hash"] for wheel in package.get("wheels", [])]
        if not hashes:
            raise RuntimeError(f"{group_name} package {name} has no locked wheel hashes")
        parts = [f"{name}=={package['version']}"]
        parts.extend(f"--hash={hash_value}" for hash_value in sorted(set(hashes)))
        continuation = " " + "\\" + "\n    "
        lines.append(continuation.join(parts))
    Path(output_path).write_text("\n".join(lines) + "\n")

path = Path("/build/VibeVoice/vibevoice/processor/audio_utils.py")
text = path.read_text()

thread_old = '"-threads", "0",'
thread_new = '"-threads", os.getenv("VIBEVOICE_FFMPEG_THREADS", "1"),'
if text.count(thread_old) != 2:
    raise RuntimeError("VibeVoice ffmpeg thread patch did not match exactly twice")
text = text.replace(thread_old, thread_new)

probe_protocol_old = '''            "-v", "quiet",
            "-show_entries", "stream=sample_rate",
'''
probe_protocol_new = '''            "-v", "quiet",
            "-protocol_whitelist", "file",
            "-show_entries", "stream=sample_rate",
'''
if text.count(probe_protocol_old) != 1:
    raise RuntimeError("VibeVoice ffprobe protocol patch did not match exactly once")
text = text.replace(probe_protocol_old, probe_protocol_new)

probe_run_old = "original_sr = int(run(cmd_probe, capture_output=True, check=True).stdout.decode().strip())"
probe_run_new = "original_sr = int(_run_ffmpeg(cmd_probe).stdout.decode().strip())"
if text.count(probe_run_old) != 1:
    raise RuntimeError("VibeVoice ffprobe runner patch did not match exactly once")
text = text.replace(probe_run_old, probe_run_new)

file_protocol_old = '''        "-threads", os.getenv("VIBEVOICE_FFMPEG_THREADS", "1"),
        "-i", file,
'''
file_protocol_new = '''        "-threads", os.getenv("VIBEVOICE_FFMPEG_THREADS", "1"),
        "-protocol_whitelist", "file,pipe",
        "-i", file,
'''
if text.count(file_protocol_old) != 1:
    raise RuntimeError("VibeVoice file ffmpeg protocol patch did not match exactly once")
text = text.replace(file_protocol_old, file_protocol_new)

pipe_protocol_old = '''        "-threads", os.getenv("VIBEVOICE_FFMPEG_THREADS", "1"),
        "-i", "pipe:0",
'''
pipe_protocol_new = '''        "-threads", os.getenv("VIBEVOICE_FFMPEG_THREADS", "1"),
        "-protocol_whitelist", "pipe",
        "-i", "pipe:0",
'''
if text.count(pipe_protocol_old) != 1:
    raise RuntimeError("VibeVoice pipe ffmpeg protocol patch did not match exactly once")
text = text.replace(pipe_protocol_old, pipe_protocol_new)

run_old = '''def _run_ffmpeg(cmd: list, *, stdin_bytes: bytes = None):
    """Run ffmpeg with optional global concurrency limiting.

    This is important for vLLM multi-request concurrency: spawning too many
    ffmpeg processes can saturate CPU/IO and cause request failures/timeouts.
    """
    if _FFMPEG_SEM is None:
        return run(cmd, capture_output=True, check=True, input=stdin_bytes)
    with _FFMPEG_SEM:
        return run(cmd, capture_output=True, check=True, input=stdin_bytes)
'''
run_new = '''def _get_ffmpeg_timeout_seconds():
    """Get the FFmpeg subprocess timeout from environment variable."""
    v = os.getenv("VIBEVOICE_FFMPEG_TIMEOUT_SECONDS", "")
    try:
        n = float(v) if v.strip() else 0.0
    except Exception:
        n = 0.0
    return n if n > 0 else None


def _run_ffmpeg(cmd: list, *, stdin_bytes: bytes = None):
    """Run ffmpeg with bounded process concurrency and runtime."""
    timeout = _get_ffmpeg_timeout_seconds()
    if _FFMPEG_SEM is None:
        return run(cmd, capture_output=True, check=True, input=stdin_bytes, timeout=timeout)
    with _FFMPEG_SEM:
        return run(cmd, capture_output=True, check=True, input=stdin_bytes, timeout=timeout)
'''
if run_old not in text:
    raise RuntimeError("VibeVoice ffmpeg runner patch did not match")
path.write_text(text.replace(run_old, run_new))

metadata_path = Path("/build/VibeVoice/pyproject.toml")
metadata = metadata_path.read_text()
deps_old = '''dependencies = [
    "torch",
    "transformers>=4.51.3,<5.0.0",
    "accelerate",
    "llvmlite>=0.40.0",
    "numba>=0.57.0",
    "diffusers",
    "tqdm",
    "numpy",
    "scipy",
    "librosa",
    "ml-collections",
    "absl-py",
    "gradio",
    "av",
    "aiortc",
    "uvicorn[standard]",
    "fastapi",
    "pydub",
    "requests",
]
'''
deps_new = '''dependencies = [
    "diffusers==0.36.0",
]
'''
if metadata.count(deps_old) != 1:
    raise RuntimeError("VibeVoice dependency metadata patch did not match exactly once")
metadata_path.write_text(metadata.replace(deps_old, deps_new))

write_locked_direct_requirements(
    "vibevoice-image",
    "/tmp/vvv-vibevoice-image-requirements.txt",
)
PY
RUN pip install --no-cache-dir \
        --require-hashes \
        --only-binary=:all: \
        --no-deps \
        -r /tmp/vvv-vibevoice-image-requirements.txt && \
    pip install --no-cache-dir --no-deps --no-build-isolation /build/VibeVoice && \
    python3 - <<'PY' && \
    python3 /build/VibeVoice/vllm_plugin/tools/generate_tokenizer_files.py \
        --output /models/VibeVoice-ASR && \
    rm -rf /build/VibeVoice /tmp/vvv-vibevoice-image-requirements.txt
from transformers import AutoConfig
import librosa
import scipy.signal
import soundfile
from vllm.multimodal import audio as vllm_audio
import vllm_plugin

vllm_plugin.register_vibevoice()
AutoConfig.from_pretrained("/models/VibeVoice-ASR", trust_remote_code=True)
for module_name, module in (
    ("librosa", vllm_audio.librosa),
    ("soundfile", vllm_audio.soundfile),
    ("scipy.signal", vllm_audio.scipy_signal),
):
    if module.__class__.__name__ == "PlaceholderModule":
        raise RuntimeError(f"vLLM audio dependency {module_name} is missing")
PY

# ── Layer 4: Isolated FastAPI server runtime ─────────────────────────
COPY server/ /opt/vvv-server/server/
RUN python3 - <<'PY' && \
    python3 -m venv /opt/vvv-server-venv && \
    /opt/vvv-server-venv/bin/pip install --no-cache-dir \
        --require-hashes \
        --only-binary=:all: \
        -r /tmp/vvv-requirements.txt && \
    mkdir -p /run/vibevoice /run/vibevoice-auth && \
    rm -rf /build/vvv /tmp/vvv-requirements.txt
from pathlib import Path
import tomllib

with open("/build/vvv/uv.lock", "rb") as f:
    lock = tomllib.load(f)

packages = {package["name"]: package for package in lock["package"]}
root = packages["vibe-voice-vendor"]
runtime_names = set()
pending = [dep["name"] for dep in root["dependencies"]]
while pending:
    name = pending.pop()
    if name in runtime_names:
        continue
    package = packages[name]
    if "registry" not in package.get("source", {}):
        raise RuntimeError(f"Runtime package {name} is not registry-pinned")
    runtime_names.add(name)
    pending.extend(dep["name"] for dep in package.get("dependencies", []))

lines = [
    "# Generated from uv.lock during the Docker build.",
    "# pip is run with --require-hashes, so downloaded artifacts must match this lockfile.",
]
for name in sorted(runtime_names):
    package = packages[name]
    hashes = []
    if "sdist" in package:
        hashes.append(package["sdist"]["hash"])
    hashes.extend(wheel["hash"] for wheel in package.get("wheels", []))
    if not hashes:
        raise RuntimeError(f"Runtime package {name} has no locked hashes")
    parts = [f"{name}=={package['version']}"]
    parts.extend(f"--hash={hash_value}" for hash_value in sorted(set(hashes)))
    continuation = " " + "\\" + "\n    "
    lines.append(continuation.join(parts))
Path("/tmp/vvv-requirements.txt").write_text("\n".join(lines) + "\n")
PY

ENV VIBEVOICE_FFMPEG_MAX_CONCURRENCY=1
ENV VIBEVOICE_FFMPEG_THREADS=1
ENV VIBEVOICE_FFMPEG_TIMEOUT_SECONDS=900
ENV VIBEVOICE_USE_MEAN=1
ENV PYTORCH_ALLOC_CONF=expandable_segments:True

ENTRYPOINT ["python3", "-m", "vllm.entrypoints.openai.api_server", \
            "--model", "/models/VibeVoice-ASR"]

CMD ["--served-model-name", "vibevoice", \
     "--host", "127.0.0.1", \
     "--trust-remote-code", \
     "--dtype", "bfloat16", \
     "--max-num-seqs", "64", \
     "--max-model-len", "48000", \
     "--gpu-memory-utilization", "0.90", \
     "--no-enable-prefix-caching", \
     "--enable-chunked-prefill", \
     "--chat-template-content-format", "openai", \
     "--tensor-parallel-size", "1", \
     "--port", "8000"]
