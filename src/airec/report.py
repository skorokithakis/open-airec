"""Render command results as JSON or short human-readable text.

Kept separate from the device-operation flow so __main__ only builds plain
payloads; nothing here touches BLE or the filesystem.
"""

import json

# Commands whose existing JSON output is compact (no indentation). All other
# commands keep the indented shape the CLI already printed.
_COMPACT_JSON_COMMANDS = frozenset({"stop", "delete", "clock", "set-clock"})

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(size_bytes: int) -> str:
    """Format a byte count using decimal units (1 MB = 1,000,000 bytes)."""
    value = float(size_bytes)
    unit = "B"
    for candidate in _SIZE_UNITS:
        unit = candidate
        if value < 1000 or candidate == _SIZE_UNITS[-1]:
            break
        value /= 1000
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.1f} {unit}"


def render_json(command: str, payload: dict) -> str:
    """Serialize a payload, preserving each command's existing JSON shape."""
    indent = None if command in _COMPACT_JSON_COMMANDS else 2
    return json.dumps(payload, indent=indent)


def _scan_text(payload: dict) -> str:
    recorders = payload["recorders"]
    if not recorders:
        return "No AIREC recorders found."
    return "\n".join(f"{r['name']}  {r['address']}  {r['rssi']} dBm" for r in recorders)


def _list_text(payload: dict) -> str:
    lines = [f"Battery: {payload['battery_percent']}%"]
    recordings = payload["recordings"]
    if not recordings:
        lines.append("No recordings.")
        return "\n".join(lines)
    header = ("ID", "Recorded at", "Size")
    rows = [(r["recording_id"], r["recorded_at"].replace("T", " "),
             human_size(r["size_bytes"])) for r in recordings]
    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(3)]
    lines.append("  ".join(header[i].ljust(widths[i]) for i in range(3)).rstrip())
    lines.extend(f"{row[0].ljust(widths[0])}  {row[1].ljust(widths[1])}  {row[2].rjust(widths[2])}"
                 for row in rows)
    return "\n".join(lines)


def _storage_text(payload: dict) -> str:
    return "\n".join((f"Total: {payload['total_mb']} MB",
                      f"Free: {payload['free_mb']} MB",
                      f"Used: {payload['used_mb']} MB"))


def _status_text(payload: dict) -> str:
    return "\n".join((f"State: {payload['state']}",
                      f"Recording ID: {payload['recording_id'] or 'none'}"))


def _stop_text(payload: dict) -> str:
    return f"Finalized recording ID: {payload['finalized_recording_id'] or 'none'}"


def _delete_text(payload: dict) -> str:
    return f"Deleted recording ID: {payload['deleted_recording_id']}"


def _clock_text(payload: dict) -> str:
    return f"Device local time: {payload['device_local_time']}"


def _download_text(payload: dict) -> str:
    raw, file_size = payload["raw_size_bytes"], payload["file_size_bytes"]
    return "\n".join((f"Recording ID: {payload['recording_id']}",
                      f"Path: {payload['path']}",
                      f"Raw size: {human_size(raw)} ({raw} bytes)",
                      f"File size: {human_size(file_size)} ({file_size} bytes)"))


_RENDERERS = {
    "scan": _scan_text,
    "list": _list_text,
    "storage": _storage_text,
    "status": _status_text,
    "start": _status_text,
    "pause": _status_text,
    "resume": _status_text,
    "stop": _stop_text,
    "delete": _delete_text,
    "clock": _clock_text,
    "set-clock": _clock_text,
    "download": _download_text,
}


def render_text(command: str, payload: dict) -> str:
    """Render a payload as human-readable text for the given command."""
    return _RENDERERS[command](payload)
