# Live audio (L1 static research)

Scope: what the 2.1.3 app does for live/realtime audio, derived only from the
decompiled snapshot. No device was contacted for this document; no `src/` or
`tests/` change is part of it. This is the L1 companion to ticket `oa-brixi`; the
hardware probe is L2 (`oa-aosqc`, blocked on this work).

L2 hardware update (2026-10-02): the live probe ran for ticket `oa-aosqc`. The
stream is active during a recording **without `0x6d`**, and the on-wire framing
is fixed 80-byte Opus packets. See
[L2 live probe](#l2-live-probe-hardware-2026-10-02) below; the original L1 text
is preserved unchanged.

Sources, relative to the app's Blutter output `artifacts/re/out/asm/airec/`:

- `airec_module/record/realtime/realtime_monitor_page.dart` (the "AIREC 实时听音"
  page; contains `_RealtimeMonitorPageState` plus a debug command sheet).
- `airec_module/record/realtime/realtime_monitor_client.dart` (WebSocket bridge).
- `airec_module/record/realtime/ctrl_channel_client.dart` (second WebSocket).
- `airec_module/record/realtime/clip_recorder.dart`.
- `airec_bluetooth/xlx_link/bluetooth_manager.dart` (the BLE notify path,
  including the `0011201a` handler).
- `airec_bluetooth/xlx_link/ble_mgr.dart`,
  `protocol/command/setting/wifi.dart`, `.../record.dart`,
  `protocol/command/key_profiles.dart`, `protocol/command/helper.dart`.
- `ka_frame_synchronizer.dart` (Opus frame alignment).
- `airec_common/config/device_profile.dart` plus the const profile objects in
  `artifacts/re/out/objs.txt`.

Addresses are code addresses in this snapshot, not APIs.

## Headline

The app has **two separate "live audio" mechanisms** and they should not be
conflated:

1. **The realtime-listen page** (`realtime_monitor_page.dart`) is a
   **Wi-Fi/cloud** feature. The device streams audio over its Wi-Fi connection to
   the vendor server; the app receives that audio over a **WebSocket** and plays
   it locally. BLE is used only to command the device (`0x65`/`0x68` for Wi-Fi,
   `0x6d` to open and `0x6e` to close the realtime transfer). The audio never
   reaches the app over characteristic `0011201a`.
2. **A direct-BLE live-audio path** exists in `bluetooth_manager.dart`: the app
   subscribes to `0011201a` notification at connect time and decodes a framed
   audio stream. This is independent of the realtime-listen page and independent
   of `0x6d` in the app's own logic.

The L2 probe below is aimed at the second path (does `0x6d` make the firmware
emit on `0011201a`), because that is the only thing an offline AIREC client can
consume without the vendor cloud.

## Ticket questions

### 1. Payloads of `0x6d`/`0x6e` and their replies

- `RealtimeTransferOpenRequest` (0x6d) and `RealtimeTransferCloseRequest` (0x6e)
  subclass `AIREC_BleRequest` with **no `createValue` override** (`setting/wifi.dart`
  classes at `0x267aaf8` and `0x267ab04`; they define only `toString`). Ordinary
  framing therefore produces **no payload**:
  - open = `55 aa 01 6d`
  - close = `55 aa 01 6e`
  - **CONFIRMED** (framing from `protocol/command/request.dart` `pack` at
    `0x17a0e38`, the same path the implemented client uses).
- `RealtimeTransferOpenResponse` (class 8510) has **no members at all** and
  `RealtimeTransferCloseResponse` (`0x267a0a0`) defines only `toString`; neither
  overrides `unpack`. The command dispatch table
  (`setting/setting_command.dart` at `0x1819cd4` / `0x1819d14` and `0x1819d4c` /
  0x1819da4) will build these objects for an incoming `aa 55 .. 6d`/`6e`, but the
  app does not read their payload. `AIREC_CommandHelper::write` (`0x179733c`)
  only packs and writes; the caller does not await a reply.
  - Reply, if the firmware sends one: `aa 55 01 6d` / `aa 55 01 6e` (empty
    payload). Status **CONFIRMED** for "app expects/parses nothing"; **UNKNOWN**
    whether the firmware actually answers and with what bytes.
- The page sends `0x6d` and `0x6e` fire-and-forget; there is no handler
  registered for either notification on this page.

### 2. Is `0x6d` required before the device notifies on `0011201a`?

- App side: **no**. `_monitorUART` subscribes to `0011201a` whenever the
  characteristic is discovered in the primary UART service, regardless of `0x6d`:
  `bluetooth_manager.dart` `_monitorUART` at `0x188ef98`; the 1a branch is at
  `0x1890120`–`0x18903c0` (cancel-listen-then-subscribe, then
  `_enableNotify(ch, "audioRecord(实时录音 0011201a)")` at `0x18903ac`).
  Nothing in that path references the realtime command. **CONFIRMED** for app
  subscription being unconditional.
- Firmware side: whether `0x6d` is what makes the device start emitting on
  `0011201a` is **UNKNOWN**. This is exactly what L2 must answer.
- Note `0x6d` in the page is only ever sent after the device's Wi-Fi reports
  status 2 (connected to server): `_onWifiStatusResponse` at `0x2266100` sends
  `0x6d` at `0x2266280`. `_startRealtime` (`0x225b45c`) additionally guards the
  send on `_wifiInitDone` (asm `field_eb`, `0x225b61c`–`0x225b668`). So the
  intended production trigger is Wi-Fi-connected, but that is the app's choice,
  not proof the firmware requires it.

### 3. Must a recording be active?

- App side: the listen flow does **not** start or require a recording. `initState`
  (`0x24debd4`) only wires Wi-Fi/AI/history; `_handleStart` (`0x2257e2c`) checks
  the device Wi-Fi status and starts realtime; `_startRealtime` never calls
  `getRecSt`/`startRecord`. There is a *separate* debug record button,
  `_handleDebugRecordToggle` (`0x224a8d0`), which does send `startRecord` (0x03)
  and `endRecord` (0x04), but it is not part of the listen flow. **CONFIRMED**
  that the app does not require recording to send `0x6d`.
- Firmware side: whether the device will stream on `0011201a` (or to the cloud)
  without an active recording is **UNKNOWN**. The `0x3b`/`0x36` traffic below
  suggests the device treats recording as a distinct mode, but no static gate is
  visible.

### 4. Packet format and framing on `0011201a`

The app supports several chip profiles, and the 1a handler branches on
`DeviceProfile` (see `device_profile.dart`; the constant profiles are in
`objs.txt` as `Obj!DeviceProfile@...`). All profiles use 16 kHz
(`off_8 = 0x3e80`) and one of three `OpusFrameFormat` values:

| `OpusFrameFormat` | id | Framing the code expects |
| --- | --- | --- |
| `separator5B50` | 0 | `5B 50`-delimited segments, 160-byte payloads |
| `fixedKA80` | 1 | fixed 80-byte Opus frames, resynced by an Opus probe |
| `telinkFramed` | 2 | Telink framing (separate service/characteristics) |

- **`separator5B50` path — CONFIRMED at the app level:**
  `_monitorUART`'s 1a closure (`0x18905c4`) calls `splitBy5B50` (`0x1895e04`)
  on the accumulated buffer. `splitBy5B50` scans for the two bytes `0x5B 0x50`
  (`0x1895f78`/`0x1895f8c`), returns the bytes *between* successive markers, and
  keeps the tail for the next notification. The closure only processes an
  element whose length is exactly `0xA0` = 160 (`0x1890754`). So on the wire the
  stream is `5B 50 <160 bytes> 5B 50 <160 bytes> …`. The 160-byte element is
  forwarded to the ASR/AI pipeline, and the notification buffer is also written
  through `AudioFileMgr` (Opus/WAV/MP3 by record type). Whether one 160-byte slot
  is one Opus packet or two 80-byte packets is **UNKNOWN**.
- **`fixedKA80` path — CONFIRMED at the app level (80-byte frames):** a
  `KaFrameSynchronizer` (`ka_frame_synchronizer.dart`) accumulates the raw
  notification bytes and cuts them into fixed **80-byte** frames
  (`_cutAlignedFrames` at `0x18949dc`, frame size immediate `#0x50`), verifying
  sync with an `OpusProbeDecoder` (`probe` at `0x1895328`; failure triggers
  resync, `reportFrameFailure` at `0x18158dc`). This is the same 80-byte Opus
  slot size the archive download already uses.
- **Which profile the tested JL recorder uses — INFERRED.** `DeviceProfile._current`
  (`0x1815644`) looks up `RecordMgr.field_13` (chip type, set from `0x20`) in the
  profile map (`_profiles` at `0x1815780`; keys `0,2,4,8,12,14,16,18,40`). The
  probe's work mode was 1 → chip type 1, which is **not a key**, so the fallback
  object `Obj!DeviceProfile@37278d1` applies: `OpusFrameFormat = fixedKA80`,
  `OtaType = none`. If that inference is right, the tested device uses 80-byte
  aligned Opus. It has not been observed; treat as INFERRED/UNKNOWN.
- **No `aa 55` framing and no visible sequence number** on the 1a payload itself.
  The 1a data is handled by the raw audio path, not the control-frame decoder
  (`readAll` at `0x18995f8`). Any per-frame header inside the 160-byte or
  80-byte slots is not parsed as such by the app; the app passes the slot
  straight to Opus decoding. **CONFIRMED** for "not `aa55`-framed"; **UNKNOWN**
  for any slot-internal header (the app never inspects one).
- Note the `fixedKA80` synchronizer has an Opus *probe* precisely because frames
  can start mid-stream; that implies the app does not rely on an explicit
  per-notification boundary.

### 5. How the app decodes / plays it

There are again two paths:

- **Realtime-listen page (Wi-Fi/WebSocket) — CONFIRMED.** `RealtimeMonitorClient.connect`
  (`0x225ba74`) opens `wss://usa.jnnrec.com:8288/ws` with
  `/app/listen?deviceSN=<sn>&token=<token>&playerId=<id>&transcribeEnabled=<0|1>`
  (string at `0x225baec`), and pings periodically (`{"action":"ping"}`,
  closure `0x225c098`). Server events seen in `_onMessage` (`0x225c334`):
  `pong`, `registered` (with `streaming`), `stream_start`, `stream_stop`,
  `device_disconnected`, `device_offline`, plus binary frames. A binary frame is
  handed to `_onAudio` (`0x225cb28`) at `0x225caec`. `_onAudio`:
  - `_stripWav` (`0x225f068`) — if bytes 0–3 are `RIFF` and bytes 8–11 are `data`,
    drop the 44-byte header (`0x2c`); otherwise pass through;
  - `ClipRecorder::feed`/`feedAll` (local clip capture);
  - `FlutterSoundPlayer::uint8ListSink.add` (streaming playback);
  - `RealtimeTranscriptionController::feedAudio`;
  - on error, `_recoverPlayer`.
  Playback is therefore PCM/WAV-ish bytes relayed by the vendor server; the app
  does no Opus decode on this path. Exact codec/rate come from the
  `startPlayerFromStream` call (`0x225b6ac`) and were not pinned down.
  **INFERRED** for "PCM16, 16 kHz", consistent with the synchronizer.
- **Direct BLE `0011201a` — CONFIRMED decode, mode-dependent output.**
  The closure at `0x18905c4`, after framing, routes to
  `AudioFileMgr::writeOpus` / `writeWav` / `writeMp3` according to
  `RecordMgr.field_f` (record type: 0 opus, 2 wav, 4 mp3 — matching the `0x20`
  audio-format nibble), forks frames to `AliyunAsrMgr` /
  `_forkFrameToAiTranscription` (`0x1895868`), and for the `fixedKA80` branch
  decodes via `_decodeKaFramesToPcm` (`0x1893a50`) then
  `_dispatchRawStreamToAiTranscription` (`0x1892a78`, gated by
  `DeviceRawPcmGate`). So the direct path is decoded to PCM for on-device AI
  transcription and simultaneously archived as Opus/WAV/MP3.
  **CONFIRMED**; exact per-slot header remains **UNKNOWN**.

### 6. What the app sends on page exit and on disconnect

- **Page exit** — `_RealtimeMonitorPageState.dispose` (`0x2580008`):
  1. cancels timers;
  2. `sendRealtimeTransferClose` → `55 aa 01 6e` (`0x2580244`);
  3. `RealtimeMonitorClient.close` (WebSocket, `0x258025c`);
  4. `CtrlChannelClient.close` (`0x2580274`);
  5. `RealtimeTranscriptionController.dispose`, `FlutterSoundPlayer.stop/close`.
  It does **not** stop a recording. **CONFIRMED.**
- **BLE disconnect / auto-reconnect** — `_triggerDeviceReconnect` (`0x225fee8`):
  send `0x6e` → close the WebSocket → `Future.delayed` → send `0x6d` →
  `Future.delayed` → `_startRealtime` (reconnect WS). Strings at `0x225ff58`,
  `0x226003c`, `0x22600e0`. **CONFIRMED.**
- The page also opens a second WebSocket for control/transcription:
  `CtrlChannelClient.connect` (`0x24dfc94`) to `wss://usa.jnnrec.com:8388/ws/ctrl`
  (string `0x24dfce4`), sending `{sn, role:"app", token, playerId}` and pinging.
  **CONFIRMED.**
- Wi-Fi command sequence used by the page: `0x65` status on init (`0x2265d3c`),
  poll `0x65` every 2 s up to 30 times (`_startWifiPoll` `0x2265e18`), and `0x68`
  open Wi-Fi when needed (`_sendWifiOpenIfNeeded` `0x226636c`, call at
  `0x22664ac`). **CONFIRMED.**

### 7. Role of `0x3b` and `0x36` during recording

- `0x3b` (`AIREC_record_realtime_duration`, key at `0x181a998`): the reply is
  `aa 55 03 3b <2 bytes>`; `RecordRealtimeDurationResponse.unpack`
  (`setting/record.dart`, `0x271a274`, `readShort` at `0x271a2a8`) reads one
  **big-endian signed 16-bit** value. There is no request class, so this is
  **device-originated only** — it is surfaced asynchronously on the event bus
  `AIREC_record_realtime_duration` (consumers include `record_activity.dart`
  ~`0x24e5b20`, `record_trans.dart` ~`0x24f4fc8`, and
  `ai_transcription_controller.dart` ~`0x1ee2414`). It is the live elapsed-time
  tick during a recording. **CONFIRMED** for format/unsolicited; the earlier
  "likely elapsed seconds" reading stands.
- `0x36` (`AIREC_device_power_state`, key at `0x181aa78`): request `55 aa 01 36`
  has no payload (`DevicePowerStateRequest`, `record.dart` class 8447, no
  `createValue`); `DevicePowerStateResponse.unpack` (`0x271a1ec`, `readByte` `0x271a220`)
  reads **one byte** (`01` charging, `00` not). The client may query it, but the
  app also treats unsolicited `0x36` as device-originated during recording
  (`protocol.md` documents the observed unsolicited frames). **CONFIRMED** for
  request/reply shape; the unsolicited-during-recording behaviour is
  **INFERRED** (from the earlier hardware session, not from this static work).

### 8. Any `0x3c` (disk format) use on that page

- The normal realtime-listen flow never sends `0x3c`. `0x3c` is used only by
  `airec_module/my/device_manage.dart` (`sendDeviceDiskFormatRequest` call at
  `0x2130550`, page builder `_buildWidgetDeviceDiskFormat` at `0x212fefc`,
  events `AIREC_device_disk_format`/`_f0`/`_f1`/`_f2`). The request method itself
  is `ble_mgr.dart` `sendDeviceDiskFormatRequest` at `0x213056c`.
- **However**, `realtime_monitor_page.dart` contains a hidden debug command sheet
  (`_showCmdListSheet` / `_buildCmdItems`, labels listed at ~lines 6105–9473).
  One item is labelled **"磁盘格式化 ⚠️"** and its closure calls
  `sendDeviceDiskFormatRequest` (`0x22486fc` → call at `0x2248728`). So `0x3c` is
  reachable from that page through the debug UI only. **CONFIRMED.** This must
  never be exposed by the client, and an L2 probe must avoid the debug sheet.
- The debug sheet also exposes other writes the client excludes (`0x67`, `0x69`,
  `0x6b`, `0x6c`, `0x71`/`0x72`, `0x77`/`0x78`, `0x18`/`0x19`, `0x22`…, plus
  disk format). It is a research/debug build artifact, not app behaviour.

## L2 live probe (hardware, 2026-10-02)

Ticket `oa-aosqc`, one-off script `artifacts/re/probe_oa-aosqc.py` (not part of
the library; raw capture stays under `artifacts/re/probe_output/`, local only).
The recorder was **already recording** when the probe connected (status
`recording`), so the planned stopped baseline and the button-start detection were
skipped; the key observation (the stream with **no `0x6d`**) was still made. No
start/stop/format opcode was sent. Because `1a` was already active, `0x6d` was
never reached and `0x6e` was never needed; the `0x6d`/`0x6e` firmware replies
therefore remain **UNKNOWN** (L1's empty-payload request framing is unchanged).

Method:

- Initialized exactly like the library client (identity `55 aa 01 01`, required
  reply), subscribed control `3a` and live `0011201a`, and captured every `1a`
  and `3a` notification with a monotonic host offset, length and hex.
- The primary service exposed `0011201a`, `0011202a`, `0011203a`, `0011204a` and
  `0011206a`; the live characteristic was present and subscribable.
- The control trace was held off during the identity exchange so the MAC response
  was never written to disk. No device address is recorded here.

Result — `0x6d` is NOT required while a recording is active:

- `0x0f` reported `recording` before and during the capture.
- Over a ~30 s window with **no `0x6d`**, `0011201a` produced 585
  notifications / 119808 bytes (≈20 notifications/s; 621 notifications /
  127220 bytes over the full session). The stream was continuously active, so
  `0x6d` was never sent and `0x6e` was never needed.

Observed framing — fixed 80-byte Opus packets (`fixedKA80`), CONFIRMED:

- Notifications are plain **stream chunks, not framed records**: two sizes were
  seen, 244 bytes (497×) and 48 bytes (124×). 244 = negotiated MTU-3, so the
  firmware pushes a byte stream with no `aa55` header and no
  per-notification packet boundary. This matches the app's
  `KaFrameSynchronizer` (accumulate, then cut fixed 80-byte frames).
- Concatenating the notifications yields 127220 bytes. Aligning at the first
  packet boundary and cutting every 80 bytes gives 1589 complete packets plus a
  68-byte truncated tail. **Every** full packet has Opus TOC config `9`, i.e.
  the 80-byte slot profile (`fixedKA80`).
- Byte 0 is the Opus TOC, values `0x4b` (1522×) and `0x48` (67×): config 9
  (SILK wideband, 16 kHz) and mono (bit 2 clear). Byte 1 is the frame-count byte
  for code 3 (`0x41` = one frame) or the first payload byte for code 0. The
  recurring `0x4b 0x41` is **not** a magic header; it is the encoder's repeated
  TOC plus one-frame count, and the same pair leads the downloadable `.airec`
  profile, whose every 80-byte slot also decodes as Opus.
- Rate: 1589 packets over the ~31.9 s notification span = 49.8 packets/s, i.e.
  one 20 ms Opus frame per packet, ≈4.0 kB/s (≈32 kbps) at 16 kHz mono.
- No `5B 50` framing: only 2 `5B 50` byte pairs occur in the 127220 bytes and
  both fall inside Opus payload; there is no 160-byte segmentation. The
  `separator5B50` profile is not this device.
- Decode check (ffmpeg, already installed; no project dependency added): wrapping
  the 1589 aligned 80-byte packets with the library Ogg Opus muxer
  (`airec.audio.to_ogg_opus`) produced a 31.78 s mono 16 kHz PCM sample with
  rms≈2042, peak≈20595 and 88% nonzero samples. The stream is real audio. (No
  audio content is recorded here; the sample stayed local.)

Control traffic observed:

- `0x0f` reply while recording: `aa 55 10 0f 00 <14 ASCII digits>` (state
  recording, device-local timestamp).
- 150 unsolicited `aa 55 03 3b XX XX` frames at ~5 Hz over 30 s. Read as
  big-endian signed 16-bit, the value rose 134→163 (+1/s): elapsed **seconds**.
  This reconfirms L1's `0x3b` = BE s16 live duration tick, now on hardware.
- No `0x6d`/`0x6e` traffic (neither was sent).

Disconnect behaviour: **none observed** — the link stayed up for the whole
session (no drop, no reconnection), despite the recorder's ~-80 dBm RSSI.

State changes: the only writes were `55 aa 01 01` (identity, part of
initialization) and a single read-only `55 aa 01 0f`. No `0x03`/`0x04`/`0x6d`/
`0x6e`/`0x3c` or any other opcode was sent, and the active recording was left
running for the user to stop with the button.

## Evidence index

| Topic | App evidence (code address) |
| --- | --- |
| `0x6d`/`0x6e` opcodes | `key_profiles.dart` `AIREC_realtime_open` `0x181a5a8`, `_close` `0x181a570` |
| Requests are payload-free | `setting/wifi.dart` `RealtimeTransferOpenRequest` `0x267aaf8`, `CloseRequest` `0x267ab04` (only `toString`) |
| Responses have no parser | `RealtimeTransferOpenResponse` class 8510 (empty); `CloseResponse` `0x267a0a0`; dispatch `setting_command.dart` `0x1819cd4`/`0x1819d14`, `0x1819d4c`/`0x1819da4` |
| Fire-and-forget write | `protocol/command/helper.dart` `write` `0x179733c` |
| `0x6d` sent after Wi-Fi ready | `realtime_monitor_page.dart` `_onWifiStatusResponse` `0x2266100` (send `0x2266280`); `_startRealtime` `0x225b45c` (guard `0x225b61c`, send `0x225b668`) |
| `0x6e` on exit | `dispose` `0x2580008` (send `0x2580244`) |
| `0x6e`→`0x6d` on reconnect | `_triggerDeviceReconnect` `0x225fee8` |
| WS audio client | `realtime_monitor_client.dart` `connect` `0x225ba74` (URL `0x225baec`), `_onMessage` `0x225c334`, `close` `0x2264010` |
| WS audio playback | `realtime_monitor_page.dart` `_onAudio` `0x225cb28`, `_stripWav` `0x225f068` |
| Ctrl WS | `ctrl_channel_client.dart` `connect` `0x24dfc94` (URL `0x24dfce4`); `_startCtrl` `0x24dfa68` |
| Wi-Fi `0x65`/`0x68` | `_initWifiAndRealtime` `0x2265b78`; `_startWifiPoll` `0x2265e18`; `_sendWifiOpenIfNeeded` `0x226636c` |
| `1a` subscribed at connect | `bluetooth_manager.dart` `handleService` `0x188b9a4`, `_monitorUART` `0x188ef98`, 1a branch `0x1890120`–`0x18903c0` |
| `5B 50` framing, 160-byte slots | `_monitorUART` closure `0x18905c4` (0xA0 check `0x1890754`), `splitBy5B50` `0x1895e04` (`0x5b`/`0x50` compare `0x1895f78`/`0x1895f8c`) |
| 80-byte Ka frames | `ka_frame_synchronizer.dart` `_cutAlignedFrames` `0x18949dc` (`#0x50`), `_trySync` `0x1894d30`, `probe` `0x1895328`, `reportFrameFailure` `0x18158dc` |
| Profile→framing table | `objs.txt` `Obj!DeviceProfile@...` (`fixedKA80`/`telinkFramed`/`separator5B50`); `device_profile.dart` `_current` `0x1815644`, `_profiles` `0x1815780`, `forChipType` `0x1864c18` |
| `0x3b` = BE s16 | `record.dart` `RecordRealtimeDurationResponse.unpack` `0x271a274` (`readShort` `0x271a2a8`) |
| `0x36` = 1 byte | `record.dart` `DevicePowerStateRequest` class 8447; `DevicePowerStateResponse.unpack` `0x271a1ec` (`readByte` `0x271a220`) |
| `0x3c` on the page (debug only) | `realtime_monitor_page.dart` closure `0x22486fc`/call `0x2248728`; label `磁盘格式化 ⚠️`; `ble_mgr.dart` `sendDeviceDiskFormatRequest` `0x213056c`; `device_manage.dart` call `0x2130550` |
| Debug record button | `_handleDebugRecordToggle` `0x224a8d0` (`startRecord` `0x224a980`, `endRecord` `0x224a9f0`) |

## Proposed L2 probe sequence

Note: this is the L1 proposal. The executed L2 probe (see
[L2 live probe](#l2-live-probe-hardware-2026-10-02)) found the stream already
active without `0x6d`, so phase 1 was not run and no `0x6d`/`0x6e` was sent.

Goal: determine whether `0x6d` starts a stream on `0011201a`, and under what
state (stopped vs recording), and what the raw framing looks like. Read-only
apart from `0x6d`/`0x6e` and, in phase 2, a controlled start/stop.

This probe writes `0x6d`/`0x6e`, which `protocol.md` currently classifies under
excluded setting writes. **It needs explicit approval and a controlled
recording.** It must not touch Wi-Fi (`0x65`/`0x68`), disk format (`0x3c`),
OTA, delete, or the debug sheet.

Setup:

1. Connect with the fake-safe client, subscribe to control `3a` **and** live
   `1a` notifications, and log every `1a` notification with a host timestamp and
   byte count.
2. Wait 200 ms, send `55 aa 01 01`, require the identity reply.
3. Send `55 aa 01 0f` (recording status) and record the state.
4. Observe `1a` silently for ~5 s as a baseline (expect nothing).

Phase 1 — `0x6d` while stopped:

5. Send `55 aa 01 6d`. Watch `3a` for an `aa 55 .. 6d` reply; capture if present.
6. Observe `1a` for 15 s. Record any bytes and look for `5B 50` markers, 160-byte
   multiples, or 80-byte alignment.
7. Send `55 aa 01 6e`. Capture any `aa 55 .. 6e` reply.
8. Observe `1a` for 5 s to confirm it goes quiet, then re-check `55 aa 01 0f`.

Phase 2 — `0x6d` while recording (only if phase 1 was inconclusive):

9. Send `55 aa 01 03` (start), require the `0x03` reply; wait 100 ms; send
   `55 aa 02 21 52` (ordinary mode hint; no ack expected); confirm active with
   `55 aa 01 0f`.
10. Send `55 aa 01 6d`; observe `1a` for 15–30 s during active recording.
11. Send `55 aa 01 6e`; then `55 aa 01 04` (stop/finalize) and confirm stopped
    with `55 aa 01 0f`. Keep the recording (do not delete).

Stop / exit conditions:

- Treat any hint of `0x3c`, `0x25`/`0x27`/`0x28`, `0x34`, `0x64`, or Wi-Fi writes
  as an abort and disconnect with a logged error.
- Always send `55 aa 01 6e` before disconnecting if `0x6d` was sent, then
  disconnect.
- If `1a` yields data, stop as soon as one clean `5B 50`-framed 160-byte segment
  (or one Opus-probe-valid 80-byte frame) is captured, so the capture stays
  small.
- Do not run the debug command sheet.

## Does the probe change persistent device state?

Unknown, and it must be treated as possibly yes:

- The app models `0x6d`/`0x6e` as an open/close **runtime toggle** and always
  pairs them (including `0x6e` in `dispose`), which suggests session state
  rather than a stored setting. **INFERRED.**
- The firmware could still latch "realtime transfer enabled" until `0x6e`, until
  a timeout, or until power-off; some neighbouring commands (`0x77`/`0x78`) are
  explicitly persistent defaults, so this cannot be assumed harmless.
- Side effects if the device does start streaming: it may push live audio to the
  vendor server (privacy/network traffic) and draw extra power. Sending `0x6d`
  with Wi-Fi unconfigured may also provoke connection attempts.
- Mitigation/monitoring in L2: send `0x6e`, disconnect, reconnect on a later
  session and confirm `1a` is silent and `0x0f` is stopped; if anything looks
  latched, power-cycle the recorder before finishing. `0x3c` is never sent.
