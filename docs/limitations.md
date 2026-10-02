# Unsupported features and limitations

## Hardware features not implemented

| Feature | Evidence and remaining work |
| --- | --- |
| Wi-Fi fast transfer | App contains hotspot control and TCP/UDP transfer paths. This recorder's Wi-Fi support has not been confirmed. Needs capability detection, setup, network protocol recovery and controlled validation. |
| Resumable archive downloads | Download request contains a byte offset; the client permits only zero. Nonzero offsets, size interpretation, partial-file integrity and reconnect behavior need validation. |
| Live audio stream | A live notify characteristic exists, but there is no public subscription/stream API or validated decoding path for it. |
| Firmware version, chip type and device capability queries | App contains these operations; no public methods or validated parsing yet. |
| Other device settings | Recording modes, segmentation, gains and other app settings are not exposed. Only ordinary recording mode is supported. |
| Automatic device discovery/selection | CLI locates an explicitly supplied address. No public name/model scanner, device picker or address-rotation tracking. |
| Recording duration metadata | Catalog contains time and byte size, not a decoded duration. Opus timing is computed only when wrapping the supported archive profile. |
| Bulk download/delete workflows | Single-recording APIs can be called in sequence, but no built-in batch orchestration or progress reporting. No erase-all command. |

Transcription, cloud synchronization, account login and uploads are outside this
hardware-interface library's scope. It does not offer on-device transcription.

## Deliberately excluded dangerous commands

Firmware/OTA updates, bootloader operations, device reset, disk formatting and
bulk erase are not implemented and are outside the command allowlist. The OTA
characteristic is never written. This is a safety boundary, not a claim that
the hardware cannot perform these operations.

## Compatibility and protocol limits

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

## Side effects and failure behavior

Power-on may start recording. Disconnecting while paused can affect finalization.
Very short recordings may be automatically discarded, with no confirmed threshold.
Stop acknowledgement metadata is therefore not proof that an archive was retained.

Deletion and clock setting require stopped state. Downloads refuse active/paused
state unless explicitly permitted to finalize it; no automatic restart afterward.
Start on active and pause/resume in the desired state are no-ops, but firmware or
physical controls can still race with status checks.

Timeout/disconnection can happen **after** a command executes. Never blindly
retry a mutation. Reconnect, read current state/catalog/clock, and decide explicitly.
Most failed operations invalidate readiness without forcibly closing the BLE
link; reconnect before another operation. Failed/cancelled downloads attempt to
cancel transfer and disconnect. Cleanup is best-effort if the radio disappears.

## Troubleshooting

1. Confirm the recorder is powered on, nearby and not connected to the phone app.
2. Confirm the selected address is still current and the OS adapter is powered on.
3. If initialization fails or disconnects, restart the recorder and reconnect;
   check status afterward because restart may begin recording.
4. If download/delete/clock setting reports active/paused state, explicitly stop
   only if you intend to finalize that recording. Do not treat `0x04` as a query.
5. For unsupported audio, save `--format raw` and investigate locally rather than
   changing recording/firmware settings speculatively.
6. A `trace` callback can log control traffic for debugging. It includes private
   identifiers/metadata; redact it before sharing and never publish raw recordings.
