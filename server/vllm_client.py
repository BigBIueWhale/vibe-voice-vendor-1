import json
from collections.abc import AsyncIterator

import httpx


async def stream_transcription(
    *,
    http_client: httpx.AsyncClient,
    vllm_base_url: str,
    model_name: str,
    audio_base64: str,
    hotwords: str | None,
    max_tokens: int,
    temperature: float,
    top_p: float,
) -> AsyncIterator[str]:
    """Stream transcription from vLLM via OpenAI-compatible SSE endpoint."""
    audio_url = f"data:audio/wav;base64,{audio_base64}"

    content: list[dict[str, object]] = [
        {"type": "audio_url", "audio_url": {"url": audio_url}},
    ]

    text_prompt = "Transcribe the audio."
    if hotwords:
        text_prompt = f"Transcribe the audio. Hotwords: {hotwords}"
    content.append({"type": "text", "text": text_prompt})

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": True,
    }

    url = f"{vllm_base_url}/v1/chat/completions"

    async with http_client.stream(
        "POST",
        url,
        json=payload,
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0),
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[len("data: ") :]
            if data_str.strip() == "[DONE]":
                return
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            text = delta.get("content")
            if text:
                yield text
