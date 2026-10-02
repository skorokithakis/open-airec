# Python library

All exports below come from `airec`. Device operations are asynchronous.
Install with `pip install -e .` from the repository. For the command-line tool,
see the [README](../README.md). For protocol details, see [research](research/).

## Quick start

```python
import asyncio
from pathlib import Path

from airec import AirecClient, find_recorders, save_audio

async def main():
    recorders = await find_recorders()
    if len(recorders) != 1:
        raise SystemExit(f"expected one recorder, found {len(recorders)}")
    async with AirecClient(recorders[0].device) as recorder:
        print(await recorder.battery())
        print(await recorder.storage())
        recordings = await recorder.list_recordings()
        for row in recordings:
            print(row.recording_id, row.recorded_at, row.size_bytes)

        # Download only while stopped; no implicit recording control.
        if recordings and (await recorder.recording_status()).state == "stopped":
            raw = await recorder.download_recording(recordings[0])
            directory = Path("recordings")
            directory.mkdir(exist_ok=True)
            save_audio(raw, directory / f"{recordings[0].recording_id}.opus")

asyncio.run(main())
```

## Finding recorders

`await find_recorders(timeout=10.0) -> list[Recorder]` scans for `timeout`
seconds. It returns devices whose advertised name starts with `AIREC`, ignoring
case. It falls back to the OS-resolved name when the advertisement has no name.
It does not filter on service UUID, because advertising may omit the primary
service. It only listens to advertisements and never connects.

`Recorder(name, address, rssi, device)` is an immutable dataclass. Pass
`device` (a Bleak `BLEDevice`) to `AirecClient` to connect without a second
address lookup. The name prefix was checked against one recorder only.

## Connection and errors

`AirecClient(device, *, timeout=10.0, trace=None, client_factory=BleakClient)`

- `device`: explicit Bleak address/UUID or a discovered `BLEDevice` (preferred).
- `timeout`: finite positive seconds for queries and control operations. Multi-step
  controls/deletion share that deadline; discovery in the CLI also uses it.
- `trace(direction, data)`: synchronous optional callback for control bytes.
  Directions are `send`, `receive`, and `send_chunk` when requests are split.
  Raw audio is not traced, but identifiers/catalog metadata are. A callback
  should be fast and must not raise exceptions.
- `client_factory`: injected BLE transport factory, primarily for offline tests.

Use `async with AirecClient(device) as recorder:` to connect, initialize and
disconnect. Alternatively call `await connect()` / `await disconnect()` explicitly.
`mac_response` contains the raw identity reply while ready; its full format is
not guaranteed. There are no implicit connections or mutation retries.

`ProtocolError` means a malformed, inconsistent or rejected device response.
`ActiveRecordingError` extends it for forbidden active/paused recording states.
`DownloadInterrupted` extends it and adds `.partial` for a started transfer that
failed before the verified end marker.
`ConnectionError` signals disconnected/not initialized; `TimeoutError` signals a
missing response/deadline. Invalid caller input raises `ValueError`; BLE/OS
exceptions can also propagate. After operation failure, reconnect before reuse.
Timeout never proves that a mutation did not execute. Context-manager cleanup
preserves an original error if disconnection also fails.

## Query methods and models

| Method | Result |
| --- | --- |
| `await battery()` | Integer percentage, 0–100 |
| `await storage()` | `StorageInfo(total_mb, free_mb)`, with derived `used_mb` |
| `await recording_status()` | `RecordingStatus(state, recording_id=None)` |
| `await list_recordings()` | Tuple of `Recording` rows through the explicit end marker |
| `await clock()` | Naive `datetime` containing device-local wall time |
| `await firmware_version()` | Firmware version string (whole payload decoded as UTF-8) |
| `await firmware_type()` | Firmware/board type string (whole payload decoded as UTF-8) |
| `await chip_info()` | `ChipInfo(work_mode, audio_format)` |
| `await is_charging()` | `True` when the one-byte 0x36 reply is `1`, else `False` |
| `await device_settings()` | Length-dependent `DeviceSettings` snapshot, raw bytes included |

These five queries are read-only, send an empty request payload and never mutate
device state. A malformed, empty or wrong-length reply raises `ProtocolError`.

Models are immutable dataclasses:

