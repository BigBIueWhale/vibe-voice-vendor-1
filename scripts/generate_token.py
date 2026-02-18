"""Generate an ES256 key pair and signed JWT for VVV server authentication."""

import argparse
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate VVV auth key pair and JWT token")
    parser.add_argument(
        "--keys-dir", default="keys", help="Directory for key files (default: keys)"
    )
    parser.add_argument("--subject", default=None, help="Token subject/username (default: random)")
    args = parser.parse_args()

    keys_dir = Path(args.keys_dir)
    private_key_path = keys_dir / "private.pem"
    public_key_path = keys_dir / "public.pem"

    if not private_key_path.exists():
        keys_dir.mkdir(parents=True, exist_ok=True)
        private_key = ec.generate_private_key(ec.SECP256R1())

        private_key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_key_path.write_bytes(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        print(f"Generated new key pair in {keys_dir}/")
    else:
        private_key = serialization.load_pem_private_key(  # type: ignore[assignment]
            private_key_path.read_bytes(), password=None
        )
        print(f"Using existing key pair from {keys_dir}/")

    subject = args.subject or uuid.uuid4().hex[:12]

    token = jwt.encode(
        {"sub": subject, "jti": uuid.uuid4().hex},
        private_key,
        algorithm="ES256",
    )

    print(f"Subject:     {subject}")
    print(f"Token:       {token}")
    print(f"Public key:  {public_key_path}")
    print(f"\nSet on server: VVV_JWT_PUBLIC_KEY_FILE={public_key_path}")


if __name__ == "__main__":
    main()
