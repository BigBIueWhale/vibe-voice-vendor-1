import struct
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport

import server.auth
from server.app import create_app
from server.auth import _load_public_key
from server.config import Settings
from server.queue import TranscriptionQueue

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


def _make_wav(sample_rate: int = 16000, num_samples: int = 16000) -> bytes:
    """Create a minimal valid WAV file."""
    bits_per_sample = 16
    num_channels = 1
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = num_samples * num_channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + b"\x00" * data_size


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    _load_public_key.cache_clear()
    server.auth._revocation_cache = (0.0, frozenset())
    key_file = tmp_path / "public.pem"
    key_file.write_bytes(_PUBLIC_PEM)
    return Settings(
        vllm_base_url="http://127.0.0.1:37845",
        jwt_public_key_file=str(key_file),
        max_queue_size=5,
    )


@asynccontextmanager
async def _lifespan_client(
    settings: Settings,
) -> AsyncIterator[httpx.AsyncClient]:
    """Create an app with lifespan and return an httpx client."""
    app = create_app(settings=settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Manually set up app state that lifespan would create
        app.state.http_client = httpx.AsyncClient()
        app.state.queue = TranscriptionQueue(max_size=settings.max_queue_size)
        app.state.queue.start_worker()
        try:
            yield client
        finally:
            await app.state.queue.stop()
            await app.state.http_client.aclose()


async def test_health_endpoint(settings: Settings) -> None:
    async with _lifespan_client(settings) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["vllm"] == "unreachable"


async def test_transcribe_requires_auth(settings: Settings) -> None:
    async with _lifespan_client(settings) as client:
        resp = await client.post("/v1/transcribe")
        # HTTPBearer returns 403 when no Authorization header at all
        assert resp.status_code in (401, 403)


async def test_queue_status_requires_auth(settings: Settings) -> None:
    async with _lifespan_client(settings) as client:
        resp = await client.get("/v1/queue/status")
        assert resp.status_code in (401, 403)


async def test_queue_status_with_auth(settings: Settings) -> None:
    async with _lifespan_client(settings) as client:
        resp = await client.get(
            "/v1/queue/status",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "your_jobs" in data
        assert "total_queued" in data


async def test_transcribe_empty_audio(settings: Settings) -> None:
    async with _lifespan_client(settings) as client:
        resp = await client.post(
            "/v1/transcribe",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
            files={"audio": ("test.wav", b"", "audio/wav")},
        )
        assert resp.status_code == 400


async def test_https_required_rejects_http(tmp_path: Path) -> None:
    _load_public_key.cache_clear()
    server.auth._revocation_cache = (0.0, frozenset())
    key_file = tmp_path / "public.pem"
    key_file.write_bytes(_PUBLIC_PEM)
    https_settings = Settings(
        vllm_base_url="http://127.0.0.1:37845",
        jwt_public_key_file=str(key_file),
        max_queue_size=5,
        require_https=True,
    )
    async with _lifespan_client(https_settings) as client:
        # No X-Forwarded-Proto header → should be rejected
        resp = await client.get(
            "/v1/queue/status",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert resp.status_code == 403
        assert "HTTPS" in resp.json()["detail"]


async def test_https_required_allows_health(tmp_path: Path) -> None:
    _load_public_key.cache_clear()
    server.auth._revocation_cache = (0.0, frozenset())
    key_file = tmp_path / "public.pem"
    key_file.write_bytes(_PUBLIC_PEM)
    https_settings = Settings(
        vllm_base_url="http://127.0.0.1:37845",
        jwt_public_key_file=str(key_file),
        max_queue_size=5,
        require_https=True,
    )
    async with _lifespan_client(https_settings) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200


async def test_https_required_passes_with_header(tmp_path: Path) -> None:
    _load_public_key.cache_clear()
    server.auth._revocation_cache = (0.0, frozenset())
    key_file = tmp_path / "public.pem"
    key_file.write_bytes(_PUBLIC_PEM)
    https_settings = Settings(
        vllm_base_url="http://127.0.0.1:37845",
        jwt_public_key_file=str(key_file),
        max_queue_size=5,
        require_https=True,
    )
    async with _lifespan_client(https_settings) as client:
        resp = await client.get(
            "/v1/queue/status",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "X-Forwarded-Proto": "https",
            },
        )
        assert resp.status_code == 200
