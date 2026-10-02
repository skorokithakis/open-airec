"""Query and control an AIREC voice recorder over Bluetooth."""

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path
from datetime import datetime
from dataclasses import asdict

from bleak import BleakScanner

from . import AirecClient
from .audio import OggOpusWriter
from .client import (DownloadInterrupted, SETTING_NAMES, parse_setting_value,
                     _validate_recording_id)
from .report import render_json, render_sync_event, render_text
from .scan import find_recorders
from .sync import (SyncFailed, sync_directory, _DEFAULT_DOWNLOAD_TIMEOUT,
                   _download_with_resume)


async def _select_device(address, timeout):
    """Resolve the target BLEDevice, scanning only when no address is configured.

    An explicit address (or AIREC_ADDRESS) is looked up directly. Otherwise a
    scan must yield exactly one AIREC recorder; zero or multiple matches abort
    with a listing rather than guessing.
    """
    address = address or os.environ.get("AIREC_ADDRESS")
    if address:
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise ConnectionError("selected recorder is not advertising")
        return device
    recorders = await find_recorders(timeout)
    if not recorders:
        raise ConnectionError("no AIREC recorder found; pass --address or set AIREC_ADDRESS")
    if len(recorders) > 1:
        found = ", ".join(f"{r.name} ({r.address})" for r in recorders)
        raise ConnectionError(f"multiple AIREC recorders found; pass --address: {found}")
    return recorders[0].device


def _emit(command, payload, as_json):
    if as_json:
        print(render_json(command, payload))
    else:
        print(render_text(command, payload))


async def _info_payload(client):
    """Collect every read-only info query; one failure aborts the whole command."""
    version = await client.firmware_version()
    firmware_type = await client.firmware_type()
    chip = await client.chip_info()
    charging = await client.is_charging()
    settings = await client.device_settings()
    return {
        "firmware_version": version,
        "firmware_type": firmware_type,
        "chip_info": {
            "work_mode": chip.work_mode,
            "work_mode_name": chip.work_mode_name,
            "audio_format": chip.audio_format,
            "audio_format_name": chip.audio_format_name,
        },
        "is_charging": charging,
        "device_settings": {**asdict(settings), "raw": settings.raw.hex()},
    }


def _binary_stdout():
    """Byte stream used by ``listen -o -``; a seam for tests and broken pipes."""
    return sys.stdout.buffer


def _finish_quietly(writer):
    """Best-effort EOS page; a closed pipe must not replace the real outcome."""
    try:
        writer.finish()
    except (OSError, ValueError):
        pass


def _flush_quietly(stream):
    try:
        stream.flush()
    except (OSError, ValueError):
        pass


