from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "VVV_"}

    vllm_base_url: str = "http://127.0.0.1:37845"
    server_host: str = "127.0.0.1"
    server_port: int = 54912
    max_audio_bytes: int = 500 * 1024 * 1024  # 500 MB
    max_queue_size: int = 50
    jwt_public_key_file: str = ""  # Path to ES256 public key PEM file
    revoked_tokens_file: str = ""  # Path to file listing revoked JTI values (one per line)
    require_https: bool = False  # Reject non-HTTPS requests on protected endpoints
    vllm_model_name: str = "vibevoice"
    vllm_max_tokens: int = 65536
    vllm_temperature: float = 0.0
    vllm_top_p: float = 1.0