- `Recording(recording_id, metadata)`: 14 ASCII digits in `YYYYMMDDHHMMSS` format,
  and four raw size bytes. `size_bytes` decodes big-endian size; `recorded_at`
  returns a naive local `datetime`. `from_frame(frame)` validates a catalog row.
- `RecordingStatus`: `state` is `recording`, `paused`, or `stopped`.
  `recording_id` is available for active status, not the paused/stopped wire reply.
- `StorageInfo`: coarse firmware MB; `used_mb = total_mb - free_mb`.
- `ChipInfo(work_mode, audio_format)`: the integer codes are authoritative;
  `work_mode_name` and `audio_format_name` return an app-derived display name, or
  `None` when the code is unknown. The app's derived `chipType` mapping is not
  exposed.
- `DeviceSettings`: a read-only snapshot of the variable-length 0x26 reply. It
  carries `noise_reduction`, `led`, `segment_duration`, `idle_shutdown`,
  `usb_support`, `mic_gain`, `power_on_record`, `disk_format_supported`,
  `default_wifi_on` and `default_monitor_on`, plus the exact `raw` payload bytes.
  Boolean fields treat a nonzero byte as `True`; `None` means the reported length
  omits that field. `segment_duration` and `idle_shutdown` are reported as raw
  integers because their units are unconfirmed. Fields are parsed by payload
  length (`9`, `10`, `12`, `13` and `>= 15`); other lengths raise `ProtocolError`.

## Recording controls

| Method | Behavior and return |
| --- | --- |
| `await start_recording()` | Start ordinary recording from stopped state; active is a no-op, paused raises `ActiveRecordingError`. Returns verified active `RecordingStatus`. |
| `await pause_recording()` | Check state, toggle only if active, verify paused. Paused is a no-op; stopped raises `ProtocolError`. Returns status. |
| `await resume_recording()` | Check state, toggle only if paused, verify active. Active is a no-op; stopped raises `ProtocolError`. Returns status. |
| `await stop_recording()` | Finalize active/paused recording, verify stopped. Returns `Recording` metadata, or `None` if already stopped. |

Use one connection for a control cycle. Starting does not schedule an automatic
stop. A stop reply is not proof that a very short file survives firmware cleanup.

```python
async with AirecClient(device) as recorder:
    await recorder.start_recording()
    # ... record for the intended interval ...
    await recorder.pause_recording()
    await recorder.resume_recording()
    finalized = await recorder.stop_recording()
```

## Archive downloading and output

`await download_recording(recording, *, offset=0, timeout=120.0,
max_size=128 * 1024 * 1024, stop_if_recording=False) -> bytes`

- `recording`: a `Recording` with valid four-byte metadata, or an ID string
  resolved uniquely against a fresh catalog. Caller-supplied metadata may be
  stale; acknowledgement size must match it or the operation fails.
- `offset`: in-range starting byte position (`0 <= offset < catalog size`; an
  empty recording may only start at `0`). The request carries the four-byte
  big-endian offset and the device streams `size - offset` bytes. Resume is
  caller-managed. On failure, append `partial` to the bytes you already hold
  and retry with `offset + len(partial)`. That is the total length of the
  bytes you hold, not the length of `partial` alone.
- `timeout`: finite positive overall transfer/preflight deadline. Resolving a
  string uses a separate catalog query timeout; preflight queries also use the
  client's query timeout within the transfer deadline.
- `max_size`: nonnegative integer cap on expected raw bytes; does not cap all
  transient allocations or the larger Ogg output. CLI uses the default cap.
- `stop_if_recording`: explicit permission to finalize active/paused recording.
  Defaults to refusal. Does not restart recording afterward.

Only complete, size-checked transfers with acknowledgement and completion marker
are returned. The `0x07` acknowledgement always reports the full catalog size,
regardless of offset; the required stream length is catalog size minus offset.
Failure/cancellation attempts `0x08` cancellation and disconnects. A failure
after the transfer started raises `DownloadInterrupted` (a `ProtocolError`)
whose `.partial` holds the contiguous bytes received from `offset`. It is
non-empty only when a matching `0x07` acknowledgement (correct ID and full
catalog size) was the transfer's valid acknowledgement; a missing, mismatched or
rejected (`0xfd`) transfer yields empty `.partial`, because pre-ack bytes cannot
be attributed to this archive. It is never returned as success and is not
size/end-verified. No streaming, progress or automatic retry/reconnect API
exists.

