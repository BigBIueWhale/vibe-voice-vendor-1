"""Generate a one-file VVV Android client import bundle."""

from __future__ import annotations

import argparse
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from client.client import _normalize_base_url, _normalize_server_pin

_BUNDLE_TYPE = "vvv-client-config"
_BUNDLE_VERSION = 1
_PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
_PUBLIC_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH


class ClientBundle(TypedDict):
    type: str
    version: int
    server_url: str
    server_spki_pin: str
    client_certificate_pem: str
    client_private_key_pem: str


def build_client_bundle(
    *,
    server_url: str,
    server_spki_pin: str,
    client_certificate_pem: str,
    client_private_key_pem: str,
    client_ca_certificate_pem: str,
) -> ClientBundle:
    """Validate inputs and return the canonical VVV client import bundle."""
    normalized_url = _normalize_base_url(server_url)
    normalized_pin = _normalize_server_pin(server_spki_pin)
    normalized_client_certificate = _normalize_pem_block(
        client_certificate_pem,
        "CERTIFICATE",
    )
    normalized_client_private_key = _normalize_pem_block(
        client_private_key_pem,
        "PRIVATE KEY",
    )
    normalized_client_ca_certificate = _normalize_pem_block(
        client_ca_certificate_pem,
        "CERTIFICATE",
    )
    _validate_client_material(
        client_certificate_pem=normalized_client_certificate,
        client_private_key_pem=normalized_client_private_key,
        client_ca_certificate_pem=normalized_client_ca_certificate,
    )
    return {
        "type": _BUNDLE_TYPE,
        "version": _BUNDLE_VERSION,
        "server_url": normalized_url,
        "server_spki_pin": normalized_pin,
        "client_certificate_pem": normalized_client_certificate,
        "client_private_key_pem": normalized_client_private_key,
    }


def write_client_bundle_file(
    *,
    output_path: Path,
    server_url: str,
    server_spki_pin_file: Path,
    client_ca_certificate_file: Path,
    client_certificate_file: Path,
    client_private_key_file: Path,
) -> ClientBundle:
    """Build and write a client bundle containing private key material."""
    bundle = build_client_bundle(
        server_url=server_url,
        server_spki_pin=_read_public_text(server_spki_pin_file),
        client_ca_certificate_pem=_read_public_text(client_ca_certificate_file),
        client_certificate_pem=_read_public_text(client_certificate_file),
        client_private_key_pem=_read_private_text(client_private_key_file),
    )
    _write_private_json(output_path, bundle)
    return bundle


def _normalize_pem_block(raw: str, label: str) -> str:
    value = raw.strip().replace("\r\n", "\n").replace("\r", "\n") + "\n"
    begin = f"-----BEGIN {label}-----\n"
    end = f"-----END {label}-----\n"
    if not value.startswith(begin) or not value.endswith(end):
        raise ValueError(f"expected a single {label} PEM block")
    value.encode("ascii")
    return value


def _load_ec_public_key_from_cert(
    raw: str,
    label: str,
) -> tuple[x509.Certificate, ec.EllipticCurvePublicKey]:
    cert = x509.load_pem_x509_certificate(raw.encode("ascii"))
    public_key = cert.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError(f"{label} does not contain an EC public key")
    if public_key.curve.name != ec.SECP256R1().name:
        raise ValueError(f"{label} uses {public_key.curve.name}, expected secp256r1")
    return cert, public_key


