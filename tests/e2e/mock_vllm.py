"""Mock vLLM server that returns fixed VibeVoice JSON segments for E2E testing."""

import argparse
import asyncio
import json
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

MOCK_TRANSCRIPTION = (
    '[{"Start":0,"End":1,"Content":"Hello, this is a test of the '
    'VibeVoice transcription system."}]'
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions() -> StreamingResponse:
    chunks = [
        MOCK_TRANSCRIPTION[:26],
        MOCK_TRANSCRIPTION[26:52],
        MOCK_TRANSCRIPTION[52:],
    ]

    async def generate() -> AsyncIterator[str]:
        for i, text in enumerate(chunks):
            chunk = {
                "id": f"chatcmpl-{i}",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.01)

        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock vLLM server for E2E testing")
    parser.add_argument("--host", required=True, help="Bind address")
    parser.add_argument("--port", type=int, required=True, help="Bind port")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
