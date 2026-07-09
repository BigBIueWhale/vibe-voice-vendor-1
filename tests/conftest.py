import uuid
from pathlib import Path

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import server.auth
from server.app import create_app
from server.auth import _load_public_key
from server.config import Settings

_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_PUBLIC_PEM = _PRIVATE_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)

TEST_TOKEN = pyjwt.encode(
    {"sub": "test-user", "jti": uuid.uuid4().hex},
    _PRIVATE_KEY,
    algorithm="ES256",
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    _load_public_key.cache_clear()
    server.auth._revocation_cache = (0.0, frozenset())
    key_file = tmp_path / "public.pem"
    key_file.write_bytes(_PUBLIC_PEM)
    revoked_file = tmp_path / "revoked.txt"
    revoked_file.write_text("")
    return Settings(
        asr_backend="vibevoice",
        vllm_base_url="http://127.0.0.1:9999",
        max_audio_bytes=500 * 1024 * 1024,
        max_queue_size=5,
        jwt_public_key_file=str(key_file),
        revoked_tokens_file=str(revoked_file),
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