def _silence_stdout():
    """Point fd 1 at the null device after a broken pipe so shutdown stays quiet."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def _listen_summary(packets, reason):
    # One live packet is one 20 ms frame (docs/research/live-audio.md).
    return f"Listened {packets * 0.02:.1f} s ({packets} packets); ended: {reason}."


async def _listen(client, output):
    """Stream live audio to ``output`` (``"-"`` is binary stdout) until it ends.

    Header and data pages are flushed as they are produced, so an interrupted or
    killed process leaves a playable prefix. A clean end and Ctrl-C write the EOS
    page; an error keeps the prefix without one and propagates. The summary goes
    to stderr only, so ``-o -`` carries nothing but audio on stdout.
    """
    target = None
    close_target = output != "-"
    writer = None
    packets = 0
    reason = "stream ended"
    print("Listening for live audio; press Ctrl-C to stop.", file=sys.stderr)
    try:
        target = _binary_stdout() if output == "-" else open(output, "xb")
        writer = OggOpusWriter(target)  # header pages go out inside the protected block
        target.flush()  # publish the Ogg header pages before any audio arrives
        async with contextlib.aclosing(client.live_audio()) as stream:
            async for packet in stream:
                writer.write_packet(packet)
                packets += 1
                target.flush()
        writer.finish()
        target.flush()
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Ctrl-C cancels the task; keep the prefix and close it with an EOS page.
        if writer is not None:
            _finish_quietly(writer)
        if target is not None:
            _flush_quietly(target)
        reason = "interrupted"
    except BrokenPipeError:
        # The player closed its end of the pipe; end quietly.
        reason = "output closed"
        _silence_stdout()
    except BaseException:
        reason = "error"
        raise
    finally:
        try:
            if target is not None and close_target:
                target.close()
        finally:
            print(_listen_summary(packets, reason), file=sys.stderr)


_CONTROL_METHODS = {"status": "recording_status", "start": "start_recording",
                    "pause": "pause_recording", "resume": "resume_recording",
                    "stop": "stop_recording"}


async def run(args):
    command = args.command or "list"
    if command == "scan":
        recorders = await find_recorders(args.timeout)
        _emit(command, {"recorders": [
            {"name": r.name, "address": r.address, "rssi": r.rssi} for r in recorders]}, args.json)
        return
    destination = None
    if command == "download":
        _validate_recording_id(args.recording_id)
        suffix = ".opus" if args.format == "opus" else ".airec"
        destination = args.output or Path("recordings") / (args.recording_id + suffix)
        scratch = destination.parent / f"{args.recording_id}.part"
        if destination.resolve() == scratch.resolve():
            raise ValueError(
                f"output path is reserved for the download scratch file: {scratch.name}")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"refusing to overwrite {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
    if command == "listen":
        if args.json:
            raise ValueError("listen writes audio to stdout and text to stderr; --json is not supported")
        if args.output != "-":
            listen_output = Path(args.output)
            listen_output.parent.mkdir(parents=True, exist_ok=True)
            if listen_output.exists() or listen_output.is_symlink():
                raise FileExistsError(f"refusing to overwrite {listen_output}")
    if command == "delete":
        _validate_recording_id(args.recording_id)
        if not args.yes:
            raise ValueError("deletion is permanent; pass --yes to confirm")
    if command == "sync" and args.delete_after and not args.yes:
        raise ValueError("deletion is permanent; pass --yes to confirm")
    clock_value = None
    if command == "set-clock" and args.time:
        clock_value = datetime.fromisoformat(args.time)
        if clock_value.tzinfo is not None:
            raise ValueError("supply device-local time without a timezone offset")
    setting_value = None
    if command == "set-setting":
        setting_value = parse_setting_value(args.name, args.value)
    device = await _select_device(args.address, args.timeout)
    async with AirecClient(device, timeout=args.timeout) as client:
        if command == "storage":
            storage = await client.storage()
            payload = {**asdict(storage), "used_mb": storage.used_mb}
        elif command in _CONTROL_METHODS:
            result = await getattr(client, _CONTROL_METHODS[command])()
            if command == "stop":
                payload = {"finalized_recording_id": result.recording_id if result else None}
            else:
                payload = asdict(result)
        elif command == "delete":
            await client.delete_recording(args.recording_id)
            payload = {"deleted_recording_id": args.recording_id}
        elif command in ("clock", "set-clock"):
            value = await client.set_clock(clock_value) if command == "set-clock" else await client.clock()
            payload = {"device_local_time": value.isoformat()}
        elif command == "set-setting":
            settings = await client.set_setting(args.name, setting_value)
            payload = {"setting": args.name, "value": setting_value,
                       "device_settings": {**asdict(settings), "raw": settings.raw.hex()}}
        elif command == "info":
            payload = await _info_payload(client)
        elif command == "listen":
            await _listen(client, args.output)
            return
        elif command == "download":
            matches = [row for row in await client.list_recordings()
                       if row.recording_id == args.recording_id]
            if len(matches) != 1:
                raise ValueError("recording ID is absent or ambiguous in the catalog")
            recording = matches[0]
            part = destination.parent / f"{args.recording_id}.part"
            try:
                transferred, resumed_from = await _download_with_resume(
                    client, recording, part, destination, format=args.format,
                    timeout=args.download_timeout, stop_if_recording=args.stop_recording)
            except DownloadInterrupted as exc:
                raise RuntimeError(
                    "download interrupted; rerun the same command to resume "
                    f"({type(exc).__name__}: {exc})") from exc
            payload = {"recording_id": args.recording_id, "path": str(destination),
                       "raw_size_bytes": recording.size_bytes,
                       "file_size_bytes": destination.stat().st_size,
                       "resumed_from": resumed_from or None, "bytes": transferred}
        elif command == "sync":
            def progress(event):
                if not args.json:
                    print(render_sync_event(asdict(event)))
            try:
                events = await sync_directory(
                    client, args.directory, format=args.format,
                    stop_recording=args.stop_recording, delete_after=args.delete_after,
                    download_timeout=args.download_timeout, progress=progress,
                )
            except SyncFailed as exc:
                # JSON consumers still get a structured record of what was done
                # before the run stopped; text mode keeps its stderr report.
                if args.json:
                    _emit(command, {"directory": str(args.directory), "format": args.format,
                                    "events": [asdict(event) for event in exc.events],
                                    "error": str(exc)}, True)
                raise
            payload = {"directory": str(args.directory), "format": args.format,
                       "events": [asdict(event) for event in events]}
        else:
            battery = await client.battery()
            recordings = await client.list_recordings()
            payload = {
                "battery_percent": battery,
                "recordings": [{
                    "recording_id": row.recording_id,
                    "recorded_at": row.recorded_at.isoformat(),
                    "size_bytes": row.size_bytes,
                } for row in recordings],
            }
    _emit(command, payload, args.json)


def _global_option_parser(suppress_defaults=False):
    """Parent parser for options accepted both before and after a command.

    Subcommand copies suppress their defaults so an option given before the
    subcommand survives the subparser's own namespace update.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--address", default=argparse.SUPPRESS if suppress_defaults else None,
                        help="explicit Bluetooth address (device UUID on macOS); "
                             "defaults to $AIREC_ADDRESS, then a unique scan match")
    parent.add_argument("--timeout", type=float,
                        default=argparse.SUPPRESS if suppress_defaults else 10.0,
                        help="seconds to wait for scanning and device replies (default: 10)")
    parent.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS if suppress_defaults else False,
                        help="print machine-readable JSON instead of human-readable text")
    return parent


