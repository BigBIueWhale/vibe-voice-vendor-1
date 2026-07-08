import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.generate_token import main


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
