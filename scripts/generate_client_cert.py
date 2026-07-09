"""Generate local mTLS client-auth artifacts for the VVV public proxy."""

from __future__ import annotations

import argparse
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_CERT_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
_DIR_MODE = stat.S_IRWXU
_KEY_MODE = stat.S_IRUSR | stat.S_IWUSR
_MAX_SUBJECT_BYTES = 256
_MAX_VALIDITY_DAYS = 3650


def _write_file(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(mode)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _validate_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink")
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    mode = _mode(path)
    if mode != _DIR_MODE:
        raise ValueError(f"{path} mode is {mode:03o}, expected 700")


def _validate_file(path: Path, expected_mode: int) -> None:
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink")
    if not path.is_file():
        raise ValueError(f"{path} is not a regular file")
    mode = _mode(path)
    if mode != expected_mode:
        raise ValueError(f"{path} mode is {mode:03o}, expected {expected_mode:03o}")


def _create_or_validate_directory(path: Path) -> None:
    if path.exists():
        _validate_directory(path)
    else:
        path.mkdir(parents=True)
        path.chmod(_DIR_MODE)
        _validate_directory(path)


def _validate_subject(subject: str) -> None:
    if not subject:
        raise ValueError("subject must not be empty")
    if len(subject.encode("utf-8")) > _MAX_SUBJECT_BYTES:
        raise ValueError(f"subject exceeds {_MAX_SUBJECT_BYTES} bytes")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in subject):
        raise ValueError("subject must not contain control characters")


def _validate_days(days: int) -> None:
    if not 1 <= days <= _MAX_VALIDITY_DAYS:
        raise ValueError(f"--days must be between 1 and {_MAX_VALIDITY_DAYS}")


def _load_ec_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError(f"{path} is not an EC private key")
    if key.curve.name != ec.SECP256R1().name:
        raise ValueError(f"{path} is {key.curve.name}, expected secp256r1")
    return key


def _load_ec_public_key_from_cert(path: Path) -> tuple[x509.Certificate, ec.EllipticCurvePublicKey]:
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    public_key = cert.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError(f"{path} does not contain an EC public key")
    if public_key.curve.name != ec.SECP256R1().name:
        raise ValueError(f"{path} is {public_key.curve.name}, expected secp256r1")
    return cert, public_key


def _verify_cert_signature(
    cert: x509.Certificate,
    issuer_public_key: ec.EllipticCurvePublicKey,
    label: str,
) -> None:
    signature_hash_algorithm = cert.signature_hash_algorithm
    if signature_hash_algorithm is None:
        raise ValueError(f"{label} signature hash algorithm is unavailable")
    try:
        issuer_public_key.verify(
            cert.signature,
            cert.tbs_certificate_bytes,
            ec.ECDSA(signature_hash_algorithm),
        )
    except InvalidSignature as exc:
        raise ValueError(f"{label} signature is invalid") from exc


