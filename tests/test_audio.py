import asyncio
import base64
import shutil
import struct
from pathlib import Path
from typing import Any

import pytest

from server.audio import (
    compress_file_to_opus,
    detect_mime_type,
    encode_audio_base64,
    probe_duration,
    probe_duration_file,
)

has_ffprobe = shutil.which("ffprobe") is not None


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _HangingProcess:
    def __init__(self) -> None:
        self.killed = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.killed:
            return b"", b""
        await asyncio.sleep(3600)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _CancelledProcess:
    def __init__(self) -> None:
        self.killed = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.killed:
            return b"", b""
        raise asyncio.CancelledError

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _make_wav(sample_rate: int = 16000, num_samples: int = 16000, num_channels: int = 1) -> bytes:
    """Create a minimal valid WAV file."""
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = num_samples * num_channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,  # fmt chunk size
        1,  # PCM
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    # Fill with silence
    audio_data = b"\x00" * data_size
    return header + audio_data


def test_encode_audio_base64_roundtrip() -> None:
    raw = b"hello audio bytes"
    encoded = encode_audio_base64(raw)
    assert base64.b64decode(encoded) == raw


def test_detect_mime_type_wav() -> None:
    assert detect_mime_type("recording.wav") == "audio/wav"


def test_detect_mime_type_mp3() -> None:
    assert detect_mime_type("song.mp3") == "audio/mpeg"


def test_detect_mime_type_flac() -> None:
    assert detect_mime_type("track.flac") == "audio/flac"


def test_detect_mime_type_ogg() -> None:
    assert detect_mime_type("voice.ogg") == "audio/ogg"


def test_detect_mime_type_opus() -> None:
    assert detect_mime_type("voice.opus") == "audio/ogg"


def test_detect_mime_type_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unrecognized audio extension"):
        detect_mime_type("data.xyz")


def test_detect_mime_type_case_insensitive() -> None:
    assert detect_mime_type("FILE.WAV") == "audio/wav"
    assert detect_mime_type("track.MP3") == "audio/mpeg"


@pytest.mark.skipif(not has_ffprobe, reason="ffprobe not installed")
async def test_probe_duration_wav() -> None:
    wav_bytes = _make_wav(sample_rate=16000, num_samples=16000)
    duration = await probe_duration(wav_bytes)
    assert abs(duration - 1.0) < 0.1


@pytest.mark.skipif(not has_ffprobe, reason="ffprobe not installed")
async def test_probe_duration_half_second() -> None:
    wav_bytes = _make_wav(sample_rate=16000, num_samples=8000)
    duration = await probe_duration(wav_bytes)
    assert abs(duration - 0.5) < 0.1


@pytest.mark.skipif(not has_ffprobe, reason="ffprobe not installed")
async def test_probe_duration_invalid() -> None:
    with pytest.raises(RuntimeError, match="ffprobe failed"):
        await probe_duration(b"not audio data at all")


async def test_probe_duration_rejects_malformed_ffprobe_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(b"not json")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="ffprobe output is not valid JSON"):
        await probe_duration_file("audio.wav")


async def test_probe_duration_rejects_non_numeric_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(b'{"format":{"duration":"nope"}}')

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="ffprobe duration is not numeric"):
        await probe_duration_file("audio.wav")


async def test_probe_duration_uses_local_file_protocol_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_args: list[str] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured_args[:] = [str(arg) for arg in args]
        return _FakeProcess(b'{"format":{"duration":"1.25"}}')

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await probe_duration_file("audio.wav") == 1.25
    assert captured_args[:5] == [
        "ffprobe",
        "-v",
        "quiet",
        "-protocol_whitelist",
        "file",
    ]


async def test_probe_duration_timeout_kills_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HangingProcess()

    async def fake_exec(*args: Any, **kwargs: Any) -> _HangingProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("server.audio._FFPROBE_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(RuntimeError, match="ffprobe timed out"):
        await probe_duration_file("audio.wav")

    assert process.killed


async def test_compress_uses_bounded_local_ffmpeg_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_args: list[str] = []
    src = tmp_path / "audio.wav"
    src.write_bytes(b"fake audio")

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured_args[:] = [str(arg) for arg in args]
        return _FakeProcess(b"")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    await compress_file_to_opus(str(src))

    assert captured_args[:9] == [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-threads",
        "1",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(src),
    ]


async def test_compress_timeout_kills_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _HangingProcess()
    src = tmp_path / "audio.wav"
    src.write_bytes(b"fake audio")

    async def fake_exec(*args: Any, **kwargs: Any) -> _HangingProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("server.audio._FFMPEG_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(RuntimeError, match="ffmpeg opus compression timed out"):
        await compress_file_to_opus(str(src))

    assert process.killed


async def test_compress_cancellation_kills_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _CancelledProcess()
    src = tmp_path / "audio.wav"
    src.write_bytes(b"fake audio")

    async def fake_exec(*args: Any, **kwargs: Any) -> _CancelledProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(asyncio.CancelledError):
        await compress_file_to_opus(str(src))

    assert process.killed
