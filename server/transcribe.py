import httpx

from server.config import Settings
from server.models import JobStatus
from server.queue import TranscriptionJob
from server.vllm_client import stream_transcription


async def process_transcription_job(
    job: TranscriptionJob,
    http_client: httpx.AsyncClient,
    config: Settings,
) -> None:
    """Worker function that processes a single transcription job."""
    try:
        first_chunk = True
        async for chunk in stream_transcription(
            http_client=http_client,
            vllm_base_url=config.vllm_base_url,
            model_name=config.vllm_model_name,
            audio_base64=job.audio_base64,
            hotwords=job.hotwords,
            max_tokens=config.vllm_max_tokens,
            temperature=config.vllm_temperature,
        ):
            if first_chunk:
                job.status = JobStatus.STREAMING
                first_chunk = True
            await job.chunk_queue.put(chunk)

        # Signal end of stream
        await job.chunk_queue.put(None)
    except Exception:
        job.error_message = "Transcription failed"
        await job.chunk_queue.put(None)
        raise
    finally:
        job.audio_base64 = ""
