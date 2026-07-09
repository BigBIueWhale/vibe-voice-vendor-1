"""Validate existing VVV mTLS client-auth artifacts without modifying them."""

from __future__ import annotations

import argparse

from scripts.generate_client_cert import validate_client_auth_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate VVV mTLS client-auth artifacts")
    parser.add_argument("--certs-dir", required=True, help="Directory containing client-ca.pem")
    parser.add_argument(
        "--keys-dir",
        required=True,
        help="Directory containing client-cert.pem and client-key.pem",
    )
    args = parser.parse_args()

    try:
        validate_client_auth_artifacts(args.certs_dir, args.keys_dir)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    print("Status:          valid")
    print(f"Client CA cert:  {args.certs_dir}/client-ca.pem")
    print(f"Client cert:     {args.keys_dir}/client-cert.pem")
    print(f"Client key:      {args.keys_dir}/client-key.pem")


if __name__ == "__main__":
    main()
