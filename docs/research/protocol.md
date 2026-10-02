# Implemented BLE protocol

This describes the primary profile observed on one recorder, not a universal
vendor specification. Use UUIDs, not ATT handles (which vary).

| Role | UUID |
| --- | --- |
| Primary service | `0011200a-2233-4455-6677-8899dfdedddc` |
| Live notifications (not implemented) | `0011201a-2233-4455-6677-8899dfdedddc` |
| Control write without response | `0011202a-2233-4455-6677-8899dfdedddc` |
| Control notifications | `0011203a-2233-4455-6677-8899dfdedddc` |
| Archive data notifications | `0011204a-2233-4455-6677-8899dfdedddc` |
| OTA (never write) | `0011206a-2233-4455-6677-8899dfdedddc` |

Requests: `55 aa LENGTH COMMAND PAYLOAD`. Replies: `aa 55 LENGTH COMMAND PAYLOAD`.
Length includes command and payload. A control notification can contain multiple
frames or fragments. Requests use write-without-response; a queued write alone
is not proof of execution. Acknowledgements/status checks provide that evidence.

## Connection

Validate service and characteristic properties, subscribe to `3a`, wait 200 ms,
send `55aa0101`, and require a nonempty `0x01` identity reply. No pause toggle,
clock setting, cloud/login or pairing is part of initialization. Advertising may
omit the primary service, so address discovery is followed by connected validation.

## Commands exposed by the client

| Request | Meaning | Expected reply/check |
| --- | --- | --- |
| `55aa0101` | Identity/MAC initialization | Nonempty `0x01` payload |
| `55aa010e` | Battery | `0x0e` with one byte 0–100 |
| `55aa0105` | Catalog | `0x05` rows, empty `0x06` end |
| `55aa010b` | Storage | `0x0c` free and `0x0d` total, failure `0xfb` |
| `55aa010f` | Recording status | `0x0f`: active `00` + timestamp, paused `01`, stopped `02` |
| `55aa0103` | Start recording | `0x03` timestamp reply; active status verified after mode hint |
| `55aa022152` | Ordinary recording mode hint | `0x21`, payload ASCII `R`; no acknowledgement required |
| `55aa0110` | Pause/resume toggle | Empty `0x10` ack or `0xff` failure; verify requested state |
| `55aa0104` | Stop/finalize current recording | `0x04` timestamp + four-byte size, then stopped status |
| `55aa1307 ID OFFSET` | Download archive from `OFFSET` | `0x07` matching ID + full catalog size; raw data on `4a`; empty `0x09` end; failure `0xfd` |
| `55aa0108` | Stop/cancel archive transfer | Best-effort cleanup followed by disconnect; firmware may answer `0xfd` instead of `0x08` |
| `55aa0f0a ID` | Delete single archive | Empty `0x0a` ack or `0xfc` failure; verify catalog absence |
| `55aa0f02 TIME` | Set clock | Empty `0x02` ack; verify clock read-back |
| `55aa0130` | Read clock | `0x30` with 14 ASCII timestamp bytes |
| `55aa0112` | Firmware version | `0x12` whole payload UTF-8 |
| `55aa0129` | Firmware type | `0x29` whole payload UTF-8 |
| `55aa0120` | Chip/work mode | `0x20` exactly one byte: high nibble work mode, low nibble audio format |
| `55aa0136` | Power/charging state | `0x36` one byte: `01` charging, `00` not |
| `55aa0126` | Device settings/capabilities | `0x26` variable-length settings payload |

`ID` and `TIME` are exactly 14 ASCII bytes `YYYYMMDDHHMMSS` and must be valid
calendar values. No timezone on the wire. Catalog/stop/download sizes are four-byte
big-endian unsigned values. Storage accepts two-/four-byte big-endian values in
device-reported MB; free must not exceed total.

The app's packer explicitly sets download length 19 and deletion length 15;
these equal ordinary framing for the implemented 18-byte and 14-byte payloads.
No clock-sync step is required for archive selection/download.

## Download offset

`OFFSET` is a four-byte big-endian unsigned byte position into the archive. The
firmware honors it byte-exactly, including unaligned values: the probe downloaded
one archive at offset 0, then at 800 (80-byte aligned) and 801 (unaligned), and each
nonzero transfer returned exactly `size - offset` bytes that were byte-identical to
the corresponding slice of the offset-0 download.

The `0x07` acknowledgement always carries the **full catalog size**, not the
remaining `size - offset`, and the stream ends with an empty `0x09`. A client must
therefore check the ack against the catalog size and expect `size - offset` stream
bytes. The app ignores the ack payload and compares only local file size with the
catalog size. On interruption this client reports the received prefix in
`DownloadInterrupted.partial` so the caller can resume from it.

## Device information

These queries have no payload and are read-only.

- `0x12` firmware version and `0x29` firmware type return the whole payload as a
  UTF-8 string (the probe saw `2.0.0` and `A3AA`).
- `0x20` returns exactly one byte: `work_mode = b >> 4`, `audio_format = b & 0x0f`.
  Audio format: 0 opus, 1 wav, 2 mp3, 3 pcm, other unknown. Work mode from the app:
  1 jl + ble, 4 3085 + ble + wifi (legacy), 6 jl + wifi hotspot, 7 jl + ble + wifi
  hotspot, 8 jl + ble + wifi station, 9 jl + ble + wifi station + hotspot,
  10 3085 + ble + wifi, 11 JL TWS translation earbuds; other values are unknown.
  The probe saw `10`: work mode 1 (jl + ble), opus.
