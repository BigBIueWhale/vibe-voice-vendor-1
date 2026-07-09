from __future__ import annotations

import stat
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import ExtendedKeyUsageOID

from scripts.generate_client_cert import (
    _generate_client_auth_artifacts,
    validate_client_auth_artifacts,
)


def test_generates_client_auth_artifacts(tmp_path: Path) -> None:
    result = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )

    assert result["status"] == "generated"
    assert Path(result["client_ca_cert_path"]).exists()
    assert Path(result["client_cert_path"]).exists()
    assert Path(result["client_key_path"]).exists()


def test_client_auth_artifact_permissions(tmp_path: Path) -> None:
    result = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )

    ca_mode = Path(result["client_ca_cert_path"]).stat().st_mode & 0o777
    cert_mode = Path(result["client_cert_path"]).stat().st_mode & 0o777
    key_mode = Path(result["client_key_path"]).stat().st_mode & 0o777
    assert ca_mode == stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
    assert cert_mode == stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
    assert key_mode == stat.S_IRUSR | stat.S_IWUSR


def test_client_certificate_has_client_auth_eku(tmp_path: Path) -> None:
    result = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="vvv-client",
        days=365,
    )

    client_cert = x509.load_pem_x509_certificate(Path(result["client_cert_path"]).read_bytes())
    eku = client_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku.value


def test_client_ca_is_ca_and_client_key_is_p256(tmp_path: Path) -> None:
    result = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )

    ca_cert = x509.load_pem_x509_certificate(Path(result["client_ca_cert_path"]).read_bytes())
    basic = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.value.ca is True

    key = load_pem_private_key(Path(result["client_key_path"]).read_bytes(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.key_size == 256


def test_existing_complete_state_is_refused_and_validator_accepts(tmp_path: Path) -> None:
    first = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )
    second = _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )

    assert first["status"] == "generated"
    assert "error" in second
    assert "already exist" in second["error"]
    validate_client_auth_artifacts(str(tmp_path / "certs"), str(tmp_path / "keys"))


def test_partial_state_is_refused(tmp_path: Path) -> None:
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "client-ca.pem").write_text("partial")

    result = _generate_client_auth_artifacts(
        certs_dir=str(certs),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )

    assert "error" in result


def test_existing_bad_directory_mode_is_refused(tmp_path: Path) -> None:
    certs = tmp_path / "certs"
    certs.mkdir(mode=0o755)

    result = _generate_client_auth_artifacts(
        certs_dir=str(certs),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=365,
    )

    assert "error" in result
    assert "expected 700" in result["error"]
