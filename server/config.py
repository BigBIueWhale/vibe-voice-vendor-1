import math
from typing import Literal

from pydantic import BaseModel, field_validator


class Settings(BaseModel):
    asr_backend: Literal["vibevoice", "groq"]
    max_audio_bytes: int
    max_queue_size: int
    # vLLM / VibeVoice settings (used when asr_backend == "vibevoice")
    vllm_base_url: str
    vllm_model_name: str
    vllm_temperature: float
    vllm_top_p: float
    # Groq Whisper settings (used when asr_backend == "groq")
    groq_api_key: str
    groq_model_name: str

    @field_validator("max_audio_bytes", "max_queue_size")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @field_validator("vllm_temperature")
    @classmethod
    def _valid_temperature(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0.0 or value >= 2.0:
            raise ValueError("vLLM temperature must be finite and in [0.0, 2.0)")
        return value

    @field_validator("vllm_top_p")
    @classmethod
    def _valid_top_p(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0.0 or value > 1.0:
            raise ValueError("vLLM top_p must be finite and in (0.0, 1.0]")
        return value
