# AIREC client development

This repository is a hardware-interface library, not a transcription/cloud app.
README.md is the end-user guide; docs/library.md is the Python API; docs/research/
holds protocol, app-analysis and technical-limits findings. Read docs/research/
before changing protocol behavior. Never delete research findings; move or
rewrite them instead. The current implementation is src/airec/; tests/ uses
fake BLE transport and never accesses real hardware.

## Commands

- Install: `.venv/bin/python -m pip install -e .`
- Tests: `.venv/bin/python -m unittest discover -s tests -v` (103 tests at handoff)
- Compile: `.venv/bin/python -m compileall -q src tests`
- CLI: `.venv/bin/python -m airec --help` (text output by default, `--json` for
  scripts; recorder from --address, AIREC_ADDRESS, or a unique `AIREC*` name scan)

Python >=3.12; Bleak >=0.22,<0.23. Only Linux/BlueZ, Bleak 0.22.3 and one recorder
were hardware-validated. Other platforms/firmware remain unvalidated. The private
BlueZ MTU workaround is isolated in _prepare_download_transport; upgrading Bleak
requires reviewing that compatibility dependency.

## Protocol and safety invariants

- Initialize notifications, wait 200 ms, require 0x01 identity response.
- 0x02 is clock sync, NOT download selection. 0x04 stops/finalizes recording,
  NOT a current-file query. Do not reuse the obsolete upstream initialization.
- Start is 0x03 plus 0x21 R after 100 ms; no 0x21 ack on tested firmware.
- Pause/resume requires status checks around a single 0x10 toggle.
- Catalog needs explicit 0x06 end; download needs matching ID and full-size ack
  (any in-range offset), exact size-minus-offset byte count and 0x09 end; no
  timeout-as-empty/partial-success behavior.
- Delete one ID only, require stopped state and unique fresh catalog match,
  verify absence. Clock setting also requires stopped state. No implicit stop
  except explicitly opted-in download finalization. No automatic mutation retry.
- Never expose/probe OTA, bootloader, reset, disk format or erase-all. Only the
  primary control characteristic is written. Wi-Fi remains unconfirmed hardware.
- Power-on may start recording; short recordings may be auto-discarded by firmware.
  Preserve existing recordings during tests. Get live-test permission, disconnect
  the phone app, and use a controlled newly created archive for deletion validation.
- Keep audio and device-identifying logs local. recordings/ and artifacts/ are
  ignored. Never stage them or embed personal device addresses in public examples.

## Repository cleanup

Obsolete APK/upstream package copies, decompilation and build output and one-off
probes were removed after consolidating findings in docs/research/. Local session logs and
downloaded recordings were preserved but excluded from commits. Full original
app evidence is not bundled; acquire a compatible snapshot before investigating
new commands. Do not broadly index extracted APKs or generated assembly trees.
