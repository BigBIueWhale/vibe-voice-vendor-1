from pathlib import Path

import pytest

from server.app import create_app
from server.config import Settings

TEST_CLIENT_IDENTITY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    _ = tmp_path
    return Settings(
        asr_backend="vibevoice",
        vllm_base_url="http://127.0.0.1:9999",
        max_audio_bytes=500 * 1024 * 1024,
        max_queue_size=5,
        require_https=False,
        vllm_model_name="vibevoice",
        vllm_temperature=0.0,
        vllm_top_p=1.0,
        groq_api_key="",
        groq_model_name="whisper-large-v3",
    )


@pytest.fixture
def app(settings: Settings):  # type: ignore[no-untyped-def]
    return create_app(settings=settings)
