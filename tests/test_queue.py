import asyncio

import pytest

from server.models import JobStatus
from server.queue import TranscriptionJob, TranscriptionQueue


@pytest.fixture
def queue() -> TranscriptionQueue:
    return TranscriptionQueue(max_size=5)


async def test_enqueue_and_position(queue: TranscriptionQueue) -> None:
    job1 = TranscriptionJob(token_fingerprint="user1111")
    job2 = TranscriptionJob(token_fingerprint="user2222")

    await queue.enqueue(job1)
    await queue.enqueue(job2)

    pos1, _ = queue.get_position_and_eta(job1.job_id)
    pos2, _ = queue.get_position_and_eta(job2.job_id)

    assert pos1 == 1
    assert pos2 == 2


async def test_queue_info_filters_by_fingerprint(queue: TranscriptionQueue) -> None:
    job1 = TranscriptionJob(token_fingerprint="user1111")
    job2 = TranscriptionJob(token_fingerprint="user2222")

    await queue.enqueue(job1)
    await queue.enqueue(job2)

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

    job = TranscriptionJob(token_fingerprint="user1111", audio_base64="test_data")
    await queue.enqueue(job)

    # Wait for processing
    chunk = await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    assert chunk == "hello"
    sentinel = await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    assert sentinel is None
    assert job.job_id in processed

    await queue.stop()


async def test_worker_clears_audio_after_processing(queue: TranscriptionQueue) -> None:
    async def mock_process(job: TranscriptionJob) -> None:
        await job.chunk_queue.put(None)

    queue.set_process_fn(mock_process)
    queue.start_worker()

    job = TranscriptionJob(token_fingerprint="user1111", audio_base64="big_audio_data")
    await queue.enqueue(job)

    await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    # Give worker time to clean up
    await asyncio.sleep(0.1)
    assert job.audio_base64 == ""

    await queue.stop()


async def test_eta_estimation(queue: TranscriptionQueue) -> None:
    # With no history, default is 30s per job
    job = TranscriptionJob(token_fingerprint="user1111")
    await queue.enqueue(job)

    _, eta = queue.get_position_and_eta(job.job_id)
    assert eta == 30.0


async def test_failed_job_sends_sentinel(queue: TranscriptionQueue) -> None:
    async def failing_process(job: TranscriptionJob) -> None:
        raise RuntimeError("test failure")

    queue.set_process_fn(failing_process)
    queue.start_worker()

    job = TranscriptionJob(token_fingerprint="user1111")
    await queue.enqueue(job)

    sentinel = await asyncio.wait_for(job.chunk_queue.get(), timeout=2.0)
    assert sentinel is None
    # Give worker time to update status
    await asyncio.sleep(0.1)
    assert job.status == JobStatus.FAILED

    await queue.stop()
