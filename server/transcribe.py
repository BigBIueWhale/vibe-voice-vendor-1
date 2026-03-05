import json

import httpx

from server.config import Settings
from server.groq_client import transcribe_audio
from server.models import JobStatus
from server.queue import TranscriptionJob
from server.vllm_client import stream_transcription


async def process_vibevoice_job(
    job: TranscriptionJob,
    http_client: httpx.AsyncClient,
    config: Settings,
) -> None:
    """Worker function that processes a job via local vLLM VibeVoice."""
    accumulated = []
    first_chunk = True
    async for chunk in stream_transcription(
        http_client=http_client,
        vllm_base_url=config.vllm_base_url,
        model_name=config.vllm_model_name,
        audio_base64=job.audio_base64,
        audio_mime=job.audio_mime,
        audio_duration=job.audio_duration_seconds,
        hotwords=job.hotwords,
        temperature=config.vllm_temperature,
        top_p=config.vllm_top_p,
    ):
        if first_chunk:
            job.status = JobStatus.STREAMING
            first_chunk = False
        accumulated.append(chunk)
        await job.chunk_queue.put(chunk)

    # Validate that model output is the expected JSON segment format.
    # Clients depend on [{"Start":..,"End":..,"Content":..},...] structure.
    raw = "".join(accumulated)
    _validate_vibevoice_output(raw, job)

    # Signal end of stream
    await job.chunk_queue.put(None)


def _validate_vibevoice_output(raw: str, job: TranscriptionJob) -> None:
    """Validate VibeVoice model output is parseable JSON segments.

    Sets job.error_message with full diagnostic context if validation fails.
    The data chunks are already streamed to the client, so the error event
    arrives after the data — giving the client both the raw output and the
    diagnosis.
    """
    trimmed = raw.strip()
    if not trimmed:
        job.error_message = (
            "VibeVoice model returned empty output. "
            f"audio_duration={job.audio_duration_seconds:.2f}s, "
            f"audio_mime={job.audio_mime}"
        )
        return

    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError as e:
        preview = trimmed[:500]
        job.error_message = (
            f"VibeVoice model output is not valid JSON: {e}. "
            f"audio_duration={job.audio_duration_seconds:.2f}s, "
            f"output_length={len(trimmed)}, "
            f"output_preview={preview!r}"
        )
        return

    if not isinstance(parsed, list):
        job.error_message = (
            f"VibeVoice model output is {type(parsed).__name__}, expected list. "
            f"audio_duration={job.audio_duration_seconds:.2f}s, "
            f"output_preview={trimmed[:500]!r}"
        )
        return

    for i, seg in enumerate(parsed):
        if not isinstance(seg, dict):
            job.error_message = (
                f"VibeVoice segment[{i}] is {type(seg).__name__}, expected object. "
                f"output_preview={trimmed[:500]!r}"
            )
            return
        if "Content" not in seg:
            job.error_message = (
                f"VibeVoice segment[{i}] missing 'Content' key. "
                f"keys={list(seg.keys())}, "
                f"output_preview={trimmed[:500]!r}"
            )
            return


async def process_groq_job(
    job: TranscriptionJob,
    http_client: httpx.AsyncClient,
    config: Settings,
) -> None:
    """Worker function that processes a job via Groq Whisper API."""
    job.status = JobStatus.STREAMING
    text = await transcribe_audio(
        http_client=http_client,
        api_key=config.groq_api_key,
        model_name=config.groq_model_name,
        audio_base64=job.audio_base64,
        audio_mime=job.audio_mime,
        hotwords=job.hotwords,
    )
    if text:
        # Wrap in the same JSON segment format that VibeVoice produces,
        # so existing clients that parse [{"Start":..,"End":..,"Content":..}] work.
        segment = json.dumps(
            [{"Start": 0, "End": job.audio_duration_seconds, "Content": text}]
        )
        await job.chunk_queue.put(segment)

    # Signal end of stream
    await job.chunk_queue.put(None)
