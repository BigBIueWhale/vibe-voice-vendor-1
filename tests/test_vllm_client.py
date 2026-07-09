import json
from typing import Any

import httpx

from server.vllm_client import stream_transcription


async def test_vllm_request_shape_does_not_forward_unbounded_openai_params() -> None:
    captured_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://vllm") as client:
        chunks = [
            chunk
            async for chunk in stream_transcription(
                http_client=client,
                vllm_base_url="http://vllm",
                model_name="vibevoice",
                audio_base64="AAAA",
                audio_mime="audio/wav",
                audio_duration=1.0,
                hotwords="ordinary words",
                temperature=0.0,
                top_p=1.0,
            )
        ]

    assert chunks == ["ok"]
    assert "n" not in captured_payload
    assert "best_of" not in captured_payload
    assert "use_beam_search" not in captured_payload
    assert captured_payload["temperature"] == 0.0
    assert captured_payload["top_p"] == 1.0
    assert captured_payload["stream"] is True
