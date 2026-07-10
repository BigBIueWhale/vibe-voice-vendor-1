import asyncio
import base64
import contextlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

_FFPROBE_TIMEOUT_SECONDS = 120.0
_FFMPEG_TIMEOUT_SECONDS = 900.0
_FFMPEG_THREADS = "1"

_MIME_MAP = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".wma": "audio/x-ms-wma",
    ".aac": "audio/aac",
}


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


def detect_mime_type(filename: str) -> str:
    """Detect audio MIME type from filename extension.

    Raises ValueError if the extension is not recognized.
    The caller must provide a filename with a supported audio extension.
    """
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in _MIME_MAP:
        raise ValueError(
            f"Unrecognized audio extension '{suffix}' in filename '{filename}'. "
            f"Supported extensions: {', '.join(sorted(_MIME_MAP.keys()))}"
        )
    return _MIME_MAP[suffix]


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


async def probe_duration(raw_bytes: bytes) -> float:
    """Get audio duration in seconds via ffprobe without transcoding.

    Uses a temp file instead of stdin pipe because ffprobe cannot determine
    duration for some formats (e.g. WAV) when reading from a pipe.
    """
    with tempfile.NamedTemporaryFile(suffix=".audio") as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        return await probe_duration_file(tmp.name)


async def probe_duration_file(path: str) -> float:
    """Get audio duration in seconds via ffprobe for a file path."""
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-protocol_whitelist",
        "file",
        "-print_format",
        "json",
        "-show_format",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_or_kill(
        process,
        timeout_seconds=_FFPROBE_TIMEOUT_SECONDS,
        operation="ffprobe",
    )

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"ffprobe failed: {error_msg}")

    try:
        info = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe output is not valid JSON: {exc}") from exc

    if not isinstance(info, dict) or "format" not in info:
        raise RuntimeError("ffprobe output missing 'format' key")
    format_info = info["format"]
    if not isinstance(format_info, dict):
        raise RuntimeError("ffprobe output 'format' is not an object")
    if "duration" not in format_info:
        raise RuntimeError("ffprobe could not determine audio duration")
    try:
        return float(format_info["duration"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ffprobe duration is not numeric: {format_info['duration']!r}") from exc
