from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from client.client import VibevoiceClient
from client.models import EventType


async def _transcribe(
    client: VibevoiceClient,
    audio_path: str,
    hotwords: str | None,
    output: str | None,
) -> None:
    output_file = open(output, "w") if output else None  # noqa: SIM115
    try:
        async for event in client.transcribe(audio_path, hotwords):
            if event.event_type == EventType.QUEUE:
                print(
                    f"[Queue] Position: {event.position}, "
                    f"ETA: {event.estimated_wait_seconds:.0f}s",
                    file=sys.stderr,
                )
            elif event.event_type == EventType.DATA:
                text = event.text or ""
                print(text, end="", flush=True)
                if output_file:
                    output_file.write(text)
            elif event.event_type == EventType.ERROR:
                print(f"\n[Error] {event.error}", file=sys.stderr)
                sys.exit(1)
            elif event.event_type == EventType.DONE:
                print()  # Final newline
    finally:
        if output_file:
            output_file.close()


async def _status(client: VibevoiceClient) -> None:
    info = await client.queue_status()
    your_jobs: Any = info.get("your_jobs", [])
    total = info.get("total_queued", 0)

    print(f"Total queued: {total}")
    if your_jobs:
        print("Your jobs:")
        for job in your_jobs:
            status = job.get("status", "unknown")
            pos = job.get("position")
            eta = job.get("estimated_wait_seconds")
            job_id = job.get("job_id", "?")
            parts = [f"  {job_id[:8]}... status={status}"]
            if pos is not None:
                parts.append(f"position={pos}")
            if eta is not None:
                parts.append(f"eta={eta:.0f}s")
            print(" ".join(parts))
    else:
        print("No active jobs.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="vvv", description="VibeVoice ASR client")
    parser.add_argument(
        "--server", required=True, help="Server URL (e.g. https://asr.example.com)"
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("VVV_TOKEN"),
        help="Bearer token (default: VVV_TOKEN env var)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    transcribe_parser = subparsers.add_parser("transcribe", help="Transcribe an audio file")
    transcribe_parser.add_argument("file", help="Path to audio file")
    transcribe_parser.add_argument("--hotwords", help="Comma-separated hotwords")
    transcribe_parser.add_argument("--output", help="Output file path")

    subparsers.add_parser("status", help="Check queue status")

    args = parser.parse_args()

    if not args.token:
        print("Error: provide --token or set VVV_TOKEN env var", file=sys.stderr)
        sys.exit(1)

    client = VibevoiceClient(base_url=args.server, token=args.token)

    if args.command == "transcribe":
        if not Path(args.file).exists():
            print(f"File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        asyncio.run(_transcribe(client, args.file, args.hotwords, args.output))
    elif args.command == "status":
        asyncio.run(_status(client))
