# Wi-Fi transfer (unvalidated)

Everything here comes from static analysis of the 2.1.3 app (Blutter output under
ignored `artifacts/re/out/asm/airec/`), cross-checked against
[protocol.md](protocol.md). **None of it was tested on real hardware.** The only
recorder available reports `0x20` work mode 1 (`jl + ble`), which has no Wi-Fi, so
this feature cannot be validated on it. No command in this document is sent by
`src/airec/client.py`; treat this as reconnaissance for a future Wi-Fi feature.

Credentials, SSIDs and the app's fixed hotspot password are deliberately not
reproduced here. Do not add real ones to this file.

## How a device advertises Wi-Fi

`0x20` returns one byte: `work_mode = b >> 4`, `audio_format = b & 0x0f`. From the
app's `workModeDesc` (see [protocol.md](protocol.md#device-information)), these work
modes have a Wi-Fi radio:

| work mode | description |
| --- | --- |
| 4 | 3085 + ble + wifi (legacy) |
| 6 | jl + wifi hotspot |
| 7 | jl + ble + wifi hotspot |
| 8 | jl + ble + wifi station |
| 9 | jl + ble + wifi station + hotspot |
| 10 | 3085 + ble + wifi |

Work modes 1 (`jl + ble`) and 11 (TWS earbuds) have no Wi-Fi. The app keys Wi-Fi
behaviour off the chip type / work mode and the hotspot SSID, not off a direct
capability flag.

## Wi-Fi commands

These are ordinary `55 aa LEN CMD PAYLOAD` control-characteristic frames. All of
them are excluded from this client.

