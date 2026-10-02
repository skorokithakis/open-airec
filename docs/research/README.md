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
- [Wi-Fi transfer](wifi.md): unvalidated Wi-Fi commands, session flow and
  resume behaviour (no hardware confirmation).
- [Live audio](live-audio.md): how the app's realtime listen path works over
  Wi-Fi/WebSocket, its `0x6d`/`0x6e` commands, the direct-BLE `0011201a`
  framing, and the proposed L2 probe (static research only).
- [Record type](record-type.md): static analysis of the `0x79` recording-format
  write (WAV/MP3 picker), its chip-type gating, persistence, `0x20` read-back,
  and a proposed live probe (static research only).

The raw research material (the APK, decompiled code, packet captures and
session logs) is not distributed with this repository; a 2.1.3 snapshot and its
locally rebuilt Blutter output live under ignored `artifacts/re/`. The findings
above replace the raw material for normal use, but new commands need a compatible
snapshot again. See
[retained versus removed material](app-analysis.md#retained-versus-removed-material).
