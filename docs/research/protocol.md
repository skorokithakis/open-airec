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
| `55aa1307 ID 00000000` | Download archive from zero offset | `0x07` matching ID + size; raw data on `4a`; empty `0x09` end; failure `0xfd` |
| `55aa0108` | Cancel failed archive transfer | Best-effort cleanup followed by disconnect |
| `55aa0f0a ID` | Delete single archive | Empty `0x0a` ack or `0xfc` failure; verify catalog absence |
| `55aa0f02 TIME` | Set clock | Empty `0x02` ack; verify clock read-back |
| `55aa0130` | Read clock | `0x30` with 14 ASCII timestamp bytes |

`ID` and `TIME` are exactly 14 ASCII bytes `YYYYMMDDHHMMSS` and must be valid
calendar values. No timezone on the wire. Catalog/stop/download sizes are four-byte
big-endian unsigned values. Storage accepts two-/four-byte big-endian values in
device-reported MB; free must not exceed total.

The app's packer explicitly sets download length 19 and deletion length 15;
these equal ordinary framing for the implemented 18-byte and 14-byte payloads.
No clock-sync step is required for archive selection/download.

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
  transport MTU, subscribe to `4a`, send ID/zero-offset request. Control ack,
  completion and bulk bytes may arrive in different orders; all are required.
  Extra bytes, identity/size mismatch, overflow, rejection or timeout fail.
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

References: [RFC 3533](https://www.rfc-editor.org/rfc/rfc3533),
[RFC 6716](https://www.rfc-editor.org/rfc/rfc6716),
[RFC 7845](https://www.rfc-editor.org/rfc/rfc7845).
