from enum import StrEnum

from pydantic import BaseModel


class EventType(StrEnum):
    QUEUE = "queue"
    DATA = "data"
    ERROR = "error"
    DONE = "done"


class TranscriptionEvent(BaseModel):
    event_type: EventType
    text: str | None = None
    job_id: str | None = None
    position: int | None = None
    estimated_wait_seconds: float | None = None
    error: str | None = None
