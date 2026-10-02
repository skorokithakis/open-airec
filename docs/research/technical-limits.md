# Technical limits

These limits come from the protocol research and hardware validation. For
user-facing limits, see [limitations](../limitations.md).

- Only one recorder, Linux/BlueZ and Bleak 0.22.3 were hardware-validated.
  macOS, Windows, newer Bleak versions and other firmware remain unvalidated.
- Dependency range is deliberately `bleak>=0.22,<0.23`: archive downloads on
  Linux isolate a private `_acquire_mtu()` workaround. BlueZ can report a stale
  20-byte write limit even when negotiated MTU is 247; requests may need more.
- No authenticated account/handshake/pairing workflow is implemented. Their
  absence in the successful sessions does not prove other devices need none.
- Operations are serialized per client. Separate clients/phone connections
  must not compete for the device. There is no automatic reconnect or retry.
- Catalog completeness relies on the end marker, without an independent count,
  pagination, cursor, deduplication or protection against device-side omissions.
- Downloads require ID/size acknowledgement, catalog byte count and end marker,
  but no independently validated file checksum is available. Data is buffered in
  memory (128 MiB raw-size cap by default), with a bounded notification queue.
  Overflow fails instead of returning corrupt/partial audio.
- Audio packaging supports fixed 80-byte mono Opus packets only. It preserves
  packets without re-encoding. Encoder delay is unknown (pre-skip zero), and
  packet checks are not a complete Opus bitstream validator. Other profiles can
  be saved raw, but are not guaranteed playable. No WAV/MP3 decoding API exists.
  The firmware may finalize an archive with a truncated final 80-byte slot
  (observed on 6 of 11 archives from one recorder, with remainders of 32, 48
  and 64 bytes). The Ogg wrapper drops such a tail after it checks the tail's
  leading TOC byte. Up to 79 trailing raw bytes can be left out of the wrapped
  audio. `format="raw"` preserves them. The missing bytes cannot be
  reconstructed.
- `save_audio()` uses a temporary file and hard link for atomic no-overwrite
  publication. Filesystems without hard-link support fail rather than silently
  falling back to an unsafe overwrite. The parent directory must exist.
- Storage values are coarse firmware-reported MB. Exact physical byte capacity,
  filesystem overhead and rounding have not been independently verified.
- Clock and recording timestamps carry no timezone; no UTC conversion, DST or
  historical timezone recovery is performed. IDs are second-resolution timestamps;
  same-second collisions are not resolved automatically. Deletion rejects duplicate
  catalog IDs rather than guessing which recording to remove.
- Device-info queries are read-only and empty-payload. Some `0x26` field units are
  unconfirmed: segment duration is labelled minutes and idle shutdown has no proven
  unit. `disk_format` is a capability flag, not an action, and default
  Wi-Fi/monitor flags are only present on longer payloads. Unknown work modes and
  audio formats must be tolerated rather than assumed.
- Download offsets are honored byte-exactly, including unaligned offsets. The
  `0x07` acknowledgement always carries the full catalog size, so the expected
  stream length is catalog size minus offset, not the remaining size in the ack.
- The firmware may answer `0x08` with `0xfd`; either is a stop response.
- The app accepts `0x09` once the local file reaches 50% of the expected size,
  ignores the `0x07` ack payload and adds stall/timeout heuristics. This client
  deliberately keeps exact byte-count and ID/size validation; do not copy the app's
  leniency.
- The app itself auto-deletes recordings shorter than 5 s. Short recordings can
  therefore disappear through app cleanup, firmware discard or both; the cause of
  the observed unsolicited `0x0a` is unconfirmed.
- Never query `0x64` (it returns stored Wi-Fi credentials) and never send the OTA,
  format or bulk-delete opcodes (`0x25`, `0x27`, `0x28`, `0x3c`, `0x34`), Wi-Fi
  writes or setting writes. See [protocol](protocol.md#excluded-commands).
- These findings rely on one recorder and firmware. New commands require acquiring
  a compatible APK snapshot again; the decompiled evidence is not distributed.

The app's `0x04` command stops and finalizes the current recording. It is not
a query. Do not use it to read state.
