import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from server.config import Settings

_bearer_scheme = HTTPBearer()

_REVOCATION_CACHE_TTL = 30.0  # seconds
_MAX_BEARER_TOKEN_BYTES = 8 * 1024
_MAX_JWT_SUB_BYTES = 256
_MAX_JWT_JTI_BYTES = 128
_MAX_REVOCATION_FILE_BYTES = 1024 * 1024
_revocation_cache: tuple[float, frozenset[str]] = (0.0, frozenset())


@lru_cache(maxsize=1)
def _load_public_key(path: str) -> object:
    with open(path, "rb") as f:
        return load_pem_public_key(f.read())


def _get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _load_revoked_tokens(filepath: str) -> frozenset[str]:
    """Load revoked JTI values from file, with a 30-second cache."""
    global _revocation_cache
    now = time.monotonic()
    cached_at, cached_set = _revocation_cache
    if now - cached_at < _REVOCATION_CACHE_TTL:
        return cached_set

    try:
        with Path(filepath).open("rb") as f:
            data = f.read(_MAX_REVOCATION_FILE_BYTES + 1)
    except OSError as exc:
        raise RuntimeError("Revocation list unavailable") from exc
    if len(data) > _MAX_REVOCATION_FILE_BYTES:
        raise RuntimeError("Revocation list too large")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Revocation list is not UTF-8") from exc

    revoked_entries: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > _MAX_JWT_JTI_BYTES:
            raise RuntimeError("Revocation entry too long")
        revoked_entries.add(line)
    revoked = frozenset(revoked_entries)

    _revocation_cache = (now, revoked)
    return revoked


def verify_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    settings: Annotated[Settings, Depends(_get_settings)],
) -> str:
    """Verify a JWT bearer token using ES256 public key. Returns the 'sub' claim."""
    if not settings.jwt_public_key_file:
        raise HTTPException(status_code=401, detail="No public key configured")

    token = credentials.credentials
    if (
        not token
        or len(token) > _MAX_BEARER_TOKEN_BYTES
        or any(char.isspace() for char in token)
    ):
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        public_key = _load_public_key(settings.jwt_public_key_file)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Public key unavailable") from exc

    try:
        payload: dict[str, object] = jwt.decode(
            token,
            public_key,  # type: ignore[arg-type]
            algorithms=["ES256"],
            options={"require": ["sub", "jti"]},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > _MAX_JWT_SUB_BYTES:
        raise HTTPException(status_code=401, detail="Invalid token")

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti or len(jti) > _MAX_JWT_JTI_BYTES:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        revoked = _load_revoked_tokens(settings.revoked_tokens_file)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Revocation list unavailable") from exc
    if jti in revoked:
        raise HTTPException(status_code=401, detail="Token has been revoked")

    return sub
