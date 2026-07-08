import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from server.audio import detect_mime_type, probe_duration_file
from server.auth import verify_token
from server.models import ErrorEvent, JobStatus, QueuePositionEvent, TranscriptionChunkEvent
from server.queue import TranscriptionJob, TranscriptionQueue

router = APIRouter()
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _spool_upload(upload: UploadFile, max_audio_bytes: int) -> tuple[str, int]:
    fd, path = tempfile.mkstemp(prefix="vvv-upload-", suffix=".audio")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_audio_bytes:
                    raise HTTPException(status_code=413, detail="Audio file too large")
                out.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="Empty audio file")
        return path, total
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        raise


@router.post("/v1/transcribe")
async def transcribe(
    request: Request,
    audio: UploadFile,
    token_fingerprint: Annotated[str, Depends(verify_token)],
    hotwords: str | None = None,
) -> StreamingResponse:
    queue: TranscriptionQueue = request.app.state.queue
    max_audio_bytes: int = request.app.state.settings.max_audio_bytes

    if audio.filename is None:
        raise HTTPException(status_code=400, detail="Audio file must include a filename")
    try:
        mime_type = detect_mime_type(audio.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    job = TranscriptionJob(
        token_fingerprint=token_fingerprint,
        audio_mime=mime_type,
        hotwords=hotwords,
    )

    try:
        queue.reserve(job)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="Queue is full") from None

    try:
        audio_path, _ = await _spool_upload(audio, max_audio_bytes)
        job.audio_path = audio_path
        try:
            job.audio_duration_seconds = await probe_duration_file(audio_path)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=f"Cannot read audio: {exc}") from None
        queue.submit(job.job_id)
    except Exception:
        queue.cancel(job.job_id)
        raise

    async def event_stream() -> AsyncIterator[str]:
        terminal = False
        try:
            # Send initial queue position
            position, eta = queue.get_position_and_eta(job.job_id)
            if position is not None:
                assert eta is not None, f"ETA must not be None when position={position} is not None"
                event = QueuePositionEvent(
                    job_id=job.job_id,
                    position=position,
                    estimated_wait_seconds=eta,
                )
                yield f"event: queue\ndata: {event.model_dump_json()}\n\n"

            # Stream transcription chunks while polling for disconnects.
            while True:
                if await request.is_disconnected():
                    queue.cancel(job.job_id)
                    terminal = True
                    return
                try:
                    chunk = await asyncio.wait_for(job.chunk_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if chunk is None:
                    break

                chunk_event = TranscriptionChunkEvent(text=chunk)
                yield f"data: {chunk_event.model_dump_json()}\n\n"

            terminal = True
            if job.status == JobStatus.CANCELLED:
                return
            if job.status == JobStatus.FAILED or job.error_message is not None:
                error_event = ErrorEvent(error=job.error_message or "Transcription failed")
                yield f"event: error\ndata: {error_event.model_dump_json()}\n\n"
            else:
                yield f"event: done\ndata: {json.dumps({'job_id': job.job_id})}\n\n"
        finally:
            if not terminal:
                queue.cancel(job.job_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
