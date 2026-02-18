import asyncio
import base64
import struct


async def convert_audio_to_wav_base64(input_bytes: bytes) -> tuple[str, float]:
    """Convert any audio/video format to 16kHz mono WAV via FFmpeg (pipe-to-pipe).

    Returns (base64-encoded WAV, duration in seconds).
    """
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-i", "pipe:0",
        "-ar", "16000",
        "-ac", "1",
        "-f", "wav",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(input=input_bytes)

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"FFmpeg conversion failed: {error_msg}")

    duration = _wav_duration(stdout)
    wav_b64 = base64.b64encode(stdout).decode("ascii")
    return wav_b64, duration


def _wav_duration(wav_bytes: bytes) -> float:
    """Extract duration from WAV header."""
    if len(wav_bytes) < 44:
        raise RuntimeError("Invalid WAV data: too short")

    # WAV header: bytes 24-27 = sample rate, bytes 34-35 = bits per sample
    # bytes 40-43 = data chunk size
    sample_rate = struct.unpack_from("<I", wav_bytes, 24)[0]
    bits_per_sample = struct.unpack_from("<H", wav_bytes, 34)[0]
    num_channels = struct.unpack_from("<H", wav_bytes, 22)[0]

    if sample_rate == 0 or bits_per_sample == 0 or num_channels == 0:
        raise RuntimeError("Invalid WAV header values")

    # Find the 'data' chunk
    offset = 12  # skip RIFF header
    while offset < len(wav_bytes) - 8:
        chunk_id = wav_bytes[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, offset + 4)[0]
        if chunk_id == b"data":
            bytes_per_sample = bits_per_sample // 8
            total_samples: int = chunk_size // (bytes_per_sample * num_channels)
            return float(total_samples / sample_rate)
        offset += 8 + chunk_size

    raise RuntimeError("No data chunk found in WAV")
