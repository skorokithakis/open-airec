# AIREC client

An experimental, asynchronous Python library for interfacing directly with an
AIREC voice recorder over Bluetooth Low Energy. No account, cloud service or
transcription service is required. The CLI is included as a convenience.

**Hardware-validated on one recorder under Linux/BlueZ with Bleak 0.22.3 on
2026-10-02.** Other models, firmware and operating systems are not yet validated.

## Feature support

| Feature | Status |
| --- | --- |
| Connect and initialize | Implemented and hardware-validated |
| Battery percentage | Implemented and hardware-validated |
| Catalog: IDs, local timestamps, file sizes | Implemented and hardware-validated |
| Archive downloads: raw bytes or playable Ogg Opus | Implemented and hardware-validated |
| Recording status, start, pause, resume, stop/finalize | Implemented and hardware-validated |
| Delete one recording, verify catalog absence | Implemented and hardware-validated |
| Read/set device-local clock | Implemented and hardware-validated |
| Total/free/used storage | Implemented and hardware-validated |
| Wi-Fi transfer, download resume, live audio | Not implemented |
| Firmware/chip information and other device settings | Not implemented |
| Firmware update, reset, format, bulk erase | Deliberately excluded |

See [unsupported features and limitations](docs/limitations.md) for the full list.

## Installation

Requires Python 3.12 or newer and a working Bluetooth adapter/OS Bluetooth stack.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Activate the environment or use `.venv/bin/airec-client`. The equivalent module
invocation is `.venv/bin/python -m airec_client`.

Turn on the recorder, keep it nearby, and disconnect the official phone app
(turn phone Bluetooth off if necessary). Use your recorder's explicit Bluetooth
address; on macOS Bleak uses a device UUID. Discovery by model/name is not an
exposed library feature, and private addresses may rotate.

## CLI

Replace `ADDRESS` and `RECORDING_ID` with your own values. Listing is the default.
All successful commands output JSON; failures exit nonzero and report to stderr.

```bash
airec-client ADDRESS                       # battery and recording catalog
airec-client ADDRESS list
airec-client ADDRESS storage               # total_mb, free_mb, used_mb
airec-client ADDRESS status                # recording, paused, or stopped
airec-client ADDRESS clock                 # device-local wall time

airec-client ADDRESS download RECORDING_ID
airec-client ADDRESS download RECORDING_ID --output audio.opus
airec-client ADDRESS download RECORDING_ID --format raw
airec-client ADDRESS download RECORDING_ID --download-timeout 300

airec-client ADDRESS start
airec-client ADDRESS pause
airec-client ADDRESS resume
airec-client ADDRESS stop
airec-client ADDRESS delete RECORDING_ID --yes
airec-client ADDRESS set-clock             # computer's local time
airec-client ADDRESS set-clock --time 2026-10-02T12:30:00
```

Global query/discovery timeout goes **before** the subcommand:
`airec-client ADDRESS --timeout 20 storage`. Default: 10 seconds.

Downloads default to `recordings/RECORDING_ID.opus`, or `.airec` with `--format
raw`. Existing files are never overwritten. The default download timeout is
120 seconds. If actively recording or paused, downloading is refused unless
`--stop-recording` explicitly allows **stopping/finalizing the current recording**.
It is not restarted afterward. Downloads never delete the device recording.

## Python example

```python
import asyncio
from pathlib import Path

from airec_client import AirecClient, save_audio

async def main():
    async with AirecClient("YOUR_DEVICE_ADDRESS") as recorder:
        print(await recorder.battery())
        print(await recorder.storage())
        print(await recorder.recording_status())
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

See the [complete API reference](docs/api.md), including recording controls,
clock setting, exceptions, timeouts and tracing.

## Safety and correctness

- Catalog success requires the `0x06` end marker. A timeout is never reported
  as an empty catalog or partial success. No independent total count is known.
- Download success requires matching recording ID and size acknowledgement,
  exactly the catalog byte count, and the `0x09` completion marker.
- Pause/resume checks current state before toggling and verifies the result.
- Deletion is permanent: it requires stopped state, a unique fresh catalog
  match, an acknowledgement, and verified absence afterward. It never removes
  local downloads. Clock setting also requires stopped state.
- A timeout **does not prove a state-changing command failed**. Reconnect and
  inspect state before deciding what to do next; mutations are never retried
  automatically. Downloads attempt cancellation and disconnect on failure.
- Power-on, connection and disconnection may affect recording behavior.
  A separate CLI invocation closes its connection; use a single Python
  connection for pause/resume cycles. Very short recordings may be discarded
  by firmware even after a successful stop acknowledgement.
- Raw control traces can contain device identifiers and recording metadata.
  Keep traces and recordings private. Nothing uploads them automatically.

## Validation and development

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m airec_client --help
```

The 76 offline tests cover framing, fake BLE transport, control transitions,
timeouts, disconnects, deletion verification, storage validation, archive
transfers and safe audio output. They do not contact hardware. No APK, phone
app, upstream package or FFmpeg installation is required to use the library or
run the tests. FFmpeg was used separately to validate two downloaded Opus files.

Read the [protocol reference](docs/protocol.md) and
[research/validation summary](docs/app-analysis.md) for evidence and provenance.
Local recordings and research logs are excluded from Git. Obsolete APK files,
decompilation/build outputs, vendor package copies and one-off probes were removed
after the findings were documented.

## License

[GNU Affero General Public License v3](LICENSE).