`to_ogg_opus(raw: bytes) -> bytes` losslessly wraps the supported archive profile
in Ogg Opus; it does not decode, transcribe or re-encode. A trailing remainder
shorter than 80 bytes is treated as a firmware-truncated final slot and dropped,
provided its leading TOC byte passes the same mono/duration checks as a full
packet. Empty data, inputs shorter than one full packet, unsupported TOC values
and an invalid trailing TOC raise `ValueError`. Full packets are preserved
bit-for-bit. Up to 79 trailing raw bytes can be left out of the wrapped audio.
`format="raw"` keeps every byte.

`save_audio(raw, destination, *, format="opus") -> pathlib.Path` converts to Ogg
Opus or preserves bytes with `format="raw"`. It writes/fsyncs a sibling temporary
file, hard-links atomically into place, and removes the temporary file. Parent
directory must exist. Existing files/symlinks raise `FileExistsError`; disk and
filesystem errors propagate. Publication does not claim directory-fsync durability.

## Bulk sync

`await sync_directory(client, directory, *, format="opus", stop_recording=False,
delete_after=False, download_timeout=600.0, progress=None) -> tuple[SyncEvent, ...]`

Downloads every catalog recording missing from `directory` using one connected
`AirecClient`. `directory` must already exist; `format` is `"opus"` (`.opus`
output) or `"raw"` (`.airec`). Output files that already exist are skipped, not
compared or overwritten. Each recording is downloaded, checked to equal its
catalog size, then published with `save_audio`. `download_timeout` is the
per-file transfer deadline passed to `download_recording`; the 600 s default
covers a full 60-minute segment at measured BLE throughput (~110-130 KB/s).

When the recorder is recording or paused, the call raises `ActiveRecordingError`
unless `stop_recording=True`, in which case the active recording is finalized
once through the normal stop path before the catalog is fetched, so the finalized
entry is included. No recording is restarted.

Interrupted transfers are resumable. On a `DownloadInterrupted`, the attributable
bytes are appended to `directory/<ID>.part` (append, then fsync; an empty
`.partial` creates no file). On the next run a `.part` shorter than the catalog
size resumes from its length and is concatenated with the new bytes; a `.part` at
least as large as the catalog entry is discarded and restarted from zero. A
`.part` that is not an ordinary regular file (for example a symlink) is refused.
The `.part` file is removed only after the complete recording is published.

`delete_after=True` calls `delete_recording()` for each recording this run
published (resumed files included) only after its local file exists. Skipped
files are never deleted. `progress(event)` is an optional synchronous callback
that receives one `SyncEvent` per recording. `SyncEvent` fields are `action`
(`skipped`, `downloaded`, `resumed`, `deleted` or `failed`), `recording_id`,
`bytes` (raw bytes transferred this run), `resumed_from` (nonzero resume offset
or `None`) and `error` (failure summary).

The first failure stops the run and raises `SyncFailed` (a `RuntimeError`) whose
`.events` holds every outcome reached, including the terminal `failed` event, with
the original exception chained as `__cause__`. There is no retry, reconnect,
concurrency or byte-level progress; rerunning continues from what remains.

## Deletion and clock setting

`await delete_recording(recording_id: str) -> None` is an explicit irreversible
operation. It requires stopped status, a unique ID in a fresh catalog, empty
success acknowledgement and verified catalog absence afterward. It never deletes
local output or exposes an erase-all operation. No extra confirmation flag in
Python: calling this method is the confirmation. CLI requires `--yes`.

`await set_clock(value: datetime | None = None) -> datetime` requires stopped
status. `None` uses computer-local time at execution. Explicit input must be a
naive datetime; microseconds are dropped. Convert aware datetimes yourself to the
intended wall timezone and then remove timezone information. It sends the timestamp,
requires an empty acknowledgement, reads back and permits nonnegative drift up
to `timeout + 2` seconds. Existing archive IDs are not renamed.

## Framing utilities

`Frame(command, payload)` is an immutable response model.
`encode_request(command, payload=b"")` returns ordinary request framing, with
a one-byte command-plus-payload length; command must fit a byte and payload is
at most 254 bytes. This encoder is not permission to send arbitrary commands:
the client's write path separately restricts its allowlist and payload shapes.

`FrameDecoder.feed(data)` incrementally returns tuples of complete control frames;
`reset()` clears buffered fragments. It resynchronizes after garbage and bounds
pending data by the wire length. Never feed raw live/archive audio into it.
