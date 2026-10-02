# Research

These documents record how the AIREC Bluetooth protocol was found and tested.
They are for developers who want to change protocol behavior or add commands.
Read them before you change anything in `src/airec/client.py`.

- [Protocol](protocol.md): UUIDs, framing, commands and the sequences the
  client uses.
- [App analysis and hardware validation](app-analysis.md): where each finding
  came from and what was tested on real hardware.
- [Technical limits](technical-limits.md): known limits of the protocol,
  downloads, audio packaging, storage values and timestamps.

The raw research material (the APK, decompiled code, packet captures and
session logs) is not distributed with this repository; a 2.1.3 snapshot and its
locally rebuilt Blutter output live under ignored `artifacts/re/`. The findings
above replace the raw material for normal use, but new commands need a compatible
snapshot again. See
[retained versus removed material](app-analysis.md#retained-versus-removed-material).
