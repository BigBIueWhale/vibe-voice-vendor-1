"""Generate an ES256 key pair and signed JWT for VVV server authentication."""

import argparse
import os
import stat
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _write_secret(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate VVV auth key pair and JWT token")
    parser.add_argument("--keys-dir", required=True, help="Directory for key files")
    parser.add_argument("--subject", required=True, help="Token subject/username")
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="Print the generated bearer token to stdout as well as writing token.txt",
    )
    args = parser.parse_args()

    keys_dir = Path(args.keys_dir)
    private_key_path = keys_dir / "private.pem"
    public_key_path = keys_dir / "public.pem"

    if not private_key_path.exists():
        keys_dir.mkdir(parents=True, exist_ok=True)
        keys_dir.chmod(stat.S_IRWXU)
        private_key = ec.generate_private_key(ec.SECP256R1())

        _write_secret(
            private_key_path,
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
        print(f"Generated new key pair in {keys_dir}/")
    else:
        private_key = serialization.load_pem_private_key(  # type: ignore[assignment]
            private_key_path.read_bytes(), password=None
        )
        private_key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        keys_dir.chmod(stat.S_IRWXU)
        print(f"Using existing key pair from {keys_dir}/")

    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_key_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    token = jwt.encode(
        {"sub": args.subject, "jti": uuid.uuid4().hex},
        private_key,
        algorithm="ES256",
    )

    token_path = keys_dir / "token.txt"
    _write_secret(token_path, (token + "\n").encode())

    print(f"Subject:     {args.subject}")
    if args.print_token:
        print(f"Token:       {token}")
    print(f"Saved to:    {token_path}")
    print(f"Public key:  {public_key_path}")


if __name__ == "__main__":
    main()
