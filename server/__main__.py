import argparse
import contextlib
import os
import socket
from pathlib import Path

import uvicorn

from server.app import create_app
from server.config import Settings

_UVICORN_BACKLOG = 64
_UVICORN_LIMIT_CONCURRENCY = 128
_UVICORN_HEADER_BYTES = 16 * 1024


def _parse_bool(value: str) -> bool:
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"Expected true/false, got: {value}")


def _bind_private_uds(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.listen(_UVICORN_BACKLOG)
        sock.setblocking(False)
    except Exception:
        sock.close()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise
    return sock


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vvv-server",
        description="VibeVoice ASR Server",
    )
    parser.add_argument(
        "--asr-backend",
        choices=["vibevoice", "groq"],
        default="vibevoice",
        help="ASR backend: local vLLM VibeVoice or Groq Whisper cloud API (default: vibevoice)",
    )
    parser.add_argument(
        "--uds",
        required=True,
        help="Private Unix domain socket path to bind",
    )
    parser.add_argument(
        "--max-audio-bytes", type=int, required=True, help="Maximum audio upload size in bytes"
    )
    parser.add_argument(
        "--max-queue-size", type=int, required=True, help="Maximum number of queued jobs"
    )
    parser.add_argument(
        "--jwt-public-key-file", required=True, help="Path to ES256 public key PEM file"
    )
    parser.add_argument(
        "--revoked-tokens-file", required=True, help="Path to file listing revoked JTI values"
    )
    parser.add_argument(
        "--require-https",
        type=_parse_bool,
        required=True,
        help="Reject non-HTTPS requests (true/false)",
    )
    # vLLM / VibeVoice options (required when --asr-backend vibevoice)
    parser.add_argument("--vllm-base-url", default="", help="vLLM server base URL")
    parser.add_argument("--vllm-model-name", default="vibevoice", help="Model name for vLLM")
    parser.add_argument(
        "--vllm-temperature", type=float, default=0.0, help="Generation temperature"
    )
    parser.add_argument("--vllm-top-p", type=float, default=1.0, help="Top-P sampling parameter")
    # Groq Whisper options (required when --asr-backend groq)
    parser.add_argument("--groq-api-key", default="", help="Groq API key")
    parser.add_argument(
        "--groq-model-name", default="whisper-large-v3", help="Groq Whisper model name"
    )

    args = parser.parse_args()

    if args.asr_backend == "vibevoice" and not args.vllm_base_url:
        parser.error("--vllm-base-url is required when --asr-backend is vibevoice")
    if args.asr_backend == "groq" and not args.groq_api_key:
        parser.error("--groq-api-key is required when --asr-backend is groq")

    settings = Settings(
        asr_backend=args.asr_backend,
        max_audio_bytes=args.max_audio_bytes,
        max_queue_size=args.max_queue_size,
        jwt_public_key_file=args.jwt_public_key_file,
        revoked_tokens_file=args.revoked_tokens_file,
        require_https=args.require_https,
        vllm_base_url=args.vllm_base_url,
        vllm_model_name=args.vllm_model_name,
        vllm_temperature=args.vllm_temperature,
        vllm_top_p=args.vllm_top_p,
        groq_api_key=args.groq_api_key,
        groq_model_name=args.groq_model_name,
    )

    app = create_app(settings)
    uds_path = Path(args.uds)
    sock = _bind_private_uds(uds_path)
    try:
        uvicorn.run(
            app,
            fd=sock.fileno(),
            log_level="warning",
            access_log=False,
            http="h11",
            ws="none",
            proxy_headers=False,
            server_header=False,
            date_header=False,
            backlog=_UVICORN_BACKLOG,
            limit_concurrency=_UVICORN_LIMIT_CONCURRENCY,
            timeout_keep_alive=1,
            h11_max_incomplete_event_size=_UVICORN_HEADER_BYTES,
        )
    finally:
        sock.close()
        with contextlib.suppress(FileNotFoundError):
            uds_path.unlink()


if __name__ == "__main__":
    main()
