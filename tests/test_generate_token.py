import stat
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from scripts.generate_token import main, validate_artifacts


def test_generate_token_permissions_and_no_default_secret_print(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keys_dir = tmp_path / "keys"
    with patch("sys.argv", ["generate_token", "--keys-dir", str(keys_dir), "--subject", "user"]):
        main()

    private_key = keys_dir / "private.pem"
    public_key = keys_dir / "public.pem"
    token = keys_dir / "token.txt"

    assert private_key.exists()
    assert public_key.exists()
    assert token.exists()
    assert (keys_dir.stat().st_mode & 0o777) == stat.S_IRWXU
    assert (private_key.stat().st_mode & 0o777) == stat.S_IRUSR | stat.S_IWUSR
    assert (token.stat().st_mode & 0o777) == stat.S_IRUSR | stat.S_IWUSR
    assert (public_key.stat().st_mode & 0o777) == (
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
    )

    output = capsys.readouterr().out
    assert "Token:" not in output
    assert "Saved to:" in output
    assert "Expires in:" in output

    public = load_pem_public_key(public_key.read_bytes())
    payload = jwt.decode(
        token.read_text().strip(),
        public,  # type: ignore[arg-type]
        algorithms=["ES256"],
        options={"require": ["sub", "jti", "iat", "nbf", "exp"]},
    )
    assert payload["sub"] == "user"
    assert isinstance(payload["jti"], str)
    assert payload["iat"] <= payload["nbf"] <= payload["exp"]
    assert validate_artifacts(keys_dir)["sub"] == "user"


def test_generate_token_can_explicitly_print_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keys_dir = tmp_path / "keys"
    with patch(
        "sys.argv",
        ["generate_token", "--keys-dir", str(keys_dir), "--subject", "user", "--print-token"],
    ):
        main()

    output = capsys.readouterr().out
    assert "Token:" in output


def test_generate_token_refuses_partial_artifact_state(tmp_path: Path) -> None:
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(mode=0o700)
    (keys_dir / "private.pem").write_text("partial")
    (keys_dir / "private.pem").chmod(0o600)

    with (
        patch("sys.argv", ["generate_token", "--keys-dir", str(keys_dir), "--subject", "user"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert "Partial JWT artifact state exists" in str(exc_info.value)


def test_generate_token_refuses_bad_existing_directory_mode(tmp_path: Path) -> None:
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(mode=0o755)

    with (
        patch("sys.argv", ["generate_token", "--keys-dir", str(keys_dir), "--subject", "user"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert "expected 700" in str(exc_info.value)