def validate_client_auth_artifacts(certs_dir: str, keys_dir: str) -> None:
    certs = Path(certs_dir)
    keys = Path(keys_dir)
    client_ca_cert_path = certs / "client-ca.pem"
    client_cert_path = keys / "client-cert.pem"
    client_key_path = keys / "client-key.pem"

    _validate_directory(certs)
    _validate_directory(keys)
    _validate_file(client_ca_cert_path, _CERT_MODE)
    _validate_file(client_cert_path, _CERT_MODE)
    _validate_file(client_key_path, _KEY_MODE)

    ca_cert, ca_public_key = _load_ec_public_key_from_cert(client_ca_cert_path)
    client_cert, client_public_key = _load_ec_public_key_from_cert(client_cert_path)
    client_key = _load_ec_private_key(client_key_path)
    if client_key.public_key().public_numbers() != client_public_key.public_numbers():
        raise ValueError("client-key.pem does not match client-cert.pem")

    now = datetime.now(UTC)
    for cert, label in ((ca_cert, "client CA certificate"), (client_cert, "client certificate")):
        if cert.not_valid_before_utc - timedelta(seconds=60) > now:
            raise ValueError(f"{label} is not valid yet")
        if cert.not_valid_after_utc <= now:
            raise ValueError(f"{label} is expired")

    ca_basic = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    if not ca_basic.ca or ca_basic.path_length != 0:
        raise ValueError("client-ca.pem is not a path_length=0 CA certificate")
    ca_usage = ca_cert.extensions.get_extension_for_class(x509.KeyUsage).value
    if not ca_usage.key_cert_sign or not ca_usage.crl_sign:
        raise ValueError("client-ca.pem lacks CA key usage")
    _verify_cert_signature(ca_cert, ca_public_key, "client-ca.pem")

    if client_cert.issuer != ca_cert.subject:
        raise ValueError("client-cert.pem issuer does not match client-ca.pem subject")
    _verify_cert_signature(client_cert, ca_public_key, "client-cert.pem")
    client_basic = client_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    if client_basic.ca:
        raise ValueError("client-cert.pem must not be a CA certificate")
    client_usage = client_cert.extensions.get_extension_for_class(x509.KeyUsage).value
    if not client_usage.digital_signature or client_usage.key_cert_sign or client_usage.crl_sign:
        raise ValueError("client-cert.pem has invalid key usage")
    client_eku = client_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if ExtendedKeyUsageOID.CLIENT_AUTH not in client_eku:
        raise ValueError("client-cert.pem lacks clientAuth EKU")


def _generate_client_auth_artifacts(
    certs_dir: str,
    keys_dir: str,
    subject: str,
    days: int,
) -> dict[str, str]:
    """Generate a one-client local mTLS CA/cert/key set.

    The CA private key is intentionally not stored. Regenerate the set if a new
    client credential is needed.
    """
    certs = Path(certs_dir)
    keys = Path(keys_dir)
    client_ca_cert_path = certs / "client-ca.pem"
    client_cert_path = keys / "client-cert.pem"
    client_key_path = keys / "client-key.pem"
    paths = (client_ca_cert_path, client_cert_path, client_key_path)

    existing = [path for path in paths if path.exists()]
    if existing:
        names = ", ".join(str(path) for path in existing)
        return {"error": f"Client-auth artifacts already exist: {names}"}

    try:
        _validate_subject(subject)
        _validate_days(days)
        _create_or_validate_directory(certs)
        _create_or_validate_directory(keys)
    except ValueError as exc:
        return {"error": str(exc)}

    ca_key = ec.generate_private_key(ec.SECP256R1())
    client_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)

    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VVV Client Auth CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    client_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(client_subject)
        .issuer_name(ca_subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _write_file(client_ca_cert_path, ca_cert.public_bytes(serialization.Encoding.PEM), _CERT_MODE)
    _write_file(client_cert_path, client_cert.public_bytes(serialization.Encoding.PEM), _CERT_MODE)
    _write_file(
        client_key_path,
        client_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        _KEY_MODE,
    )

    try:
        validate_client_auth_artifacts(certs_dir, keys_dir)
    except (OSError, ValueError) as exc:
        return {"error": f"Generated client-auth artifacts failed validation: {exc}"}

    return {
        "status": "generated",
        "client_ca_cert_path": str(client_ca_cert_path),
        "client_cert_path": str(client_cert_path),
        "client_key_path": str(client_key_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate VVV mTLS client-auth artifacts")
    parser.add_argument("--certs-dir", required=True, help="Directory for client CA certificate")
    parser.add_argument("--keys-dir", required=True, help="Directory for client certificate/key")
    parser.add_argument("--subject", required=True, help="Client certificate subject common name")
    parser.add_argument("--days", type=int, default=3650, help="Certificate validity in days")
    args = parser.parse_args()

    result = _generate_client_auth_artifacts(
        certs_dir=args.certs_dir,
        keys_dir=args.keys_dir,
        subject=args.subject,
        days=args.days,
    )
    if "error" in result:
        raise SystemExit(f"ERROR: {result['error']}")

    print(f"Status:          {result['status']}")
    print(f"Client CA cert:  {result['client_ca_cert_path']}")
    print(f"Client cert:     {result['client_cert_path']}")
    print(f"Client key:      {result['client_key_path']}")


if __name__ == "__main__":
    main()
