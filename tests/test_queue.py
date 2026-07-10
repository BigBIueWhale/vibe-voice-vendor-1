import asyncio
from pathlib import Path

import pytest

from server.models import JobStatus
from server.queue import TranscriptionJob, TranscriptionQueue


@pytest.fixture
def queue() -> TranscriptionQueue:
    return TranscriptionQueue(max_size=5)


async def test_enqueue_and_position(queue: TranscriptionQueue) -> None:
    job1 = TranscriptionJob(client_identity="user1111")
    job2 = TranscriptionJob(client_identity="user2222")

    queue.enqueue(job1)
    queue.enqueue(job2)

    pos1, _ = queue.get_position_and_eta(job1.job_id)
    pos2, _ = queue.get_position_and_eta(job2.job_id)

    assert pos1 == 1
    assert pos2 == 2


async def test_queue_info_filters_by_client_identity(queue: TranscriptionQueue) -> None:
    job1 = TranscriptionJob(client_identity="user1111")
    job2 = TranscriptionJob(client_identity="user2222")

    queue.enqueue(job1)
    queue.enqueue(job2)

    info = queue.get_queue_info("user1111")
    assert len(info.your_jobs) == 1
    assert info.your_jobs[0].job_id == job1.job_id
    assert info.total_queued == 2


async def test_worker_processes_job(queue: TranscriptionQueue) -> None:
    processed: list[str] = []

    async def mock_process(job: TranscriptionJob) -> None:
        processed.append(job.job_id)
        await job.chunk_queue.put("hello")
        await job.chunk_queue.put(None)

    queue.set_process_fn(mock_process)
    queue.start_worker()

    job = TranscriptionJob(client_identity="user1111")
    queue.enqueue(job)

    # Wait for processing
    chunk = await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    assert chunk == "hello"
    sentinel = await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    assert sentinel is None
    assert job.job_id in processed

    await queue.stop()


async def test_worker_clears_audio_after_processing(
    queue: TranscriptionQueue,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "upload.audio"
    audio_path.write_text("big_audio_data")

    async def mock_process(job: TranscriptionJob) -> None:
        assert job.require_audio_path() == str(audio_path)

    queue.set_process_fn(mock_process)
    queue.start_worker()

    job = TranscriptionJob(client_identity="user1111", audio_path=str(audio_path))
    queue.enqueue(job)

    await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    # Give worker time to clean up
    await asyncio.sleep(0.1)
    assert job.audio_path is None
    assert not audio_path.exists()

    await queue.stop()


def test_discard_audio_removes_private_upload_dir(tmp_path: Path) -> None:
    upload_dir = tmp_path / "vvv-upload-test"
    upload_dir.mkdir()
    audio_path = upload_dir / "audio.audio"
    audio_path.write_text("big_audio_data")
    job = TranscriptionJob(audio_path=str(audio_path))

    job.discard_audio()

    assert job.audio_path is None
    assert not audio_path.exists()
    assert not upload_dir.exists()


async def test_eta_estimation(queue: TranscriptionQueue) -> None:
    # With no history, default is 30s per job
    job = TranscriptionJob(client_identity="user1111")
    queue.enqueue(job)

    _, eta = queue.get_position_and_eta(job.job_id)
    assert eta == 30.0


async def test_failed_job_sends_sentinel(queue: TranscriptionQueue) -> None:
    async def failing_process(job: TranscriptionJob) -> None:
        raise RuntimeError("test failure")

    queue.set_process_fn(failing_process)
    queue.start_worker()

    job = TranscriptionJob(client_identity="user1111")
    queue.enqueue(job)

    sentinel = await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    assert sentinel is None
    # Give worker time to update status
    await asyncio.sleep(0.1)
    assert job.status == JobStatus.FAILED

    await queue.stop()


async def test_capacity_counts_uploading_jobs(queue: TranscriptionQueue) -> None:
    small_queue = TranscriptionQueue(max_size=1)
    small_queue.reserve(TranscriptionJob(client_identity="user1111"))
    with pytest.raises(asyncio.QueueFull):
        small_queue.reserve(TranscriptionJob(client_identity="user2222"))


async def test_cancel_queued_job_skips_processing(queue: TranscriptionQueue) -> None:
    processed: list[str] = []

    async def mock_process(job: TranscriptionJob) -> None:
        processed.append(job.job_id)

    job = TranscriptionJob(client_identity="user1111")
    queue.enqueue(job)
    assert queue.cancel(job.job_id)
    queue.set_process_fn(mock_process)
    queue.start_worker()

    sentinel = await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    assert sentinel is None
    await asyncio.sleep(0.1)
    assert processed == []
    assert job.status == JobStatus.CANCELLED

    await queue.stop()


async def test_cancel_processing_job_has_single_terminal_notification(
    queue: TranscriptionQueue,
) -> None:
    started = asyncio.Event()

    async def mock_process(job: TranscriptionJob) -> None:
        started.set()
        await asyncio.Event().wait()

    queue.set_process_fn(mock_process)
    queue.start_worker()

    job = TranscriptionJob(client_identity="user1111")
    queue.enqueue(job)
    await asyncio.wait_for(started.wait(), timeout=2.0)

    assert queue.cancel(job.job_id)
    sentinel = await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    assert sentinel is None
    await asyncio.sleep(0.1)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(job.chunk_queue.get(), timeout=0.05)
    assert job.status == JobStatus.CANCELLED
    assert job.cleanup_scheduled

    await queue.stop()


async def test_stop_cancels_active_process_task(queue: TranscriptionQueue) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def mock_process(job: TranscriptionJob) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    queue.set_process_fn(mock_process)
    queue.start_worker()

    job = TranscriptionJob(client_identity="user1111")
    queue.enqueue(job)
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await queue.stop()

    assert cancelled.is_set()
    assert job.status == JobStatus.CANCELLED
    assert job.error_message == "Queue stopped"
