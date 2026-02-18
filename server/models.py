from enum import StrEnum

from pydantic import BaseModel


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"


class QueuePositionEvent(BaseModel):
    job_id: str
    position: int
    estimated_wait_seconds: float


class TranscriptionChunkEvent(BaseModel):
    text: str


class ErrorEvent(BaseModel):
    error: str


class JobInfo(BaseModel):
    job_id: str
    status: JobStatus
    position: int | None = None
    estimated_wait_seconds: float | None = None


class QueueStatusResponse(BaseModel):
    your_jobs: list[JobInfo]
    total_queued: int
