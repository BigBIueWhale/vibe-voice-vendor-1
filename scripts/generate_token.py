"""Generate a bearer token and its bcrypt hash for VVV server authentication."""

import secrets

import bcrypt


def main() -> None:
    raw_token = secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(raw_token.encode("utf-8"), bcrypt.gensalt(rounds=12))

    print(f"Token (give to client):  {raw_token}")
    print(f"Hash  (add to VVV_TOKEN_HASHES_ENV):  {token_hash.decode('utf-8')}")


if __name__ == "__main__":
    main()