| Cmd | Name | Request | Reply | Notes |
| --- | --- | --- | --- | --- |
| `0x50` | get AP name | no payload | length byte + UTF-8 name | reply is `readUTFBytes` over the available bytes, `trim`med |
| `0x60` | set Wi-Fi | index byte, SSID, password, then a third field | ack | see layout below |
| `0x61` | delete Wi-Fi | not recovered | ack | body was merged by `dedup_instructions` |
| `0x62` | link Wi-Fi | not recovered | ack | |
| `0x63` | unlink Wi-Fi | not recovered | ack | |
| `0x64` | get Wi-Fi params | index byte | index + status + SSID + password | **returns stored credentials; never query it** |
| `0x65` | Wi-Fi status | not recovered | two bytes (the reply's `toString` labels them index and status) | |
| `0x66` | set Wi-Fi work mode | one byte (1 station, 2 hotspot; 0 also sent) | ack | `WifiCloseRequest`, empty-body `createValue` is shared |
| `0x67` | close Wi-Fi | no payload | ack | `WifiSetModeRequest`, empty payload |
| `0x68` | open Wi-Fi | no payload | ack | |
| `0x69` | set transfer mode | one byte: 0 BLE, 1 Wi-Fi | ack | |
| `0x6b` | get Wi-Fi MAC | no payload | 6 bytes, formatted `xx:xx:...` | |
| `0x6c` | set Wi-Fi speed | one byte level, bounded by the app | ack | also sent over the Wi-Fi socket (see below) |
| `0x77` | default Wi-Fi switch | one byte enable | ack | |

**The `0x66`/`0x67` labels in the app are swapped.** The app key enum is named
`AIREC_close_wifi` for `0x66` and `AIREC_set_wifi_mode` for `0x67`, and the key
descriptions are swapped relative to what the request classes actually do. The
class that sends `0x66` prints "设置wifi工作模式 (1=station, 2=hotspot)", and the
class that sends `0x67` prints "关闭wifi" and has an empty payload. Trust the
class behaviour, not the key names.

### `0x60` payload layout

The setter (`WifiSetRequest.createValue`) writes, in order:

1. index byte;
2. the SSID in a fixed-width field;
3. the password in a fixed-width field;
4. a third string field that the caller always sends empty.

`WifiGetParamResponse.unpack` reads the same layout: index, status, then
`readUTFBytes(32)` for the SSID and `readUTFBytes(64)` for the password, before
stripping control characters and trimming. The setter's padding helper is named
`padStringTo32Bytes`; an earlier summary called both string fields 32 bytes, but the
parser's fixed reads are 32 then 64. Marked unvalidated: the absolute field widths
were not confirmed on hardware.

## The Wi-Fi session

The app brings up a **device hotspot** and sends the transfer over a socket to the
device. From `socket_manager.dart` and `wifi_udp_transfer_session.dart`:

- Hotspot SSIDs seen in the app are built as `JAVA_AIREC_<mac>` and
  `AIREC_WIFI_<mac>` (also scanned as `AI_Mic*`); `WifiUdpConstants` normalises the
  MAC out of the SSID.
- Device/subnet: `192.168.11.0/24`, device at `192.168.11.1`.
- Two connections: **audio** on port `32769` and **command** on port `32770`.
- The session start handshake is the four bytes `55 AA 99 66`, sent to the device;
  the device's UDP confirmation is `11 22 AA BB`. The app resends the handshake
  until it sees data, giving up after 30 s.
- Transport selection is by SSID: `protocolForSsid` returns UDP for a legacy
  `JAVA_AIREC_` hotspot and TCP otherwise (`AIREC_WIFI_`). This mapping is inferred
  from the `_connectAudio` branch; the enum values themselves are not named in the
  output.
- The app idle-disconnects the socket and closes the device hotspot after 360 s of
  no new data.

### UDP path

`_connectAudioUdp` binds the audio/command sockets and `NativeUdpReceiver` (an FFI
plugin) writes received bytes straight to the local file at a given `offset`. The
handshake is re-sent on resume ("续传前重发握手包"). Progress and completion are
reported by the native receiver through events (`progress`, `bytesWritten`,
`handshake_confirmed`, `backpressure`).

Completion uses `WifiUdpDiskGate` against the expected size:

- finished when `expected <= 0 and disk > 0`, or `disk + 64 >= expected`, or
  `expected - disk <= 64`, or `disk >= 0.99 * expected`;
- "near complete" when `disk / expected >= 0.88`.

That is much more tolerant than this client's exact byte count; do not copy it.

### TCP path

`_connectAudioTcp` connects to the same `192.168.11.1` and ports and sends the same
`55 AA 99 66` start handshake over TCP. On the Telink family the app instead uses
`wifi_transfer/wifi_tcp_file_transfer.dart`:

- host `192.168.200.1` for chip type 20, otherwise `192.168.43.1` (from
  `WifiHotspotController.tcpHost`);
- a chip-type-dependent TCP port from `WifiHotspotController.tcpPort` (the exact
  port constants could not be recovered with confidence);
- a `12 5B <cmd> 00 <len16> <payload> 5B 12` "net payload" framing
  (`wifi_tcp_protocol.dart`), where `<cmd>` is a Wi-Fi-side command byte, not an
  AIREC `0x..` opcode;
- `cmd=0x02` is a file-list request, `cmd=0x20` is a files request
  (`offset=0, size=...`), and the device pushes file data as `cmd=0x17` blocks;
- after the TCP connection is ready the app also sends BLE hints (raw `11 2B`
  channel switch and `11 28 <name>` read-file, outside the AIREC opcode table) and
  waits for the device to stream on TCP;
- **TCP completion check:** each block updates a running CRC32. `_onFileComplete`
  compares the received CRC against the value declared in the file list (it tests
  the complement in one branch) and logs `OK`/`MISMATCH`, or warns
  "device did not provide CRC, file may be incomplete". It saves the file either
  way; completion of the whole transfer is when every listed file has completed.

## Resume by offset (`0x07` / `0x08`)

This is the same resume mechanism as BLE, driven from `wifi_udp_transfer_session.dart`:

- `currentDiskOffset` = current length of the local file; the app resumes with
  `0x07` `syncFile(uid, offset)` and appends the incoming bytes. It never trusts
  the `0x07` ack payload.
- On a stall the app escalates in stages:
  - **soft resume** — `NativeUdpReceiver.softResync` (drop and re-send the
    handshake) then wait for the handshake and send `0x07` at the current offset.
    A 6 s no-growth watch escalates to hard resume.
  - **hard resume** — stop the native receiver, bump the speed, restart the
    receiver and send `0x07` at the current offset.
  - **Wi-Fi restart** (`wifiRestartKeepOffset`) — keep the file, re-send
    `0x66` work-mode writes, `0x69(1)`, `0x68`, reconnect to the hotspot found
    from the saved SSID, restart the receiver and send `0x07` at the current
    offset. The app snapshots a prefix of the local file first and, after the
    restart, verifies the new bytes continue that prefix, otherwise it rejects the
    result.
- `0x08` (`EndSyncRequest`, "stop sync") pauses/finalises: the app sends it before
  deleting a file that is downloading, and the reply keeps the partial local file
  for a later `0x07` resume. `0xFD` within 3 s of a `0x08` is treated as a normal
  reply to the stop, not a failure.
- No resume path in the app verifies an exact byte count; it works from local disk
  length vs the catalog size (and the CRC check on the Telink TCP path).

## Wi-Fi side effects before a BLE sync

On Wi-Fi-capable models the app's `AudioSyncMgr.start` writes to the device before
a BLE file sync: it sends `0x69(0)` (switch back to BLE transfer mode) and `0x67`
(close the hotspot), and in some paths also `0x66(0)`. A BLE-only client does not
need any of this and this client sends none of it; it is noted because those writes
mutate device Wi-Fi state.

## Unknowns / gaps

- Nothing here is hardware-validated; the only device is work mode 1 (no Wi-Fi).
- `0x61`/`0x62`/`0x63` request payloads were not recovered from the binary.
- The absolute widths of the `0x60` SSID/password fields (32/64 vs 16/32) are
  inferred from the parser's two fixed reads and the padding helper's name.
- The UDP vs TCP transport enum names and the exact TCP chipType-to-port mapping
  were not recovered.
- What actually triggers a device to push data (versus only replying), and the
  semantics of the raw `11 2B` / `11 28` BLE hints, are not known.
- `0x64` must never be queried; it returns stored Wi-Fi credentials.