def main():
    parser = argparse.ArgumentParser(description=__doc__, parents=[_global_option_parser()])
    command_options = _global_option_parser(suppress_defaults=True)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("scan", parents=[command_options],
                        help="scan for AIREC recorders and print name, address and RSSI")
    commands.add_parser("list", parents=[command_options], help="list recording metadata (the default)")
    commands.add_parser("storage", parents=[command_options],
                        help="read total, free and used storage in device-reported MB")
    commands.add_parser("info", parents=[command_options],
                        help="read firmware, chip, charging and device settings from the recorder")
    for name, help_text in (("status", "read recording state"), ("start", "start ordinary recording"),
                            ("pause", "pause active recording"), ("resume", "resume paused recording"),
                            ("stop", "stop/finalize current recording"), ("clock", "read device clock")):
        commands.add_parser(name, parents=[command_options], help=help_text)
    delete = commands.add_parser("delete", parents=[command_options],
                                 help="permanently delete one archive; recorder must be stopped")
    delete.add_argument("recording_id")
    delete.add_argument("--yes", action="store_true", help="confirm permanent deletion")
    clock = commands.add_parser("set-clock", parents=[command_options],
                                help="set device clock; recorder must be stopped")
    clock.add_argument("--time", help="local ISO datetime without timezone (default: computer local time)")
    setting = commands.add_parser("set-setting", parents=[command_options],
                                  help="set one device setting; recorder must be stopped")
    setting.add_argument("name", help=f"one of: {', '.join(SETTING_NAMES)}")
    setting.add_argument("value", help="'on'/'off' for switches, otherwise an integer")
    download = commands.add_parser("download", parents=[command_options],
                                   help="download one recording; never deletes it")
    download.add_argument("recording_id", help="14-digit ID from the catalog")
    download.add_argument("--output", type=Path, help="destination (default: recordings/ID.opus)")
    download.add_argument("--format", choices=("opus", "raw"), default="opus",
                          help="output audio format: opus (default) or raw device bytes")
    download.add_argument("--download-timeout", type=float, default=_DEFAULT_DOWNLOAD_TIMEOUT,
                          help="seconds allowed for the transfer; default covers a full "
                               "60-minute segment")
    download.add_argument("--stop-recording", action="store_true",
                          help="allow stopping/finalizing an active or paused recording; does not restart it")
    sync = commands.add_parser("sync", parents=[command_options],
                               help="download every missing recording into a directory, resuming partial files")
    sync.add_argument("directory", type=Path, help="existing directory to write recordings into")
    sync.add_argument("--format", choices=("opus", "raw"), default="opus",
                      help="output audio format: opus (default) or raw device bytes")
    sync.add_argument("--stop-recording", action="store_true",
                      help="allow stopping/finalizing an active or paused recording first; does not restart it")
    sync.add_argument("--download-timeout", type=float, default=600.0,
                      help="seconds allowed per recording; default covers a full 60-minute segment")
    sync.add_argument("--delete-after", action="store_true",
                      help="delete each recording from the recorder once its local "
                           "output file exists (format must match)")
    sync.add_argument("--yes", action="store_true",
                      help="confirm permanent deletion (required with --delete-after)")
    listen = commands.add_parser("listen", parents=[command_options],
                                 help="stream an active recording's live audio as Ogg Opus")
    listen.add_argument("-o", "--output", required=True, metavar="FILE|-",
                        help="write Ogg Opus to FILE (refused if the file exists) or '-' for "
                             "stdout; the summary is text on stderr, so --json is not supported")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (Exception, KeyboardInterrupt) as exc:
        print(f"AIREC operation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
