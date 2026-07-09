from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest

from scripts.generate_client_bundle import build_client_bundle, write_client_bundle_file
from scripts.generate_client_cert import _generate_client_auth_artifacts

_SERVER_PIN = "sha256/" + base64.b64encode(b"\x11" * 32).decode("ascii")


def test_writes_strict_android_client_bundle(tmp_path: Path) -> None:
    paths = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )
    pin_file = tmp_path / "certs" / "server-spki-pin.txt"
    pin_file.write_text(_SERVER_PIN + "\n", encoding="ascii")
    pin_file.chmod(0o644)
    output = tmp_path / "keys" / "client-bundle.vvv.json"

    bundle = write_client_bundle_file(
        output_path=output,
        server_url=" https://vvv.example.invalid:42862/ ",
        server_spki_pin_file=pin_file,
        client_ca_certificate_file=Path(paths["client_ca_cert_path"]),
        client_certificate_file=Path(paths["client_cert_path"]),
        client_private_key_file=Path(paths["client_key_path"]),
    )

    assert output.exists()
    assert stat.S_IMODE(output.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    saved = json.loads(output.read_text(encoding="ascii"))
    assert saved == bundle
    assert saved["type"] == "vvv-client-config"
    assert saved["version"] == 1
    assert saved["server_url"] == "https://vvv.example.invalid:42862"
    assert saved["server_spki_pin"] == _SERVER_PIN
    assert saved["client_certificate_pem"].startswith("-----BEGIN CERTIFICATE-----\n")
    assert saved["client_private_key_pem"].startswith("-----BEGIN PRIVATE KEY-----\n")


def test_client_bundle_rejects_invalid_url(tmp_path: Path) -> None:
    paths = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )

    with pytest.raises(ValueError, match="HTTPS origin"):
        build_client_bundle(
            server_url="http://vvv.example.invalid:42862",
            server_spki_pin=_SERVER_PIN,
            client_ca_certificate_pem=Path(paths["client_ca_cert_path"]).read_text(
                encoding="ascii"
            ),
            client_certificate_pem=Path(paths["client_cert_path"]).read_text(encoding="ascii"),
            client_private_key_pem=Path(paths["client_key_path"]).read_text(encoding="ascii"),
        )


def test_client_bundle_rejects_mismatched_client_key(tmp_path: Path) -> None:
    first = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "first-certs"),
        keys_dir=str(tmp_path / "first-keys"),
        subject="first",
        days=365,
    )
    second = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "second-certs"),
        keys_dir=str(tmp_path / "second-keys"),
        subject="second",
        days=365,
    )

    with pytest.raises(ValueError, match="does not match"):
        build_client_bundle(
            server_url="https://vvv.example.invalid:42862",
            server_spki_pin=_SERVER_PIN,
            client_ca_certificate_pem=Path(first["client_ca_cert_path"]).read_text(
                encoding="ascii"
            ),
            client_certificate_pem=Path(first["client_cert_path"]).read_text(encoding="ascii"),
            client_private_key_pem=Path(second["client_key_path"]).read_text(encoding="ascii"),
        )


def test_client_bundle_refuses_to_overwrite(tmp_path: Path) -> None:
    paths = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )
    pin_file = tmp_path / "certs" / "server-spki-pin.txt"
    pin_file.write_text(_SERVER_PIN + "\n", encoding="ascii")
    pin_file.chmod(0o644)
    output = tmp_path / "keys" / "client-bundle.vvv.json"
    output.write_text("existing", encoding="ascii")
    output.chmod(0o600)

    with pytest.raises(ValueError, match="refusing to overwrite"):
        write_client_bundle_file(
            output_path=output,
            server_url="https://vvv.example.invalid:42862",
            server_spki_pin_file=pin_file,
            client_ca_certificate_file=Path(paths["client_ca_cert_path"]),
            client_certificate_file=Path(paths["client_cert_path"]),
            client_private_key_file=Path(paths["client_key_path"]),
        )
