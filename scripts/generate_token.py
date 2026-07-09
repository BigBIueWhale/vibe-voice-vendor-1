"""Generate ES256 key/token artifacts for VVV server authentication."""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_DIR_MODE = stat.S_IRWXU
_SECRET_MODE = stat.S_IRUSR | stat.S_IWUSR
_PUBLIC_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
_MAX_SUBJECT_BYTES = 256
_MAX_JTI_BYTES = 128
_DEFAULT_TOKEN_DAYS = 365
_MAX_TOKEN_DAYS = 366


def _open_create_exclusive(path: Path, mode: int) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, mode)


def _write_secret(path: Path, data: bytes) -> None:
    fd = _open_create_exclusive(path, _SECRET_MODE)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(_SECRET_MODE)


def _write_public(path: Path, data: bytes) -> None:
    fd = _open_create_exclusive(path, _PUBLIC_MODE)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(_PUBLIC_MODE)


def _replace_secret(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink")
    if not path.is_file():
        raise ValueError(f"{path} is not a regular file")
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_secret(tmp_path, data)
        os.replace(tmp_path, path)
        path.chmod(_SECRET_MODE)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


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


def _validate_subject(subject: str) -> None:
    if not subject:
        raise ValueError("subject must not be empty")
    if len(subject.encode("utf-8")) > _MAX_SUBJECT_BYTES:
        raise ValueError(f"subject exceeds {_MAX_SUBJECT_BYTES} bytes")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in subject):
        raise ValueError("subject must not contain control characters")


def _validate_days(days: int) -> None:
    if not 1 <= days <= _MAX_TOKEN_DAYS:
        raise ValueError(f"--expires-in-days must be between 1 and {_MAX_TOKEN_DAYS}")


def _load_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError(f"{path} is not an EC private key")
    if key.curve.name != ec.SECP256R1().name:
        raise ValueError(f"{path} is {key.curve.name}, expected secp256r1")
    return key


def _load_public_key(path: Path) -> ec.EllipticCurvePublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError(f"{path} is not an EC public key")
    if key.curve.name != ec.SECP256R1().name:
        raise ValueError(f"{path} is {key.curve.name}, expected secp256r1")
    return key


def _validate_key_pair(
    private_key: ec.EllipticCurvePrivateKey,
    public_key: ec.EllipticCurvePublicKey,
) -> None:
    if private_key.public_key().public_numbers() != public_key.public_numbers():
        raise ValueError("public.pem does not match private.pem")


def _validate_token(token_path: Path, public_key: ec.EllipticCurvePublicKey) -> dict[str, Any]:
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"{token_path} is empty")
    if any(char.isspace() for char in token):
        raise ValueError(f"{token_path} contains whitespace inside the token")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise ValueError(f"{token_path} has an invalid JWT header") from exc
    if set(header) - {"alg", "typ"}:
        raise ValueError(f"{token_path} has unexpected JWT header fields")
    if header.get("alg") != "ES256":
        raise ValueError(f"{token_path} uses {header.get('alg')!r}, expected ES256")
    if header.get("typ") not in (None, "JWT"):
        raise ValueError(f"{token_path} has unexpected JWT typ")
    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["ES256"],
            options={"require": ["sub", "jti", "iat", "nbf", "exp"]},
        )
    except jwt.InvalidTokenError as exc:
        raise ValueError(f"{token_path} failed ES256 JWT validation") from exc

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub or len(sub.encode("utf-8")) > _MAX_SUBJECT_BYTES:
        raise ValueError(f"{token_path} has an invalid sub claim")
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti or len(jti.encode("utf-8")) > _MAX_JTI_BYTES:
        raise ValueError(f"{token_path} has an invalid jti claim")
    return payload


def validate_artifacts(keys_dir: Path) -> dict[str, Any]:
    private_key_path = keys_dir / "private.pem"
    public_key_path = keys_dir / "public.pem"
    token_path = keys_dir / "token.txt"

    _validate_directory(keys_dir)
    _validate_file(private_key_path, _SECRET_MODE)
    _validate_file(public_key_path, _PUBLIC_MODE)
    _validate_file(token_path, _SECRET_MODE)

    private_key = _load_private_key(private_key_path)
    public_key = _load_public_key(public_key_path)
    _validate_key_pair(private_key, public_key)
    return _validate_token(token_path, public_key)


def _generate_token(
    private_key: ec.EllipticCurvePrivateKey,
    subject: str,
    expires_in_days: int,
) -> str:
    now = int(time.time())
    payload = {
        "sub": subject,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "nbf": now,
        "exp": now + expires_in_days * 24 * 60 * 60,
    }
    return jwt.encode(payload, private_key, algorithm="ES256")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate VVV auth key pair and JWT token")
    parser.add_argument("--keys-dir", required=True, help="Directory for key files")
    parser.add_argument("--subject", required=True, help="Token subject/username")
    parser.add_argument(
        "--expires-in-days",
        type=int,
        default=_DEFAULT_TOKEN_DAYS,
        help=f"Generated token validity in days, 1-{_MAX_TOKEN_DAYS}",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="Print the generated bearer token to stdout as well as writing token.txt",
    )
    args = parser.parse_args()

    keys_dir = Path(args.keys_dir)
    private_key_path = keys_dir / "private.pem"
    public_key_path = keys_dir / "public.pem"
    token_path = keys_dir / "token.txt"

    subject = str(args.subject)
    try:
        _validate_subject(subject)
        _validate_days(args.expires_in_days)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    paths = (private_key_path, public_key_path, token_path)
    existing = [path for path in paths if path.exists()]
    if existing and len(existing) != len(paths):
        names = ", ".join(str(path) for path in existing)
        raise SystemExit(f"ERROR: Partial JWT artifact state exists: {names}")

    if not existing:
        if keys_dir.exists():
            try:
                _validate_directory(keys_dir)
            except (OSError, ValueError) as exc:
                raise SystemExit(f"ERROR: {exc}") from exc
        else:
            keys_dir.mkdir(parents=True)
            keys_dir.chmod(_DIR_MODE)
            try:
                _validate_directory(keys_dir)
            except (OSError, ValueError) as exc:
                raise SystemExit(f"ERROR: {exc}") from exc
        private_key = ec.generate_private_key(ec.SECP256R1())

        _write_secret(
            private_key_path,
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
        _write_public(
            public_key_path,
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
        )
        print(f"Generated new key pair in {keys_dir}/")
    else:
        try:
            _validate_directory(keys_dir)
            _validate_file(private_key_path, _SECRET_MODE)
            _validate_file(public_key_path, _PUBLIC_MODE)
            private_key = _load_private_key(private_key_path)
            public_key = _load_public_key(public_key_path)
            _validate_key_pair(private_key, public_key)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"ERROR: {exc}") from exc
        print(f"Using existing validated key pair from {keys_dir}/")

    token = _generate_token(private_key, subject, args.expires_in_days)
    if token_path.exists():
        _replace_secret(token_path, (token + "\n").encode())
    else:
        _write_secret(token_path, (token + "\n").encode())
    try:
        validate_artifacts(keys_dir)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: generated token artifacts failed validation: {exc}") from exc

    print(f"Subject:     {subject}")
    print(f"Expires in:  {args.expires_in_days} days")
    if args.print_token:
        print(f"Token:       {token}")
    print(f"Saved to:    {token_path}")
    print(f"Public key:  {public_key_path}")


if __name__ == "__main__":
    main()
