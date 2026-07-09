"""Validate existing VVV ES256 JWT artifacts without modifying them."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.generate_token import validate_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate VVV JWT auth artifacts")
    parser.add_argument("--keys-dir", required=True, help="Directory containing key files")
    args = parser.parse_args()

    keys_dir = Path(args.keys_dir)
    try:
        payload = validate_artifacts(keys_dir)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    print("Status:      valid")
    print(f"Subject:     {payload['sub']}")
    print(f"Token:       {keys_dir / 'token.txt'}")
    print(f"Public key:  {keys_dir / 'public.pem'}")


if __name__ == "__main__":
    main()
