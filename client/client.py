from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import ssl
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from client.models import EventType, TranscriptionEvent

ClientCert = str | tuple[str, str] | tuple[str, str, str]

_BOUNDARY = "----VibeVoiceCliBoundary9f2e4d"
_SERVER_PIN_PREFIX = "sha256/"
_AUDIO_CHUNK_BYTES = 1024 * 1024


class VibevoiceClient:
    def __init__(
        self,
        base_url: str,
        server_pin: str,
        cert: ClientCert,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._url = urlparse(self._base_url)
        self._server_pin = _normalize_server_pin(server_pin)
        self._cert = cert
        self._ssl_context = _build_ssl_context(cert)

    def _headers(self) -> dict[str, str]:
        return {
            "Host": _host_header(self._url.hostname or "", self._url.port),
            "Connection": "close",
        }

    def _connection(self) -> _PinnedHTTPSConnection:
        return _PinnedHTTPSConnection(
            host=self._url.hostname or "",
            port=self._url.port or 443,
            context=self._ssl_context,
            expected_pin=self._server_pin,
            timeout=10.0,
        )

    async def transcribe(
        self,
        audio_path: str | Path,
        hotwords: str | None = None,
    ) -> AsyncIterator[TranscriptionEvent]:
        """Upload audio and stream transcription events."""
        path = Path(audio_path)
        conn = self._connection()
        try:
            headers = self._headers()
            headers.update(
                {
                    "Accept": "text/event-stream",
                    "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}",
                }
            )
            conn.request(
                "POST",
                "/v1/transcribe",
                body=_multipart_body(path, hotwords),
                headers=headers,
                encode_chunked=True,
            )
            response = conn.getresponse()
            if response.status != 200:
                body = response.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"transcription failed with HTTP {response.status}: {body}")

            current_event = "data"
            while True:
                raw_line = response.readline()
                if raw_line == b"":
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event: "):
                    current_event = line[len("event: ") :]
                    continue
                if not line.startswith("data: "):
                    continue

                payload = _json_or_none(line[len("data: ") :])
                if payload is None:
                    continue

                if current_event == "queue":
                    yield TranscriptionEvent(
                        event_type=EventType.QUEUE,
                        job_id=str(payload["job_id"]),
                        position=int(payload["position"]),
                        estimated_wait_seconds=float(payload["estimated_wait_seconds"]),
                    )
                elif current_event == "data":
                    yield TranscriptionEvent(
                        event_type=EventType.DATA,
                        text=str(payload["text"]),
                    )
                elif current_event == "error":
                    yield TranscriptionEvent(
                        event_type=EventType.ERROR,
                        error=str(payload["error"]),
                    )
                elif current_event == "done":
                    yield TranscriptionEvent(
                        event_type=EventType.DONE,
                        job_id=str(payload["job_id"]),
                    )

                current_event = "data"
        finally:
            conn.close()

    async def queue_status(self) -> dict[str, object]:
        """Get queue status for the configured client certificate identity."""
        conn = self._connection()
        try:
            conn.request("GET", "/v1/queue/status", headers=self._headers())
            response = conn.getresponse()
            body = response.read()
            if response.status != 200:
                detail = body.decode("utf-8", errors="replace")
                raise RuntimeError(f"queue status failed with HTTP {response.status}: {detail}")
            return json.loads(body.decode("utf-8"))  # type: ignore[no-any-return]
        finally:
            conn.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        context: ssl.SSLContext,
        expected_pin: str,
        timeout: float,
    ) -> None:
        super().__init__(
            host=host,
            port=port,
            timeout=timeout,
            context=context,
        )
        self._expected_pin = expected_pin

    def connect(self) -> None:
        super().connect()
        if self.sock is None:
            raise RuntimeError("TLS connection did not produce a socket")
        cert_der = self.sock.getpeercert(binary_form=True)
        if not cert_der:
            raise ssl.SSLError("server did not present a certificate")
        actual_pin = _spki_pin_from_certificate_der(cert_der)
        if actual_pin != self._expected_pin:
            self.close()
            raise ssl.SSLError("server public key pin mismatch")


def _normalize_base_url(raw: str) -> str:
    url = urlparse(raw.strip())
    if url.scheme != "https":
        raise ValueError("server URL must be an HTTPS origin")
    if not url.hostname or url.username or url.password:
        raise ValueError("server URL must include a host and no user info")
    if ":" in url.hostname:
        raise ValueError("server URL must use an IPv4 address or DNS name, not an IPv6 literal")
    if url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("server URL must be an origin, not an endpoint URL")
    authority = url.hostname
    if url.port is not None:
        authority = f"{authority}:{url.port}"
    return f"https://{authority}"


def _host_header(host: str, port: int | None) -> str:
    value = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{value}:{port}" if port is not None else value


def _normalize_server_pin(raw: str) -> str:
    value = raw.strip()
    if not value.startswith(_SERVER_PIN_PREFIX):
        raise ValueError("server pin must start with sha256/")
    encoded = value.removeprefix(_SERVER_PIN_PREFIX)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("server pin is not valid base64") from exc
    if len(decoded) != 32:
        raise ValueError("server pin must be a SHA-256 hash")
    return _SERVER_PIN_PREFIX + base64.b64encode(decoded).decode("ascii")


def _spki_pin_from_certificate_der(cert_der: bytes) -> str:
    cert = x509.load_der_x509_certificate(cert_der)
    spki = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return _SERVER_PIN_PREFIX + base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def _build_ssl_context(cert: ClientCert) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if isinstance(cert, str):
        context.load_cert_chain(cert)
    elif len(cert) == 2:
        context.load_cert_chain(cert[0], cert[1])
    else:
        context.load_cert_chain(cert[0], cert[1], cert[2])
    return context


def _multipart_body(path: Path, hotwords: str | None) -> Iterable[bytes]:
    yield f"--{_BOUNDARY}\r\n".encode()
    yield (
        f'Content-Disposition: form-data; name="audio"; filename="{_safe_filename(path.name)}"\r\n'
    ).encode()
    yield b"Content-Type: audio/wav\r\n\r\n"
    with path.open("rb") as audio:
        while chunk := audio.read(_AUDIO_CHUNK_BYTES):
            yield chunk
    yield b"\r\n"
    if hotwords:
        yield f"--{_BOUNDARY}\r\n".encode()
        yield b'Content-Disposition: form-data; name="hotwords"\r\n\r\n'
        yield hotwords.encode("utf-8")
        yield b"\r\n"
    yield f"--{_BOUNDARY}--\r\n".encode()


def _safe_filename(name: str) -> str:
    return name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")


def _json_or_none(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
