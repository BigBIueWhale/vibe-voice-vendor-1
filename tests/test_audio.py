import base64
import shutil
import struct

import pytest

from server.audio import _wav_duration, convert_audio_to_wav_base64

has_ffmpeg = shutil.which("ffmpeg") is not None


def _make_wav(
    sample_rate: int = 16000, num_samples: int = 16000, num_channels: int = 1
) -> bytes:
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


def test_wav_duration_one_second() -> None:
    wav = _make_wav(sample_rate=16000, num_samples=16000)
    duration = _wav_duration(wav)
    assert abs(duration - 1.0) < 0.001


def test_wav_duration_half_second() -> None:
    wav = _make_wav(sample_rate=16000, num_samples=8000)
    duration = _wav_duration(wav)
    assert abs(duration - 0.5) < 0.001


def test_wav_duration_too_short() -> None:
    with pytest.raises(RuntimeError, match="too short"):
        _wav_duration(b"short")


@pytest.mark.skipif(not has_ffmpeg, reason="FFmpeg not installed")
async def test_convert_audio_ffmpeg() -> None:
    """Test FFmpeg conversion with a valid WAV input."""
    wav_bytes = _make_wav(sample_rate=44100, num_samples=44100)
    wav_b64, duration = await convert_audio_to_wav_base64(wav_bytes)

    # Should produce valid base64
    decoded = base64.b64decode(wav_b64)
    assert decoded[:4] == b"RIFF"

    # Duration should be approximately 1 second
    assert abs(duration - 1.0) < 0.1


@pytest.mark.skipif(not has_ffmpeg, reason="FFmpeg not installed")
async def test_convert_invalid_audio() -> None:
    """FFmpeg should fail on garbage input."""
    with pytest.raises(RuntimeError, match="FFmpeg conversion failed"):
        await convert_audio_to_wav_base64(b"not audio data at all")
