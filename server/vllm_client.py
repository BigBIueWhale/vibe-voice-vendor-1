import json
from collections.abc import AsyncIterator

import httpx


async def stream_transcription(
    *,
    http_client: httpx.AsyncClient,
    vllm_base_url: str,
    model_name: str,
    audio_base64: str,
    audio_mime: str,
    audio_duration: float,
    hotwords: str | None,
    temperature: float,
    top_p: float,
) -> AsyncIterator[str]:
    """Stream transcription from vLLM via OpenAI-compatible SSE endpoint."""
    audio_url = f"data:{audio_mime};base64,{audio_base64}"

    content: list[dict[str, object]] = [
        {"type": "audio_url", "audio_url": {"url": audio_url}},
    ]

    text_prompt = f"This is a {audio_duration:.2f} seconds audio, "
    if hotwords:
        text_prompt += f"with extra info: {hotwords}\n\n"
    text_prompt += (
        "please transcribe it with these keys: "
        "Start time, End time, Speaker ID, Content"
    )
    content.append({"type": "text", "text": text_prompt})

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant that transcribes audio "
                    "input into text output in JSON format."
                ),
            },
            {"role": "user", "content": content},
        ],
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
        if response.status_code != 200:
            body = await response.aread()
            raise RuntimeError(
                f"vLLM error {response.status_code}: {body.decode('utf-8', errors='replace')}"
            )
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[len("data: "):]
            if data_str.strip() == "[DONE]":
                return
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if "choices" not in data or not data["choices"]:
                continue
            choice = data["choices"][0]
            if "delta" not in choice or "content" not in choice["delta"]:
                continue
            text = choice["delta"]["content"]
            if text:
                yield text
