import asyncio
import base64
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from server.audio import (
    HELIBOARD_WAV_MIME_TYPE,
    compress_file_to_opus,
    encode_audio_base64,
    read_heliboard_wav_info,
    validate_heliboard_wav_metadata,
)

WavMutator = Callable[[bytes], bytes]


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


def test_validate_heliboard_wav_metadata_accepts_exact_contract() -> None:
    assert (
        validate_heliboard_wav_metadata("recording_20260710_121314_123.wav", "audio/wav")
        == HELIBOARD_WAV_MIME_TYPE
    )


@pytest.mark.parametrize(
    ("filename", "content_type", "message"),
    [
        (None, "audio/wav", "filename"),
        ("", "audio/wav", "filename"),
        ("recording.WAV", "audio/wav", r"\.wav"),
        ("recording.mp3", "audio/mpeg", r"\.wav"),
        ("recording.wav", None, "Content-Type"),
        ("recording.wav", "application/octet-stream", "audio/wav"),
        ("recording.wav", "audio/x-wav", "audio/wav"),
    ],
)
def test_validate_heliboard_wav_metadata_rejects_non_contract_values(
    filename: str | None,
    content_type: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_heliboard_wav_metadata(filename, content_type)


def test_read_heliboard_wav_info_accepts_exact_16khz_mono_pcm(tmp_path: Path) -> None:
    path = tmp_path / "recording.wav"
    path.write_bytes(_make_wav(sample_rate=16000, num_samples=16000))

    info = read_heliboard_wav_info(str(path))

    assert info.duration_seconds == 1.0
    assert info.data_size == 32000


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda b: b[:0] + b"NOPE" + b[4:], "RIFF"),
        (lambda b: b[:8] + b"NOPE" + b[12:], "WAVE"),
        (lambda b: b[:12] + b"JUNK" + b[16:], "fmt"),
        (lambda b: b[:36] + b"JUNK" + b[40:], "data"),
        (lambda b: b[:20] + struct.pack("<H", 3) + b[22:], "PCM"),
        (lambda b: b[:22] + struct.pack("<H", 2) + b[24:], "mono"),
        (lambda b: b[:24] + struct.pack("<I", 8000) + b[28:], "16000"),
        (lambda b: b[:28] + struct.pack("<I", 16000) + b[32:], "32000"),
        (lambda b: b[:32] + struct.pack("<H", 4) + b[34:], "block align"),
        (lambda b: b[:34] + struct.pack("<H", 24) + b[36:], "16"),
        (lambda b: b[:40] + struct.pack("<I", 0) + b[44:], "data size"),
        (lambda b: b[:43], "too short"),
        (lambda b: b + b"\x00", "RIFF size"),
    ],
)
def test_read_heliboard_wav_info_rejects_non_canonical_wav(
    tmp_path: Path,
    mutator: WavMutator,
    message: str,
) -> None:
    path = tmp_path / "recording.wav"
    path.write_bytes(mutator(_make_wav(sample_rate=16000, num_samples=16000)))

    with pytest.raises(ValueError, match=message):
        read_heliboard_wav_info(str(path))


def test_read_heliboard_wav_info_rejects_extra_chunks(tmp_path: Path) -> None:
    wav = _make_wav(sample_rate=16000, num_samples=16000)
    inserted_chunk = b"LIST\x04\x00\x00\x00abcd"
    file_size = len(wav) + len(inserted_chunk)
    with_list_chunk = (
        wav[:4] + struct.pack("<I", file_size - 8) + wav[8:36] + inserted_chunk + wav[36:]
    )
    path = tmp_path / "recording.wav"
    path.write_bytes(with_list_chunk)

    with pytest.raises(ValueError, match="data chunk"):
        read_heliboard_wav_info(str(path))


def test_read_heliboard_wav_info_rejects_empty_data_chunk(tmp_path: Path) -> None:
    path = tmp_path / "recording.wav"
    path.write_bytes(_make_wav(sample_rate=16000, num_samples=0))

    with pytest.raises(ValueError, match="empty"):
        read_heliboard_wav_info(str(path))


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
