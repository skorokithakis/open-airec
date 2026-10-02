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
- `save_audio()` uses a temporary file and hard link for atomic no-overwrite
  publication. Filesystems without hard-link support fail rather than silently
  falling back to an unsafe overwrite. The parent directory must exist.
- Storage values are coarse firmware-reported MB. Exact physical byte capacity,
  filesystem overhead and rounding have not been independently verified.
- Clock and recording timestamps carry no timezone; no UTC conversion, DST or
  historical timezone recovery is performed. IDs are second-resolution timestamps;
  same-second collisions are not resolved automatically. Deletion rejects duplicate
  catalog IDs rather than guessing which recording to remove.

The app's `0x04` command stops and finalizes the current recording. It is not
a query. Do not use it to read state.
