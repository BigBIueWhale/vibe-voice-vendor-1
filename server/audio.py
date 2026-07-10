import asyncio
import base64
import contextlib
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_FFMPEG_TIMEOUT_SECONDS = 900.0
_FFMPEG_THREADS = "1"

HELIBOARD_WAV_MIME_TYPE = "audio/wav"
HELIBOARD_WAV_SUFFIX = ".wav"
_HELIBOARD_WAV_HEADER_BYTES = 44
_HELIBOARD_WAV_SAMPLE_RATE = 16000
_HELIBOARD_WAV_CHANNELS = 1
_HELIBOARD_WAV_BITS_PER_SAMPLE = 16
_HELIBOARD_WAV_BLOCK_ALIGN = 2
_HELIBOARD_WAV_BYTE_RATE = 32000
_HELIBOARD_WAV_DATA_BYTES_PER_SECOND = _HELIBOARD_WAV_BYTE_RATE


@dataclass(frozen=True)
class HeliboardWavInfo:
    duration_seconds: float
    data_size: int


async def _communicate_or_kill(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    operation: str,
) -> tuple[bytes, bytes]:
    async def kill_and_drain() -> None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.communicate()

    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await kill_and_drain()
        timeout_label = f"{timeout_seconds:.3g}"
        raise RuntimeError(f"{operation} timed out after {timeout_label} seconds") from None
    except asyncio.CancelledError:
        await kill_and_drain()
        raise


def encode_audio_base64(raw_bytes: bytes) -> str:
    """Base64-encode raw audio bytes without any conversion."""
    return base64.b64encode(raw_bytes).decode("ascii")


def encode_audio_file_base64(path: str) -> str:
    """Base64-encode an audio file at worker time."""
    return encode_audio_base64(Path(path).read_bytes())


def validate_heliboard_wav_metadata(filename: str | None, content_type: str | None) -> str:
    """Validate the public upload metadata for the single supported wire format."""
    if filename is None or filename == "":
        raise ValueError("Audio file must include a filename")
    suffix = PurePosixPath(filename).suffix
    if suffix != HELIBOARD_WAV_SUFFIX:
        raise ValueError("Audio filename must end in .wav")
    if content_type != HELIBOARD_WAV_MIME_TYPE:
        raise ValueError("Audio part Content-Type must be audio/wav")
    return HELIBOARD_WAV_MIME_TYPE


def read_heliboard_wav_info(path: str) -> HeliboardWavInfo:
    """Read and validate the exact WAV container shape emitted by HeliBoard."""
    file_size = os.path.getsize(path)
    if file_size < _HELIBOARD_WAV_HEADER_BYTES:
        raise ValueError("WAV file is too short")

    with open(path, "rb") as f:
        header = f.read(_HELIBOARD_WAV_HEADER_BYTES)
    if len(header) != _HELIBOARD_WAV_HEADER_BYTES:
        raise ValueError("WAV header is incomplete")

    (
        riff_magic,
        riff_size,
        wave_magic,
        fmt_magic,
        fmt_size,
        audio_format,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        data_magic,
        data_size,
    ) = struct.unpack("<4sI4s4sIHHIIHH4sI", header)

    if riff_magic != b"RIFF":
        raise ValueError("WAV header must start with RIFF")
    if riff_size != file_size - 8:
        raise ValueError("WAV RIFF size does not match file length")
    if wave_magic != b"WAVE":
        raise ValueError("WAV header must declare WAVE")
    if fmt_magic != b"fmt ":
        raise ValueError("WAV fmt chunk must start at byte 12")
    if fmt_size != 16:
        raise ValueError("WAV fmt chunk must be canonical PCM size 16")
    if audio_format != 1:
        raise ValueError("WAV audio format must be PCM")
    if channels != _HELIBOARD_WAV_CHANNELS:
        raise ValueError("WAV must be mono")
    if sample_rate != _HELIBOARD_WAV_SAMPLE_RATE:
        raise ValueError("WAV sample rate must be 16000 Hz")
    if byte_rate != _HELIBOARD_WAV_BYTE_RATE:
        raise ValueError("WAV byte rate must be 32000")
    if block_align != _HELIBOARD_WAV_BLOCK_ALIGN:
        raise ValueError("WAV block align must be 2")
    if bits_per_sample != _HELIBOARD_WAV_BITS_PER_SAMPLE:
        raise ValueError("WAV bits per sample must be 16")
    if data_magic != b"data":
        raise ValueError("WAV data chunk must start at byte 36")
    if data_size != file_size - _HELIBOARD_WAV_HEADER_BYTES:
        raise ValueError("WAV data size does not match file length")
    if data_size == 0:
        raise ValueError("WAV data chunk is empty")
    if data_size % _HELIBOARD_WAV_BLOCK_ALIGN != 0:
        raise ValueError("WAV data size must align to 16-bit mono samples")

    return HeliboardWavInfo(
        duration_seconds=data_size / _HELIBOARD_WAV_DATA_BYTES_PER_SECOND,
        data_size=data_size,
    )


async def compress_to_opus(raw_bytes: bytes) -> bytes:
    """Compress audio to OGG/Opus via ffmpeg. Keeps file size small for cloud APIs."""
    with tempfile.NamedTemporaryFile(suffix=".audio") as src:
        src.write(raw_bytes)
        src.flush()
        return await compress_file_to_opus(src.name)


async def compress_file_to_opus(path: str) -> bytes:
    """Compress an audio file to OGG/Opus via ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as dst:
        dst_path = dst.name

    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-nostdin",
            "-threads",
            _FFMPEG_THREADS,
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "64k",
            dst_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_data = await _communicate_or_kill(
            process,
            timeout_seconds=_FFMPEG_TIMEOUT_SECONDS,
            operation="ffmpeg opus compression",
        )
        if process.returncode != 0:
            err = stderr_data.decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg opus compression failed: {err}")

        with open(dst_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(dst_path)
