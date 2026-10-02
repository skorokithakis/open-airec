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

## Settings setters

These are the six single-value settings writes recovered from app 2.1.3. The client
exposes the five hardware-confirmed setters through `set_setting`; the rest are
not sent. Every payload is the little list in the settings UI: the
request is `55 aa len cmd value` where `len = len(value)+1`, and the app registers a
response class under the same opcode. The app does **not** decode, validate or await
the ack: the callers are fire-and-forget and the `*Response` classes for all six have
no `unpack` (only a `toString`). The reply payload is therefore unconstrained; only
the opcode matters.

All six were exercised on one recorder on 2026-10-02 (ticket S2). Status is the
observed hardware result: **HW** = the write changed the expected `0x26` field and
the original read back after a single restore write; **DROPPED** = no effect on
`0x26`; **static** = app evidence only.

| Cmd | Name | Payload (after `55 aa len cmd`) | Value/encoding | Reply observed | `0x26` field it changes | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `0x18` | LED switch | `<1 byte>` | `01` on / `00` off | `aa 55 01 18` (empty) | `[1]` `led_light` | **HW** |
| `0x19` | noise reduction | `<1 byte>` | `01` / `00` | none | `[0]` `noiseState` (unchanged) | **DROPPED** |
| `0x22` | recording segment duration | `<2 bytes BE>` | u16 minutes; `<= 0` clamped to 60; 600 = no segmentation | `aa 55 01 22` (empty) | `[3:5]` `deviceRecordDuration` BE s16 | **HW** |
| `0x2a` | mic gain | `<1 byte>` | 1-7 (key description); app default 4 | none | `mic_gain` u8 | **HW** |
| `0x2e` | power-on recording | `<1 byte>` | `01` / `00` | none | `power_on_record` u8 | **HW** |
| `0x39` | idle shutdown time | `<4 bytes BE>` | u32; picker 30/60/120/180/240/480/525600, default 60 | `aa 55 01 39` (empty) | idle shutdown u32 (13-byte `0x26`) | **HW** |

Hardware results (one recorder, 2026-10-02, recorder stopped, phone app
disconnected; the `0x26` payload was 13 bytes). Each setter was written once, `0x26`
was read back, then the field was restored with one write and read back again:

- `55 aa 02 2e 00` left power-on recording off (`[11]`: `01` -> `00`). No
  same-opcode reply. Not restored; it must end off.
- `55 aa 02 18 00` cleared `[1]` (`01` -> `00`); `55 aa 02 18 01` restored it. Both
  writes were answered with `aa 55 01 18` (empty payload).
- `55 aa 02 19 00` produced no reply and no change to `[0]` (still `01`) after a 4 s
  settle, so `0x19` is dropped: it does not drive the `0x26` noise field on this
  firmware.
- `55 aa 02 2a 02` set `[10]` (`01` -> `02`); `55 aa 02 2a 01` restored it. No reply.
- `55 aa 03 22 00 3d` set `[3:5]` (`003c` -> `003d`); `55 aa 03 22 00 3c` restored it.
  Both writes answered with `aa 55 01 22`.
- `55 aa 05 39 00 00 00 3c` set `[5:9]` (`0000001e` -> `0000003c`); the original
  `55 aa 05 39 00 00 00 1e` restored it. Both writes answered with `aa 55 01 39`.
  `0x39` works alone; no `0x23` is needed.

Notes and risks:

- **Acks are optional and their absence is not failure.** `0x18`, `0x22` and `0x39`
  returned an empty same-opcode frame, while `0x2a` and `0x2e` returned nothing yet
  still changed `0x26`. Every observed reply payload was empty. A caller must
  confirm via a `0x26` read-back; treat a missing same-opcode ack as unconfirmed
  success, not failure.
- **Shared/merged `createValue` bodies.** The binary uses `dedup_instructions`. The
  full function table for the request classes has only 17 distinct `createValue`
  bodies; `0x18`, `0x19`, `0x2a` and `0x2e` have no body of their own. The only
  plain single-byte writer in the table is `SetDefaultMonitorRequest::createValue`
  at `0x259e20c` (`writeByte(field_f)`), so by elimination the four single-byte
  setters share that code. Hardware: `0x18`, `0x2a` and `0x2e` did change the
  expected field with the byte written directly, but `0x19` did not, so the shared
  body cannot be assumed to cover `0x19` on this firmware.
- **`0x22` is also written automatically, including at record start.** The app sends
  it on connect correction and from `AIREC_RecordMgr.start`/`startRecordDevice` just as
  recording starts, using 600 for "no segmentation" and otherwise the saved user
  value. A client must not treat this as a user action. All six setters were
  validated only while stopped.
- **While recording.** `0x22` is sent around the record-start transition, and the
  live recorder page's command sheet can send `0x18` on/off. The settings-page paths
  (`0x19`, `0x2a`, `0x2e`, `0x39`) had no recording-state guard in the code, but the
  S2 probe required stopped state and did not exercise them while recording.
- **`0x39` is paired with `0x23` in the app but does not need it.** The app sends
  `0x23` and, 100 ms later, `0x39` with the same value (525600 -> `0x23` gets 240
  while `0x39` keeps 525600). On hardware, `0x39` alone changed the `0x26` idle
  field, so `0x23` is not required and remains excluded/never sent.
- **LED polarity is resolved.** The key description "是否关闭指示灯" (whether to
  turn the indicator *off*) suggested an inverted meaning, but payload `01` sets
  `[1]` (register `led_light`) and payload `00` clears it, matching the app's debug
  label "LED 开" (LED on): `01` = LED on, `00` = LED off. Only the register mapping
  was observed; the physical LED was not visually confirmed.
- No setter-specific failure reply was observed. Do not invent one; treat a missing
  same-opcode ack as unconfirmed success and verify with `0x26`.

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
  transfer commands. Wi-Fi hardware remains unconfirmed. See [Wi-Fi](wifi.md).
- Settings writes not exposed by `set_setting`: `0x19`, `0x23`, `0x24`,
  `0x2b`-`0x2d`, `0x38`, `0x3a`-`0x3f`, `0x6d`, `0x6e`, `0x71`, `0x72`,
  `0x77`-`0x7b`.
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
