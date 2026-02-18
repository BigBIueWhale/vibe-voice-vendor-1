from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from server.models import JobInfo, JobStatus, QueueStatusResponse

logger = logging.getLogger(__name__)

# Sentinel to signal end-of-stream
STREAM_END: None = None


@dataclass
class TranscriptionJob:
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    token_fingerprint: str = ""
    audio_base64: str = ""
    hotwords: str | None = None
    audio_duration_seconds: float = 0.0
    status: JobStatus = JobStatus.QUEUED
    chunk_queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    error_message: str | None = None
    created_at: float = field(default_factory=time.monotonic)


class TranscriptionQueue:
    def __init__(self, max_size: int = 50) -> None:
        self._pending: asyncio.Queue[str] = asyncio.Queue(maxsize=max_size)
        self._jobs: OrderedDict[str, TranscriptionJob] = OrderedDict()
        self._processing_times: list[float] = []
        self._max_history: int = 20
        self._worker_task: asyncio.Task[None] | None = None
        self._process_fn: Callable[[TranscriptionJob], Coroutine[Any, Any, None]] | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    def set_process_fn(
        self, fn: Callable[[TranscriptionJob], Coroutine[Any, Any, None]]
    ) -> None:
        self._process_fn = fn

    def start_worker(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task

    async def enqueue(self, job: TranscriptionJob) -> None:
        self._jobs[job.job_id] = job
        await self._pending.put(job.job_id)

    def get_job(self, job_id: str) -> TranscriptionJob | None:
        return self._jobs.get(job_id)

    def get_queue_info(self, token_fingerprint: str) -> QueueStatusResponse:
        queued_ids = list(self._jobs.keys())
        your_jobs: list[JobInfo] = []
        total_queued = 0

        for job_id in queued_ids:
            job = self._jobs[job_id]
            if job.status == JobStatus.QUEUED:
                total_queued += 1

            if job.token_fingerprint == token_fingerprint:
                position = self._get_position(job_id)
                eta = self._estimate_wait(position) if position is not None else None
                your_jobs.append(
                    JobInfo(
                        job_id=job.job_id,
                        status=job.status,
                        position=position,
                        estimated_wait_seconds=eta,
                    )
                )

        return QueueStatusResponse(your_jobs=your_jobs, total_queued=total_queued)

    def get_position_and_eta(self, job_id: str) -> tuple[int | None, float | None]:
        position = self._get_position(job_id)
        eta = self._estimate_wait(position) if position is not None else None
        return position, eta

    def _get_position(self, job_id: str) -> int | None:
        position = 0
        for jid, job in self._jobs.items():
            if job.status == JobStatus.QUEUED:
                position += 1
                if jid == job_id:
                    return position
        return None

    def _estimate_wait(self, position: int) -> float:
        if not self._processing_times:
            return position * 30.0  # Default 30s per job
        avg_time = sum(self._processing_times) / len(self._processing_times)
        return position * avg_time

    async def _worker(self) -> None:
        while True:
            job_id = await self._pending.get()
            job = self._jobs.get(job_id)
            if job is None:
                continue

            job.status = JobStatus.PROCESSING
            start_time = time.monotonic()

            try:
                if self._process_fn:
                    await self._process_fn(job)
                job.status = JobStatus.COMPLETED
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error_message = str(exc)
                # Signal error to waiting client
                await job.chunk_queue.put(None)
                logger.warning("Job %s failed: %s", job.job_id[:8], exc)
            finally:
                elapsed = time.monotonic() - start_time
                self._processing_times.append(elapsed)
                if len(self._processing_times) > self._max_history:
                    self._processing_times.pop(0)

                # Clear audio data immediately
                job.audio_base64 = ""

                # Schedule cleanup (store reference to prevent GC)
                task = asyncio.create_task(self._cleanup_job(job_id))
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)

    async def _cleanup_job(self, job_id: str) -> None:
        await asyncio.sleep(30)
        self._jobs.pop(job_id, None)
