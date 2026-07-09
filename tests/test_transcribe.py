import asyncio
import struct
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

import server.auth
import server.routes.transcribe as transcribe_route
from server.app import RequireHTTPSMiddleware, create_app
from server.auth import _load_public_key
from server.config import Settings
from server.queue import TranscriptionJob, TranscriptionQueue

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


def _iter_original_routes(routes: Iterable[Any]) -> Iterator[Any]:
    for route in routes:
        effective_candidates = getattr(route, "effective_candidates", None)
        if callable(effective_candidates):
            yield from _iter_original_routes(effective_candidates())
            continue
        yield getattr(route, "original_route", route)


def _make_wav(sample_rate: int, num_samples: int) -> bytes:
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


def _make_all_settings(tmp_path: Path, **overrides: object) -> Settings:
    """Create Settings with all required fields explicitly specified."""
    _load_public_key.cache_clear()
    server.auth._revocation_cache = (0.0, frozenset())
    key_file = tmp_path / "public.pem"
    key_file.write_bytes(_PUBLIC_PEM)
    revoked_file = tmp_path / "revoked.txt"
    revoked_file.write_text("")
    values: dict[str, object] = {
        "asr_backend": "vibevoice",
        "vllm_base_url": "http://127.0.0.1:37845",
        "max_audio_bytes": 500 * 1024 * 1024,
        "max_queue_size": 5,
        "jwt_public_key_file": str(key_file),
        "revoked_tokens_file": str(revoked_file),
        "require_https": False,
        "vllm_model_name": "vibevoice",
        "vllm_temperature": 0.0,
        "vllm_top_p": 1.0,
        "groq_api_key": "",
        "groq_model_name": "whisper-large-v3",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return _make_all_settings(tmp_path)


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


async def test_health_endpoint(tmp_path: Path) -> None:
    # Use an unreachable port so vLLM health check fails
    s = _make_all_settings(tmp_path, vllm_base_url="http://127.0.0.1:1")
    async with _lifespan_client(s) as client:
        resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["vllm"] == "unreachable"


async def test_backend_http_client_ignores_ambient_proxy_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/not-the-server-ca.pem")
    app = create_app(settings=_make_all_settings(tmp_path))

    async with app.router.lifespan_context(app):
        http_client: httpx.AsyncClient = app.state.http_client
        assert http_client.trust_env is False
        assert http_client.follow_redirects is False
        assert http_client.timeout.connect == 10.0
        assert http_client.timeout.read == 600.0


def test_transcribe_route_has_no_fastapi_body_field(settings: Settings) -> None:
    app = create_app(settings=settings)
    route = next(
        route
        for route in _iter_original_routes(app.routes)
        if isinstance(route, APIRoute) and route.path == "/v1/transcribe"
    )

    assert route.body_field is None
    assert route.dependant.body_params == []


async def test_transcribe_requires_auth(settings: Settings) -> None:
    async with _lifespan_client(settings) as client:
        resp = await client.post("/v1/transcribe")
        # HTTPBearer returns 403 when no Authorization header at all
        assert resp.status_code in (401, 403)


def test_hotwords_reject_reserved_control_tokens() -> None:
    with pytest.raises(HTTPException) as excinfo:
        transcribe_route._get_hotwords(FormData({"hotwords": "meeting <|AUDIO|> notes"}))

    assert excinfo.value.status_code == 400
    assert "reserved control tokens" in excinfo.value.detail


def test_hotwords_accept_ordinary_text() -> None:
    assert (
        transcribe_route._get_hotwords(FormData({"hotwords": "names: Alice, Bob"}))
        == "names: Alice, Bob"
    )


async def test_transcribe_invalid_token_does_not_parse_multipart(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_called = False

    async def fail_if_form_is_parsed(request: Request) -> FormData:
        nonlocal parser_called
        parser_called = True
        raise AssertionError("multipart form parsed before authentication failed")

    monkeypatch.setattr(transcribe_route, "_parse_transcribe_form", fail_if_form_is_parsed)

    async with _lifespan_client(settings) as client:
        resp = await client.post(
            "/v1/transcribe",
            headers={"Authorization": "Bearer not-a-valid-jwt"},
            files={"audio": ("test.wav", b"not audio", "audio/wav")},
        )

    assert resp.status_code == 401
    assert not parser_called


async def test_transcribe_body_limit_runs_before_spool_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_called = False

    async def fail_if_spool_is_called(upload: UploadFile, max_audio_bytes: int) -> tuple[str, int]:
        nonlocal spool_called
        spool_called = True
        raise AssertionError("spool_upload called after parser body limit should have failed")

    monkeypatch.setattr(transcribe_route, "_spool_upload", fail_if_spool_is_called)
    s = _make_all_settings(tmp_path, max_audio_bytes=1)
    oversized = b"x" * (1024 * 1024 + 2)

    async with _lifespan_client(s) as client:
        resp = await client.post(
            "/v1/transcribe",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
            files={"audio": ("test.wav", oversized, "audio/wav")},
        )

    assert resp.status_code == 413
    assert not spool_called


async def test_transcribe_queue_full_does_not_parse_multipart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_called = False

    async def fail_if_form_is_parsed(request: Request) -> FormData:
        nonlocal parser_called
        parser_called = True
        raise AssertionError("multipart form parsed despite full queue")

    monkeypatch.setattr(transcribe_route, "_parse_transcribe_form", fail_if_form_is_parsed)
    s = _make_all_settings(tmp_path, max_queue_size=1)
    app = create_app(settings=s)
    app.state.http_client = httpx.AsyncClient()
    app.state.queue = TranscriptionQueue(max_size=s.max_queue_size)
    app.state.queue.reserve(TranscriptionJob(token_fingerprint="already-uploading"))
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/transcribe",
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                files={"audio": ("test.wav", b"not audio", "audio/wav")},
            )
    finally:
        await app.state.queue.stop()
        await app.state.http_client.aclose()

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Queue is full"
    assert not parser_called


async def test_transcribe_parse_failure_releases_reserved_upload_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def reject_form(request: Request) -> FormData:
        nonlocal calls
        calls += 1
        raise HTTPException(status_code=400, detail="bad multipart")

    monkeypatch.setattr(transcribe_route, "_parse_transcribe_form", reject_form)
    s = _make_all_settings(tmp_path, max_queue_size=1)

    async with _lifespan_client(s) as client:
        for _ in range(2):
            resp = await client.post(
                "/v1/transcribe",
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                files={"audio": ("test.wav", b"not audio", "audio/wav")},
            )
            assert resp.status_code == 400
            assert resp.json()["detail"] == "bad multipart"

    assert calls == 2


async def test_transcribe_parser_storage_error_releases_reserved_upload_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fail_parse(self: object) -> FormData:
        nonlocal calls
        calls += 1
        raise OSError("no space left")

    monkeypatch.setattr("server.routes.transcribe.MultiPartParser.parse", fail_parse)
    s = _make_all_settings(tmp_path, max_queue_size=1)

    async with _lifespan_client(s) as client:
        for _ in range(2):
            resp = await client.post(
                "/v1/transcribe",
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                files={"audio": ("test.wav", b"not audio", "audio/wav")},
            )
            assert resp.status_code == 507
            assert resp.json()["detail"] == "Temporary upload storage exhausted"

    assert calls == 2


async def test_transcribe_spool_storage_error_releases_reserved_upload_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fail_spool(upload: UploadFile, max_audio_bytes: int) -> tuple[str, int]:
        nonlocal calls
        calls += 1
        raise OSError("no space left")

    monkeypatch.setattr(transcribe_route, "_spool_upload", fail_spool)
    s = _make_all_settings(tmp_path, max_queue_size=1)

    async with _lifespan_client(s) as client:
        for _ in range(2):
            resp = await client.post(
                "/v1/transcribe",
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                files={"audio": ("test.wav", b"not audio", "audio/wav")},
            )
            assert resp.status_code == 507
            assert resp.json()["detail"] == "Temporary upload storage exhausted"

    assert calls == 2


async def test_transcribe_malformed_multipart_parser_error_is_400(
    tmp_path: Path,
) -> None:
    s = _make_all_settings(tmp_path, max_queue_size=1)
    boundary = "vvvboundary"
    oversized_header_name = "x" * (5 * 1024)
    body = (
        f"--{boundary}\r\n"
        f"{oversized_header_name}: y\r\n"
        "\r\n"
        "payload\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    async with _lifespan_client(s) as client:
        for _ in range(2):
            resp = await client.post(
                "/v1/transcribe",
                headers={
                    "Authorization": f"Bearer {TEST_TOKEN}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                content=body,
            )
            assert resp.status_code == 400
            assert resp.json()["detail"] == "Invalid multipart body"


class _StalledRequest:
    def __init__(self) -> None:
        self.closed = False

    async def _stream(self) -> AsyncIterator[bytes]:
        try:
            await asyncio.sleep(3600)
            yield b"never reached"
        finally:
            self.closed = True

    def stream(self) -> AsyncIterator[bytes]:
        return self._stream()


async def test_limited_request_stream_times_out_stalled_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _StalledRequest()
    monkeypatch.setattr(transcribe_route, "_REQUEST_BODY_CHUNK_TIMEOUT_SECONDS", 0.001)
    stream = transcribe_route._limited_request_stream(cast(Request, request), max_body_bytes=1024)

    with pytest.raises(MultiPartException, match="Request body stalled"):
        await anext(stream)

    assert request.closed


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
    https_settings = _make_all_settings(tmp_path, require_https=True)
    async with _lifespan_client(https_settings) as client:
        # No X-Forwarded-Proto header -> should be rejected
        resp = await client.get(
            "/v1/queue/status",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert resp.status_code == 403
        assert "HTTPS" in resp.json()["detail"]


async def test_https_required_rejects_invalid_forwarded_proto_bytes() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("middleware forwarded malformed direct request")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    middleware = RequireHTTPSMiddleware(app)
    scope: Scope = {
        "type": "http",
        "path": "/v1/queue/status",
        "headers": [(b"x-forwarded-proto", b"\xff")],
    }

    await middleware(scope, receive, send)

    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 403


async def test_https_required_allows_health(tmp_path: Path) -> None:
    https_settings = _make_all_settings(
        tmp_path,
        require_https=True,
        vllm_base_url="http://127.0.0.1:1",
    )
    async with _lifespan_client(https_settings) as client:
        resp = await client.get("/health")
        assert resp.status_code == 503


async def test_https_required_passes_with_header(tmp_path: Path) -> None:
    https_settings = _make_all_settings(tmp_path, require_https=True)
    async with _lifespan_client(https_settings) as client:
        resp = await client.get(
            "/v1/queue/status",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "X-Forwarded-Proto": "https",
            },
        )
        assert resp.status_code == 200
