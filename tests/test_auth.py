import bcrypt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from server.auth import verify_token
from server.config import Settings

TEST_TOKEN = "test-token-for-unit-tests-1234"
TEST_TOKEN_HASH = bcrypt.hashpw(
    TEST_TOKEN.encode("utf-8"), bcrypt.gensalt(rounds=4)
).decode("utf-8")


def _make_settings(hashes: str) -> Settings:
    return Settings(token_hashes_env=hashes)


def test_valid_token() -> None:
    settings = _make_settings(TEST_TOKEN_HASH)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=TEST_TOKEN)
    fingerprint = verify_token(creds, settings)
    assert fingerprint == TEST_TOKEN[:8]


def test_invalid_token() -> None:
    settings = _make_settings(TEST_TOKEN_HASH)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-token")
    with pytest.raises(HTTPException) as exc_info:
        verify_token(creds, settings)
    assert exc_info.value.status_code == 401


def test_empty_hashes() -> None:
    settings = _make_settings("")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=TEST_TOKEN)
    with pytest.raises(HTTPException) as exc_info:
        verify_token(creds, settings)
    assert exc_info.value.status_code == 401


def test_multiple_hashes() -> None:
    other_token = "other-token-abcdefgh"
    other_hash = bcrypt.hashpw(
        other_token.encode("utf-8"), bcrypt.gensalt(rounds=4)
    ).decode("utf-8")
    settings = _make_settings(f"{other_hash},{TEST_TOKEN_HASH}")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=TEST_TOKEN)
    fingerprint = verify_token(creds, settings)
    assert fingerprint == TEST_TOKEN[:8]


def test_malformed_hash_skipped() -> None:
    settings = _make_settings(f"not-a-valid-hash,{TEST_TOKEN_HASH}")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=TEST_TOKEN)
    fingerprint = verify_token(creds, settings)
    assert fingerprint == TEST_TOKEN[:8]
