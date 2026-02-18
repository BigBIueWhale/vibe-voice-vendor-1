from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from client.models import EventType, TranscriptionEvent


class VibevoiceClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def transcribe(
        self,
        audio_path: str | Path,
        hotwords: str | None = None,
    ) -> AsyncIterator[TranscriptionEvent]:
        """Upload audio and stream transcription events."""
        path = Path(audio_path)

        timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            with open(path, "rb") as f:
                files = {"audio": (path.name, f, "application/octet-stream")}
                data = {}
                if hotwords:
                    data["hotwords"] = hotwords

                async with client.stream(
                    "POST",
                    f"{self._base_url}/v1/transcribe",
                    headers=self._headers(),
                    files=files,
                    data=data,
                ) as response:
                    response.raise_for_status()
                    current_event = "data"

                    async for line in response.aiter_lines():
                        if line.startswith("event: "):
                            current_event = line[len("event: "):]
                            continue

                        if not line.startswith("data: "):
                            continue

                        data_str = line[len("data: "):]

                        try:
                            payload = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        if current_event == "queue":
                            yield TranscriptionEvent(
                                event_type=EventType.QUEUE,
                                job_id=payload.get("job_id"),
                                position=payload.get("position"),
                                estimated_wait_seconds=payload.get("estimated_wait_seconds"),
                            )
                        elif current_event == "data":
                            yield TranscriptionEvent(
                                event_type=EventType.DATA,
                                text=payload.get("text"),
                            )
                        elif current_event == "error":
                            yield TranscriptionEvent(
                                event_type=EventType.ERROR,
                                error=payload.get("error"),
                            )
                        elif current_event == "done":
                            yield TranscriptionEvent(
                                event_type=EventType.DONE,
                                job_id=payload.get("job_id"),
                            )

                        # Reset to default after processing data line
                        current_event = "data"

    async def queue_status(self) -> dict[str, object]:
        """Get queue status for your token."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self._base_url}/v1/queue/status",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
