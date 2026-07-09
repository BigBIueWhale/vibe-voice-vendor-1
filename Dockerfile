FROM vllm/vllm-openai:v0.14.1@sha256:6bf34e50e2387dc46dc87a9d6a945fdd616a022bccfddd949052f54063ebcb8c

ARG VIBEVOICE_MODEL_REVISION=d0c9efdb8d614685062c04425d91e01b6f37d944
ENV VIBEVOICE_MODEL_REVISION=${VIBEVOICE_MODEL_REVISION}

# ── Layer 1: System packages ─────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

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
import vllm_plugin

vllm_plugin.register_vibevoice()
AutoConfig.from_pretrained("/models/VibeVoice-ASR", trust_remote_code=True)
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
