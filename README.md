# airec

<!-- PyPI renders this README too. Keep image and link URLs absolute; relative paths break there. -->

<p align="center">
  <img src="https://raw.githubusercontent.com/skorokithakis/open-airec/main/misc/recorder.png" alt="The AIREC voice recorder" width="300">
</p>

`airec` lets you control an AIREC voice recorder from your computer over
Bluetooth. It is an unofficial, open-source alternative to the manufacturer's
phone app. You can start and stop recording, and copy your recordings to your
computer as playable audio files. You do not need an account, the phone app or
an internet connection.

`airec` is a command-line tool and a Python library.

**Status: experimental.** This was tested on one recorder, on Linux. Other
recorder models, firmware versions, macOS and Windows were not tested.

## Install

You need Python 3.12 or newer and a computer with Bluetooth.

```bash
uv tool install open-airec
```

## Use the command line

Turn on the recorder and disconnect the phone app. The recorder accepts only
one connection at a time.

```bash
airec status            # recording, paused or stopped
airec start             # start recording
airec stop              # stop and save the recording
mkdir -p recordings
airec sync recordings   # copy new recordings into ./recordings
```

`sync` downloads only the recordings that are not already in the folder. If it
stops early, run it again to continue. Turning the recorder on can start a
recording by itself, so run `airec status` first.

`airec` also lists, downloads and deletes single recordings, streams live
audio, shows battery and storage, and changes device settings. See the
[command-line guide](https://github.com/skorokithakis/open-airec/blob/main/docs/cli.md).

## Use the Python library

```python
import asyncio
from pathlib import Path

from airec import AirecClient, find_recorders, sync_directory

async def main():
    recorders = await find_recorders()
    if len(recorders) != 1:
        raise SystemExit(f"expected one recorder, found {len(recorders)}")
    async with AirecClient(recorders[0].device) as recorder:
        await recorder.start_recording()
        await asyncio.sleep(60)
        await recorder.stop_recording()

        directory = Path("recordings")
        directory.mkdir(exist_ok=True)
        await sync_directory(recorder, directory)

asyncio.run(main())
```

See the [library guide](https://github.com/skorokithakis/open-airec/blob/main/docs/library.md) for the full API.

## More

- [Command-line guide](https://github.com/skorokithakis/open-airec/blob/main/docs/cli.md): every command, options and troubleshooting.
- [Library guide](https://github.com/skorokithakis/open-airec/blob/main/docs/library.md): the Python API.
- [Limitations](https://github.com/skorokithakis/open-airec/blob/main/docs/limitations.md): what does not work, and why.
- [Development](https://github.com/skorokithakis/open-airec/blob/main/docs/development.md): work on `airec` itself.

## Privacy

`airec` talks only to your recorder. It sends nothing to the internet.
Recordings stay on your computer.

## License

[GNU Affero General Public License v3](https://github.com/skorokithakis/open-airec/blob/main/LICENSE).
