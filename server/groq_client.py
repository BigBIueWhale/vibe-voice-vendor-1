import base64

import httpx

from server.audio import compress_to_opus


async def transcribe_audio(
    *,
    http_client: httpx.AsyncClient,
    api_key: str,
    model_name: str,
    audio_base64: str,
    audio_mime: str,
    hotwords: str | None,
) -> str:
    """Transcribe audio using Groq's Whisper API (OpenAI-compatible endpoint).

    Audio is compressed to OGG/Opus before upload to stay well within
    Groq's 25 MB file size limit and reduce upload latency.
    Groq handles the 30-second Whisper windowing internally for longer audio.
    """
    raw_bytes = base64.b64decode(audio_base64)
    opus_bytes = await compress_to_opus(raw_bytes)

    files = {"file": ("audio.ogg", opus_bytes, "audio/ogg")}
    data: dict[str, str] = {
        "model": model_name,
        "response_format": "json",
        "temperature": "0",
    }
    if hotwords:
        data["prompt"] = hotwords

    response = await http_client.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files=files,
        data=data,
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Groq API error {response.status_code}: {response.text}"
        )
    result: str = response.json()["text"]
    return result
