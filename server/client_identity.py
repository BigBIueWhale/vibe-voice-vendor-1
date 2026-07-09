from __future__ import annotations

import re

from fastapi import HTTPException, Request

CLIENT_IDENTITY_HEADER = "x-vvv-client-spki-sha256"
_CLIENT_IDENTITY_RE = re.compile(r"^[a-f0-9]{64}$")


def require_client_identity(request: Request) -> str:
    values = request.headers.getlist(CLIENT_IDENTITY_HEADER)
    if len(values) != 1:
        raise HTTPException(status_code=403, detail="mTLS client identity required")
    identity = values[0]
    if _CLIENT_IDENTITY_RE.fullmatch(identity) is None:
        raise HTTPException(status_code=403, detail="mTLS client identity invalid")
    return identity
