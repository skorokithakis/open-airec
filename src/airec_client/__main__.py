"""Query and control an explicitly selected AIREC recorder."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime
from dataclasses import asdict

from bleak import BleakScanner

from . import AirecClient, save_audio
from .client import _validate_recording_id


async def run(args):
    destination = None
    if args.command == "download":
        _validate_recording_id(args.recording_id)
        suffix = ".opus" if args.format == "opus" else ".airec"
        destination = args.output or Path("recordings") / (args.recording_id + suffix)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"refusing to overwrite {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "delete":
        _validate_recording_id(args.recording_id)
        if not args.yes:
            raise ValueError("deletion is permanent; pass --yes to confirm")
    clock_value = None
    if args.command == "set-clock" and args.time:
        clock_value = datetime.fromisoformat(args.time)
        if clock_value.tzinfo is not None:
            raise ValueError("supply device-local time without a timezone offset")
    device = await BleakScanner.find_device_by_address(args.address, timeout=args.timeout)
    if device is None:
        raise ConnectionError("selected recorder is not advertising")
    async with AirecClient(device, timeout=args.timeout) as client:
        if args.command == "storage":
            storage = await client.storage()
            print(json.dumps({**asdict(storage), "used_mb": storage.used_mb}, indent=2))
            return
        controls = {"status": client.recording_status, "start": client.start_recording,
                    "pause": client.pause_recording, "resume": client.resume_recording,
                    "stop": client.stop_recording}
        if args.command in controls:
            result = await controls[args.command]()
            if args.command == "stop":
                result = {"finalized_recording_id": result.recording_id if result else None}
            else:
                result = asdict(result)
            print(json.dumps(result, indent=2))
            return
        if args.command == "delete":
            await client.delete_recording(args.recording_id)
            print(json.dumps({"deleted_recording_id": args.recording_id}))
            return
        if args.command in ("clock", "set-clock"):
            value = await client.set_clock(clock_value) if args.command == "set-clock" else await client.clock()
            print(json.dumps({"device_local_time": value.isoformat()}))
            return
        if args.command == "download":
            audio = await client.download_recording(
                args.recording_id, timeout=args.download_timeout,
                stop_if_recording=args.stop_recording,
            )
            save_audio(audio, destination, format=args.format)
            print(json.dumps({"recording_id": args.recording_id, "path": str(destination),
                              "raw_size_bytes": len(audio), "file_size_bytes": destination.stat().st_size}, indent=2))
            return
        battery = await client.battery()
        recordings = await client.list_recordings()
        print(json.dumps({
            "battery_percent": battery,
            "recordings": [{
                "recording_id": row.recording_id,
                "recorded_at": row.recorded_at.isoformat(),
                "size_bytes": row.size_bytes,
            } for row in recordings],
        }, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="explicit Bluetooth address (device UUID on macOS)")
    parser.add_argument("--timeout", type=float, default=10.0)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="list recording metadata (the default)")
    commands.add_parser("storage", help="read total, free and used storage in device-reported MB")
    for name, help_text in (("status", "read recording state"), ("start", "start ordinary recording"),
                            ("pause", "pause active recording"), ("resume", "resume paused recording"),
                            ("stop", "stop/finalize current recording"), ("clock", "read device clock")):
        commands.add_parser(name, help=help_text)
    delete = commands.add_parser("delete", help="permanently delete one archive; recorder must be stopped")
    delete.add_argument("recording_id")
    delete.add_argument("--yes", action="store_true", help="confirm permanent deletion")
    clock = commands.add_parser("set-clock", help="set device clock; recorder must be stopped")
    clock.add_argument("--time", help="local ISO datetime without timezone (default: computer local time)")
    download = commands.add_parser("download", help="download one recording; never deletes it")
    download.add_argument("recording_id", help="14-digit ID from the catalog")
    download.add_argument("--output", type=Path, help="destination (default: recordings/ID.opus)")
    download.add_argument("--format", choices=("opus", "raw"), default="opus")
    download.add_argument("--download-timeout", type=float, default=120.0)
    download.add_argument("--stop-recording", action="store_true",
                          help="allow stopping/finalizing an active or paused recording; does not restart it")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (Exception, KeyboardInterrupt) as exc:
        print(f"AIREC operation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
