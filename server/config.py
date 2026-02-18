from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "VVV_"}

    vllm_base_url: str = "http://127.0.0.1:8000"
    server_host: str = "127.0.0.1"
    server_port: int = 8080
    max_audio_bytes: int = 500 * 1024 * 1024  # 500 MB
    max_queue_size: int = 50
    token_hashes_env: str = ""  # Comma-separated bcrypt hashes
    vllm_model_name: str = "VibeVoice-ASR-7B"
    vllm_max_tokens: int = 32768
    vllm_temperature: float = 0.0
    vllm_top_p: float = 1.0

    @property
    def token_hashes(self) -> list[str]:
        if not self.token_hashes_env:
            return []
        return [h.strip() for h in self.token_hashes_env.split(",") if h.strip()]
