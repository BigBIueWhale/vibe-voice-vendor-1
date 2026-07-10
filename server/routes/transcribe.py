import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from server.audio import read_heliboard_wav_info, validate_heliboard_wav_metadata
from server.client_identity import require_client_identity
from server.models import ErrorEvent, JobStatus, QueuePositionEvent, TranscriptionChunkEvent
from server.queue import TranscriptionJob, TranscriptionQueue

router = APIRouter()
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_MULTIPART_FILES = 1
_MAX_MULTIPART_FIELDS = 1
_MAX_FORM_FIELD_BYTES = 16 * 1024
_MAX_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_REQUEST_BODY_CHUNK_TIMEOUT_SECONDS = 180.0
_SSE_CHUNK_WAIT_SECONDS = 1.0
_SSE_KEEPALIVE_SECONDS = 5.0
_CONTROL_TOKEN_RE = re.compile(r"<\|[^>\r\n]{1,128}\|>")
_UPLOAD_DIR_PREFIX = "vvv-upload-"
_UPLOAD_FILE_NAME = "audio.audio"


class _CloseableFile(Protocol):
    def close(self) -> object: ...


def _close_multipart_parser_files(parser: MultiPartParser) -> None:
    # Starlette closes these files for parser errors, but asyncio cancellation is
    # a BaseException and bypasses that cleanup path.
    files = cast(Iterable[_CloseableFile], getattr(parser, "_files_to_close_on_error", ()))
    for file in files:
        with contextlib.suppress(Exception):
            file.close()


async def _spool_upload(upload: UploadFile, max_audio_bytes: int) -> tuple[str, int]:
    upload_dir = tempfile.mkdtemp(prefix=_UPLOAD_DIR_PREFIX)
    path = os.path.join(upload_dir, _UPLOAD_FILE_NAME)
    total = 0
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
    except asyncio.CancelledError:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise


async def _limited_request_stream(
    request: Request,
    max_body_bytes: int,
) -> AsyncGenerator[bytes, None]:
    total = 0
    stream = request.stream()
    stream_iter = stream.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(
                anext(stream_iter),
                timeout=_REQUEST_BODY_CHUNK_TIMEOUT_SECONDS,
            )
        except StopAsyncIteration:
            return
        except TimeoutError:
            with contextlib.suppress(Exception):
                await stream.aclose()
            raise MultiPartException("Request body stalled") from None

        total += len(chunk)
        if total > max_body_bytes:
            raise MultiPartException("Request body too large")
        yield chunk


async def _parse_transcribe_form(request: Request) -> FormData:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise HTTPException(status_code=400, detail="multipart/form-data required")

    max_body_bytes = request.app.state.settings.max_audio_bytes + _MAX_MULTIPART_OVERHEAD_BYTES
    parser = MultiPartParser(
        request.headers,
        _limited_request_stream(request, max_body_bytes),
        max_files=_MAX_MULTIPART_FILES,
        max_fields=_MAX_MULTIPART_FIELDS,
        max_part_size=_MAX_FORM_FIELD_BYTES,
    )
    try:
        return await parser.parse()
    except asyncio.CancelledError:
        _close_multipart_parser_files(parser)
        raise
    except MultiPartException as exc:
        if exc.message == "Request body too large":
            status_code = 413
        elif exc.message == "Request body stalled":
            status_code = 408
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=exc.message) from None
    except OSError:
        raise HTTPException(status_code=507, detail="Temporary upload storage exhausted") from None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid multipart body") from None


def _get_audio_upload(form: FormData) -> UploadFile:
    audio = form.get("audio")
    if not isinstance(audio, UploadFile):
        raise HTTPException(status_code=400, detail="Audio file is required")
    return audio


def _validate_hotwords_value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="hotwords must be a text field")
    if _CONTROL_TOKEN_RE.search(value):
        raise HTTPException(status_code=400, detail="hotwords contain reserved control tokens")
    return value


