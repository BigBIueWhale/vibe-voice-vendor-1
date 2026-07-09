import math

import pytest
from pydantic import ValidationError

from server.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "asr_backend": "vibevoice",
        "max_audio_bytes": 1024,
        "max_queue_size": 1,
        "require_https": True,
        "vllm_base_url": "http://127.0.0.1:8000",
        "vllm_model_name": "vibevoice",
        "vllm_temperature": 0.0,
        "vllm_top_p": 1.0,
        "groq_api_key": "",
        "groq_model_name": "whisper-large-v3",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -0.1, 2.0])
def test_vllm_temperature_rejects_values_that_can_crash_vllm(value: float) -> None:
    with pytest.raises(ValidationError, match="vLLM temperature"):
        _settings(vllm_temperature=value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 0.0, 1.01])
def test_vllm_top_p_rejects_invalid_values(value: float) -> None:
    with pytest.raises(ValidationError, match="vLLM top_p"):
        _settings(vllm_top_p=value)


@pytest.mark.parametrize("field", ["max_audio_bytes", "max_queue_size"])
def test_positive_integer_limits_are_required(field: str) -> None:
    with pytest.raises(ValidationError, match="must be >= 1"):
        _settings(**{field: 0})
