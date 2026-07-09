from __future__ import annotations

import ssl
from collections.abc import Coroutine
from pathlib import Path
from unittest.mock import patch

import pytest

from client.client import VibevoiceClient
from scripts.generate_client_cert import _generate_client_auth_artifacts


def _make_tls_files(tmp_path: Path) -> dict[str, str]:
    return _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=30,
    )


def test_verify_true() -> None:
    c = VibevoiceClient("http://localhost", "tok", verify=True)
    assert c._verify is True


def test_verify_false_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be disabled"):
        VibevoiceClient("http://localhost", "tok", verify=False)


def test_verify_ca_cert_path(tmp_path: Path) -> None:
    tls = _make_tls_files(tmp_path)
    c = VibevoiceClient("http://localhost", "tok", verify=tls["client_ca_cert_path"])
    assert c._verify == tls["client_ca_cert_path"]
    assert isinstance(c._ssl_context, ssl.SSLContext)


def test_client_cert_tuple(tmp_path: Path) -> None:
    tls = _make_tls_files(tmp_path)
    c = VibevoiceClient(
        "http://localhost",
        "tok",
        verify=tls["client_ca_cert_path"],
        cert=(tls["client_cert_path"], tls["client_key_path"]),
    )
    assert c._cert == (tls["client_cert_path"], tls["client_key_path"])
    assert isinstance(c._ssl_context, ssl.SSLContext)


# ── CLI tests ────────────────────────────────────────────────────────


def test_cli_ca_cert_missing_file(tmp_path: Path) -> None:
    from client.cli import main

    fake_path = str(tmp_path / "nonexistent.pem")
    with (
        patch(
            "sys.argv",
            [
                "vvv",
                "--server",
                "https://x",
                "--token",
                "t",
                "--ca-cert",
                fake_path,
                "--client-cert",
                fake_path,
                "--client-key",
                fake_path,
                "status",
            ],
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 1


def test_cli_rejects_insecure_argument(tmp_path: Path) -> None:
    client_cert = tmp_path / "client-cert.pem"
    client_key = tmp_path / "client-key.pem"
    client_cert.write_text("fake")
    client_key.write_text("fake")
    from client.cli import main

    with (
        patch(
            "sys.argv",
            [
                "vvv",
                "--server",
                "https://x",
                "--token",
                "t",
                "--insecure",
                "--client-cert",
                str(client_cert),
                "--client-key",
                str(client_key),
                "status",
            ],
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 2


def test_cli_client_cert_missing_file(tmp_path: Path) -> None:
    from client.cli import main

    fake_path = str(tmp_path / "nonexistent.pem")
    with (
        patch(
            "sys.argv",
            [
                "vvv",
                "--server",
                "https://x",
                "--token",
                "t",
                "--client-cert",
                fake_path,
                "--client-key",
                fake_path,
                "status",
            ],
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 1


def test_cli_ca_cert_valid_file(tmp_path: Path) -> None:
    """When --ca-cert points to a real file, the client should receive it."""
    tls = _make_tls_files(tmp_path)
    ca = tls["client_ca_cert_path"]
    client_cert = tls["client_cert_path"]
    client_key = tls["client_key_path"]

    captured_clients: list[VibevoiceClient] = []
    original_init = VibevoiceClient.__init__

    def spy_init(self: VibevoiceClient, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        captured_clients.append(self)

    def close_and_exit(coro: Coroutine[object, object, object]) -> None:
        coro.close()
        raise SystemExit(0)

    with (
        patch(
            "sys.argv",
            [
                "vvv",
                "--server",
                "https://x",
                "--token",
                "t",
                "--ca-cert",
                ca,
                "--client-cert",
                client_cert,
                "--client-key",
                client_key,
                "status",
            ],
        ),
        patch.object(VibevoiceClient, "__init__", spy_init),
        patch("client.cli.asyncio.run", side_effect=close_and_exit),
        pytest.raises(SystemExit),
    ):
        from client.cli import main

        main()

    assert len(captured_clients) == 1
    assert captured_clients[0]._verify == ca
    assert captured_clients[0]._cert == (client_cert, client_key)


@pytest.mark.asyncio
async def test_httpx_receives_single_ssl_context_without_deprecated_cert_param(
    tmp_path: Path,
) -> None:
    tls = _make_tls_files(tmp_path)
    captured_kwargs: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"your_jobs": [], "total_queued": 0}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, *args: object, **kwargs: object) -> _Response:
            return _Response()

    client = VibevoiceClient(
        "https://example.test",
        "tok",
        verify=tls["client_ca_cert_path"],
        cert=(tls["client_cert_path"], tls["client_key_path"]),
    )

    with patch("client.client.httpx.AsyncClient", _Client):
        await client.queue_status()

    assert isinstance(captured_kwargs["verify"], ssl.SSLContext)
    assert "cert" not in captured_kwargs