def _load_ec_private_key(raw: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(raw.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("client private key is not an EC private key")
    if key.curve.name != ec.SECP256R1().name:
        raise ValueError(f"client private key uses {key.curve.name}, expected secp256r1")
    return key


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


def _validate_client_material(
    *,
    client_certificate_pem: str,
    client_private_key_pem: str,
    client_ca_certificate_pem: str,
) -> None:
    ca_cert, ca_public_key = _load_ec_public_key_from_cert(
        client_ca_certificate_pem,
        "client CA certificate",
    )
    client_cert, client_public_key = _load_ec_public_key_from_cert(
        client_certificate_pem,
        "client certificate",
    )
    client_key = _load_ec_private_key(client_private_key_pem)
    if client_key.public_key().public_numbers() != client_public_key.public_numbers():
        raise ValueError("client private key does not match client certificate")

    now = datetime.now(UTC)
    for cert, label in ((ca_cert, "client CA certificate"), (client_cert, "client certificate")):
        if cert.not_valid_before_utc - timedelta(seconds=60) > now:
            raise ValueError(f"{label} is not valid yet")
        if cert.not_valid_after_utc <= now:
            raise ValueError(f"{label} is expired")

    ca_basic = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    if not ca_basic.ca or ca_basic.path_length != 0:
        raise ValueError("client CA certificate must be a path_length=0 CA")
    ca_usage = ca_cert.extensions.get_extension_for_class(x509.KeyUsage).value
    if not ca_usage.key_cert_sign or not ca_usage.crl_sign:
        raise ValueError("client CA certificate lacks CA key usage")
    _verify_cert_signature(ca_cert, ca_public_key, "client CA certificate")

    if client_cert.issuer != ca_cert.subject:
        raise ValueError("client certificate issuer does not match client CA subject")
    _verify_cert_signature(client_cert, ca_public_key, "client certificate")
    client_basic = client_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    if client_basic.ca:
        raise ValueError("client certificate must not be a CA certificate")
    client_usage = client_cert.extensions.get_extension_for_class(x509.KeyUsage).value
    if not client_usage.digital_signature or client_usage.key_cert_sign or client_usage.crl_sign:
        raise ValueError("client certificate has invalid key usage")
    client_eku = client_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if ExtendedKeyUsageOID.CLIENT_AUTH not in client_eku:
        raise ValueError("client certificate lacks clientAuth EKU")


def _validate_existing_file(path: Path, mode: int) -> None:
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink")
    if not path.is_file():
        raise ValueError(f"{path} is not a regular file")
    actual_mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    if actual_mode != mode:
        raise ValueError(f"{path} mode is {actual_mode:03o}, expected {mode:03o}")


def _read_public_text(path: Path) -> str:
    _validate_existing_file(path, _PUBLIC_FILE_MODE)
    return path.read_text(encoding="ascii")


def _read_private_text(path: Path) -> str:
    _validate_existing_file(path, _PRIVATE_FILE_MODE)
    return path.read_text(encoding="ascii")


def _write_private_json(path: Path, bundle: ClientBundle) -> None:
    parent = path.parent
    if parent.is_symlink():
        raise ValueError(f"{parent} is a symlink")
    if not parent.is_dir():
        raise ValueError(f"{parent} is not a directory")
    data = json.dumps(bundle, indent=2, ensure_ascii=True).encode("ascii") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    except FileExistsError as exc:
        raise ValueError(f"{path} already exists; refusing to overwrite private material") from exc
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(_PRIVATE_FILE_MODE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a VVV Android client import bundle")
    parser.add_argument(
        "--server-url",
        required=True,
        help="Public IPv4-reachable HTTPS origin, for example https://example.com:42862",
    )
    parser.add_argument(
        "--server-spki-pin-file",
        default="certs/self-signed/server-spki-pin.txt",
        type=Path,
        help="File containing the sha256/... server SPKI pin",
    )
    parser.add_argument(
        "--client-ca-cert-file",
        default="certs/self-signed/client-ca.pem",
        type=Path,
        help="Client CA certificate used by the proxy",
    )
    parser.add_argument(
        "--client-cert-file",
        default="keys/client-cert.pem",
        type=Path,
        help="Client certificate PEM",
    )
    parser.add_argument(
        "--client-key-file",
        default="keys/client-key.pem",
        type=Path,
        help="Client private key PEM",
    )
    parser.add_argument(
        "--output",
        default="keys/client-bundle.vvv.json",
        type=Path,
        help="Output bundle path; must not already exist",
    )
    args = parser.parse_args()

    try:
        write_client_bundle_file(
            output_path=args.output,
            server_url=args.server_url,
            server_spki_pin_file=args.server_spki_pin_file,
            client_ca_certificate_file=args.client_ca_cert_file,
            client_certificate_file=args.client_cert_file,
            client_private_key_file=args.client_key_file,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    print(f"Client import bundle: {args.output}")


if __name__ == "__main__":
    main()
