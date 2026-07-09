import stat
from pathlib import Path

from server.__main__ import _bind_private_uds


def test_bind_private_uds_creates_private_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "run" / "server.sock"

    sock = _bind_private_uds(socket_path)
    try:
        parent_mode = stat.S_IMODE(socket_path.parent.stat().st_mode)
        socket_mode = stat.S_IMODE(socket_path.stat().st_mode)

        assert parent_mode == 0o700
        assert socket_mode == 0o600
        assert stat.S_ISSOCK(socket_path.stat().st_mode)
    finally:
        sock.close()
        socket_path.unlink(missing_ok=True)
