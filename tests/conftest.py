import bcrypt
import pytest

from server.app import create_app
from server.config import Settings

TEST_TOKEN = "test-token-for-unit-tests-1234"
TEST_TOKEN_HASH = bcrypt.hashpw(
    TEST_TOKEN.encode("utf-8"), bcrypt.gensalt(rounds=4)
).decode("utf-8")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        vllm_base_url="http://localhost:9999",
        token_hashes_env=TEST_TOKEN_HASH,
        max_queue_size=5,
    )


@pytest.fixture
def app(settings: Settings):  # type: ignore[no-untyped-def]
    return create_app(settings=settings)
