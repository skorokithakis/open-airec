# Unsupported features and limitations

## Hardware features not implemented

| Feature | Evidence and remaining work |
| --- | --- |
| Wi-Fi fast transfer | App contains hotspot control and TCP/UDP transfer paths. This recorder's Wi-Fi support has not been confirmed. Needs capability detection, setup, network protocol recovery and controlled validation. |
| Resumable archive downloads | Download request contains a byte offset; the client permits only zero. Nonzero offsets, size interpretation, partial-file integrity and reconnect behavior need validation. |
| Live audio stream | A live notify characteristic exists, but there is no public subscription/stream API or validated decoding path for it. |
| Firmware version, chip type and device capability queries | App contains these operations; no public methods or validated parsing yet. |
| Other device settings | Recording modes, segmentation, gains and other app settings are not exposed. Only ordinary recording mode is supported. |
| Address tracking and device picker | `airec scan` and `find_recorders()` match advertised names that start with `AIREC`. The CLI selects a recorder automatically only when exactly one matches. There is no interactive picker and no tracking of addresses that change. The name prefix was checked against one user's recorder only. |
| Recording duration metadata | Catalog contains time and byte size, not a decoded duration. Opus timing is computed only when wrapping the supported archive profile. |
| Bulk download/delete workflows | Single-recording APIs can be called in sequence, but no built-in batch orchestration or progress reporting. No erase-all command. |

Transcription, cloud synchronization, account login and uploads are outside this
hardware-interface library's scope. It does not offer on-device transcription.

## Deliberately excluded dangerous commands

Firmware/OTA updates, bootloader operations, device reset, disk formatting and
bulk erase are not implemented and are outside the command allowlist. The OTA
characteristic is never written. This is a safety boundary, not a claim that
the hardware cannot perform these operations.

## Compatibility

Only one recorder was tested, on Linux with BlueZ and Bleak 0.22.3. macOS,
Windows, other firmware and newer Bleak versions were not tested. For the
technical limits of the protocol, downloads, audio packaging, storage values
and timestamps, see [technical limits](research/technical-limits.md).

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

For common problems, see [When something goes wrong](../README.md#when-something-goes-wrong).

1. Confirm the recorder is powered on, nearby and not connected to the phone app.
2. Confirm the selected address is still current (`airec scan`) and the OS
   adapter is powered on.
3. If initialization fails or disconnects, restart the recorder and reconnect;
   check status afterward because restart may begin recording.
4. If download/delete/clock setting reports active/paused state, explicitly stop
   only if you intend to finalize that recording.
5. For unsupported audio, save `--format raw` and investigate locally rather than
   changing recording/firmware settings speculatively.
6. In the [library](library.md), a `trace` callback can log control traffic
   for debugging. It includes private identifiers/metadata; redact it before sharing and never publish raw recordings.
