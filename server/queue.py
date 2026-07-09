from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from server.models import JobInfo, JobStatus, QueueStatusResponse

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionJob:
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    client_identity: str = ""
    audio_path: str | None = None
    audio_mime: str = "application/octet-stream"
    hotwords: str | None = None
    audio_duration_seconds: float = 0.0
    status: JobStatus = JobStatus.UPLOADING
    chunk_queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    error_message: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    cancel_requested: bool = False
    process_task: asyncio.Task[None] | None = None
    terminal_notified: bool = False
    cleanup_scheduled: bool = False

    def require_audio_path(self) -> str:
        if self.audio_path is None:
            raise RuntimeError("Job has no audio file")
        return self.audio_path

    def discard_audio(self) -> None:
        if self.audio_path is None:
            return
        path = self.audio_path
        self.audio_path = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


class TranscriptionQueue:
    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: OrderedDict[str, TranscriptionJob] = OrderedDict()
        self._processing_times: list[float] = []
        self._max_history: int = 20
        self._worker_task: asyncio.Task[None] | None = None
        self._process_fn: Callable[[TranscriptionJob], Coroutine[Any, Any, None]] | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    def set_process_fn(self, fn: Callable[[TranscriptionJob], Coroutine[Any, Any, None]]) -> None:
        self._process_fn = fn

    def start_worker(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        for task in list(self._cleanup_tasks):
            task.cancel()
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
            self._cleanup_tasks.clear()

    def reserve(self, job: TranscriptionJob) -> None:
        """Reserve active-job capacity before accepting a large upload."""
        if self._active_count() >= self._max_size:
            raise asyncio.QueueFull
        job.status = JobStatus.UPLOADING
        self._jobs[job.job_id] = job

    def submit(self, job_id: str) -> None:
        job = self._jobs[job_id]
        if job.status != JobStatus.UPLOADING:
            raise RuntimeError(f"Cannot submit job in state {job.status}")
        job.status = JobStatus.QUEUED
        self._pending.put_nowait(job.job_id)

    def enqueue(self, job: TranscriptionJob) -> None:
        """Reserve and submit a ready job. Raises asyncio.QueueFull if at capacity."""
        self.reserve(job)
        self.submit(job.job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            return False

        job.cancel_requested = True
        job.status = JobStatus.CANCELLED
        job.error_message = "Job cancelled"
        if job.process_task and not job.process_task.done():
            job.process_task.cancel()
        job.discard_audio()
        self._notify_terminal(job)
        self._schedule_cleanup(job_id)
        return True

    def get_job(self, job_id: str) -> TranscriptionJob | None:
        return self._jobs.get(job_id)

    def get_queue_info(self, client_identity: str) -> QueueStatusResponse:
        queued_ids = list(self._jobs.keys())
        your_jobs: list[JobInfo] = []
        total_queued = 0

        for job_id in queued_ids:
            job = self._jobs[job_id]
            if job.status == JobStatus.QUEUED:
                total_queued += 1

            if job.client_identity == client_identity:
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

    def _active_count(self) -> int:
        return sum(
            1
            for job in self._jobs.values()
            if job.status
            in {
                JobStatus.UPLOADING,
                JobStatus.QUEUED,
                JobStatus.PROCESSING,
                JobStatus.STREAMING,
            }
        )

    async def _worker(self) -> None:
        while True:
            job_id = await self._pending.get()
            job = self._jobs.get(job_id)
            if job is None or job.status == JobStatus.CANCELLED:
                continue

            job.status = JobStatus.PROCESSING
            start_time = time.monotonic()

            try:
                if self._process_fn:
                    job.process_task = asyncio.create_task(self._process_fn(job))
                    await job.process_task
                else:
                    pass
                if job.error_message is not None:
                    job.status = JobStatus.FAILED
                else:
                    job.status = JobStatus.COMPLETED
            except asyncio.CancelledError:
                if job.cancel_requested:
                    job.status = JobStatus.CANCELLED
                    job.error_message = "Job cancelled"
                else:
                    if job.process_task and not job.process_task.done():
                        job.process_task.cancel()
                        try:
                            await job.process_task
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            logger.debug(
                                "Job %s raised during queue shutdown: %s",
                                job.job_id[:8],
                                exc,
                            )
                    job.status = JobStatus.CANCELLED
                    job.error_message = "Queue stopped"
                    raise
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error_message = str(exc) or type(exc).__name__
                logger.warning("Job %s failed: %s", job.job_id[:8], job.error_message)
            finally:
                elapsed = time.monotonic() - start_time
                if job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                    self._processing_times.append(elapsed)
                    if len(self._processing_times) > self._max_history:
                        self._processing_times.pop(0)

                job.process_task = None
                job.discard_audio()
                self._notify_terminal(job)
                self._schedule_cleanup(job_id)

    def _notify_terminal(self, job: TranscriptionJob) -> None:
        if job.terminal_notified:
            return
        job.terminal_notified = True
        job.chunk_queue.put_nowait(None)

    def _schedule_cleanup(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.cleanup_scheduled:
            return
        job.cleanup_scheduled = True
        task = asyncio.create_task(self._cleanup_job(job_id))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _cleanup_job(self, job_id: str) -> None:
        await asyncio.sleep(30)
        job = self._jobs.pop(job_id, None)
        if job is not None:
            job.discard_audio()