- `0x36` returns one byte: `01` charging, `00` not. The probe saw `00`.
- `0x26` returns a variable-length settings payload; `L` is its length:
  - `[0]` noise reduction (nonzero = on), `[1]` LED (nonzero = on), `[2]` unknown
    (preserve in the raw payload).
  - `[3:5]` recording segment duration, big-endian u16 (the `0x22` setter labels it
    minutes).
  - If `L` is 9 or 10, idle shutdown is a u8 at `[5]`; otherwise it is a big-endian
    u32 at `[5:9]`. Units are unconfirmed.
  - Then `usb_support` u8, `mic_gain` u8, `power_on_record` u8.
  - If `L == 10` or `L >= 13`, `disk_format_supported` u8. This is a capability
    flag, not a format action.
  - If `L >= 15`, `default_wifi_on` u8 and `default_monitor_on` u8 (each `== 1`).
  - Lengths below 9, or a variant that does not fit, are rejected. The probe saw a
    13-byte payload with the power-on-recording flag set, which matches the observed
    power-on recording behaviour.

Other read-only queries exist but are not implemented, because they are
earbud-specific or Wi-Fi-related and unconfirmed: `0x6f` key-customization settings
(layout not analysed), `0x65` Wi-Fi status (two bytes), `0x6b` Wi-Fi MAC and `0x50`
Wi-Fi AP name. `0x64` is excluded because it returns credentials.

During recording the device can also emit `0x36` and `0x3b` unsolicited; `0x3b`
carries a two-byte value, likely elapsed seconds (unconfirmed). A caller must not
treat an unsolicited `0x36` as the reply to its own query. Other device-originated
frames identified from the app are `0x1a`/`0x1b`/`0x1c` (device-button recording),
`0x35`, `0x37` (shutdown reminder), `0x3d` (call state) and the one-key events
`0x31`-`0x33`/`0x41`-`0x43`.

## Excluded commands

These are never sent by this client. They are OTA, destructive, credential-bearing
or state-changing, and several were not validated on hardware.

- `0x25`, `0x27`, `0x28`: firmware upgrade start/end/index (OTA characteristic
  `6a`).
- `0x3c`: disk format. Failure replies are `0xf0` (no card), `0xf1` (cannot format)
  and `0xf2` (card damaged).
- `0x34`: delete-file-count. Semantics unknown; treat as destructive.
- `0x64`: returns the stored Wi-Fi SSID and password. Never query it.
- `0x60`-`0x63` and `0x66`-`0x69`, `0x6c`: Wi-Fi set/delete/link/unlink/mode and
  transfer commands. Wi-Fi hardware remains unconfirmed.
- Settings writes: `0x18`, `0x19`, `0x22`-`0x24`, `0x2a`-`0x2e`, `0x38`-`0x3f`,
  `0x6d`, `0x6e`, `0x71`, `0x72`, `0x77`-`0x7b`.
- `0x37` is device-originated (shutdown reminder), not a request.

## Sequences and safeguards

- Start: require stopped state, send `0x03` and validate reply, wait 100 ms, send
  `0x21 R`, then verify active status. The tested firmware never acknowledges the
  mode hint; waiting for that reply caused a false timeout in early development.
- Pause/resume: query status, return unchanged if already desired, refuse stopped
  state, send toggle once and require ack plus confirmed state. Never blindly retry.
- Delete: require stopped state, fetch full catalog, require a unique requested
  ID, delete once, require ack, fetch full catalog and verify absence.
- Clock set: require stopped state, send naive local wall time and require ack,
  query `0x30`, verify read-back. Clock updates affect future timestamp IDs only.
- Download: require stopped state or explicit permission to finalize, prepare
  transport MTU, subscribe to `4a`, send the ID/offset request (offset defaults to
  0). The ack must carry the catalog size and the stream must deliver exactly
  `catalog size - offset` bytes. Control ack, completion and bulk bytes may arrive
  in different orders; all are required. Extra bytes, identity/size mismatch,
  overflow, rejection or timeout fail, and an interruption after the request is
  sent is reported as `DownloadInterrupted` with the contiguous received prefix
  in `.partial` for a caller-managed resume.
- Stop/cancel uses `0x08`; a `0xfd` within about 3 s of the stop request may be the
  firmware's stop reply rather than a failure. Cleanup is best-effort and followed
  by disconnect.
- Storage: collect both reply types in either order, tolerate identical duplicates,
  reject conflicting duplicates, malformed lengths, failure or impossible totals.

Linux/Bleak 0.22 downloads isolate the private BlueZ AcquireWrite MTU workaround;
the default/stale characteristic limit may be 20 despite negotiated MTU 247. A
download request is 22 bytes. The write path splits logical requests to its known
payload limit; no hard-coded ATT handle or write-with-response fallback is used.

## Audio

The validated archive profile is fixed 80-byte mono Opus packets. They are not
framed with `aa55` and must not pass through the control decoder. Ogg output uses
OpusHead/OpusTags, per-packet TOC timing, Ogg CRC and an end-of-stream page; original
encoder delay is unknown, so pre-skip is zero. No decoding/re-encoding is performed.
The firmware may finalize an archive with a truncated final 80-byte slot. This was
observed on 6 of 11 archives from one recorder, with remainders of 32, 48 and 64
bytes. The wrapper drops that remainder after it checks the remainder's leading
TOC byte. Up to 79 trailing raw bytes can be left out of the wrapped audio. Raw
format preserves the tail.

References: [RFC 3533](https://www.rfc-editor.org/rfc/rfc3533),
[RFC 6716](https://www.rfc-editor.org/rfc/rfc6716),
[RFC 7845](https://www.rfc-editor.org/rfc/rfc7845).