def _get_hotwords(form: FormData) -> str | None:
    return _validate_hotwords_value(form.get("hotwords"))


def _extract_transcribe_form(form: FormData) -> tuple[UploadFile, str | None]:
    audio: UploadFile | None = None
    hotwords: str | None = None
    seen_audio = False
    seen_hotwords = False

    for name, value in form.multi_items():
        if name == "audio":
            if seen_audio:
                raise HTTPException(status_code=400, detail="Duplicate audio field")
            seen_audio = True
            if not isinstance(value, UploadFile):
                raise HTTPException(status_code=400, detail="audio must be a file field")
            audio = value
        elif name == "hotwords":
            if seen_hotwords:
                raise HTTPException(status_code=400, detail="Duplicate hotwords field")
            seen_hotwords = True
            hotwords = _validate_hotwords_value(value)
        else:
            raise HTTPException(status_code=400, detail=f"Unexpected form field: {name}")

    if audio is None:
        raise HTTPException(status_code=400, detail="Audio file is required")
    return audio, hotwords


@router.post("/v1/transcribe")
async def transcribe(
    request: Request,
    client_identity: Annotated[str, Depends(require_client_identity)],
) -> StreamingResponse:
    # Keep client identity ahead of multipart parsing. FastAPI parses body
    # parameters before dependencies, so this route parses the form manually only
    # after the proxy-provided mTLS client identity is present.
    queue: TranscriptionQueue = request.app.state.queue
    job = TranscriptionJob(client_identity=client_identity)
    try:
        queue.reserve(job)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="Queue is full") from None

    form: FormData | None = None
    response_ready = False
    try:
        form = await _parse_transcribe_form(request)
        audio, hotwords = _extract_transcribe_form(form)

        response = await _transcribe_authenticated(request, audio, job, hotwords)
        response_ready = True
        return response
    finally:
        if form is not None:
            await form.close()
        if not response_ready:
            queue.cancel(job.job_id)


async def _transcribe_authenticated(
    request: Request,
    audio: UploadFile,
    job: TranscriptionJob,
    hotwords: str | None,
) -> StreamingResponse:
    queue: TranscriptionQueue = request.app.state.queue
    max_audio_bytes: int = request.app.state.settings.max_audio_bytes

    try:
        mime_type = validate_heliboard_wav_metadata(audio.filename, audio.content_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    job.audio_mime = mime_type
    job.hotwords = hotwords

    try:
        try:
            audio_path, _ = await _spool_upload(audio, max_audio_bytes)
        except OSError:
            raise HTTPException(
                status_code=507,
                detail="Temporary upload storage exhausted",
            ) from None
        job.audio_path = audio_path
        try:
            job.audio_duration_seconds = read_heliboard_wav_info(audio_path).duration_seconds
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid HeliBoard WAV: {exc}") from None
        queue.submit(job.job_id)
    except Exception:
        queue.cancel(job.job_id)
        raise

    async def event_stream() -> AsyncIterator[str]:
        terminal = False
        last_sent = time.monotonic()
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
                last_sent = time.monotonic()
            else:
                yield ": accepted\n\n"
                last_sent = time.monotonic()

            # Stream transcription chunks while polling for disconnects.
            while True:
                if await request.is_disconnected():
                    queue.cancel(job.job_id)
                    terminal = True
                    return
                try:
                    chunk = await asyncio.wait_for(
                        job.chunk_queue.get(),
                        timeout=_SSE_CHUNK_WAIT_SECONDS,
                    )
                except TimeoutError:
                    if time.monotonic() - last_sent >= _SSE_KEEPALIVE_SECONDS:
                        yield ": keepalive\n\n"
                        last_sent = time.monotonic()
                    continue
                if chunk is None:
                    break

                chunk_event = TranscriptionChunkEvent(text=chunk)
                yield f"data: {chunk_event.model_dump_json()}\n\n"
                last_sent = time.monotonic()

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
