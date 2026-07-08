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
COPY VibeVoice/ /build/VibeVoice/
RUN pip install --no-cache-dir /build/VibeVoice && \
    python3 /build/VibeVoice/vllm_plugin/tools/generate_tokenizer_files.py \
        --output /models/VibeVoice-ASR && \
    rm -rf /build

ENV VIBEVOICE_FFMPEG_MAX_CONCURRENCY=64
ENV VIBEVOICE_USE_MEAN=1
ENV PYTORCH_ALLOC_CONF=expandable_segments:True

ENTRYPOINT ["python3", "-m", "vllm.entrypoints.openai.api_server", \
            "--model", "/models/VibeVoice-ASR"]

CMD ["--served-model-name", "vibevoice", \
     "--trust-remote-code", \
     "--dtype", "bfloat16", \
     "--max-num-seqs", "64", \
     "--max-model-len", "48000", \
     "--gpu-memory-utilization", "0.90", \
     "--no-enable-prefix-caching", \
     "--enable-chunked-prefill", \
     "--chat-template-content-format", "openai", \
     "--tensor-parallel-size", "1", \
     "--allowed-local-media-path", "/tmp", \
     "--port", "8000"]
