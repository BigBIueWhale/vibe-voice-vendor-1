from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from server.config import Settings

_bearer_scheme = HTTPBearer()


def _get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def verify_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    settings: Annotated[Settings, Depends(_get_settings)],
) -> str:
    """Verify bearer token against bcrypt hashes. Returns first 8 chars as fingerprint."""
    raw_token = credentials.credentials.encode("utf-8")
    for token_hash in settings.token_hashes:
        try:
            if bcrypt.checkpw(raw_token, token_hash.encode("utf-8")):
                return credentials.credentials[:8]
        except (ValueError, TypeError):
            continue
    raise HTTPException(status_code=401, detail="Invalid token")
