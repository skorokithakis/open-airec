# Recording format / record type (`0x79`)

Static analysis of app 2.1.3 (`artifacts/re/out/asm/`, snapshot
`830f4f59e7969c70b595182826435c19`). No device was touched for this document;
it proposes the live probe (R2) but does not perform it. Addresses are code
addresses in that snapshot; all Dart Smi values in the asm are tagged
(`mov #0x10` is the int 8), so values below are untagged unless stated.

Status legend: **CONFIRMED** = directly readable in the binary; **INFERRED** =
one consistent reading with no observed counter-evidence; **UNKNOWN** = not
determinable from the app.

## Summary

- `0x79` is a one-byte setting write: `55 aa 02 79 <type>`. The app's picker
  offers exactly two options, **WAV = `00`** and **MP3 = `01`**.
- The write is fire-and-forget. The app registers **no response class** for
  `0x79`, does not decode or await any reply, and does not verify the change.
- The picker row is **only built when `RecordMgr.chipType == 8`** (work mode 8,
  "jl + ble + wifi station"). For the recorder used in this project
  (work mode 1, "jl + ble") **the row is never shown and the app never sends
  `0x79`**. This is the single most important gating finding.
- The choice is persisted only on the phone (`SharedPreferences`
  `"RecordTypeValue"`). It is **not** reflected in `0x26` (the app's `0x26`
  parser has no format field) and is **not** read back. The only device-side
  format read-back is the `0x20` low nibble (`audioFormat`), which the app does
  not feed back into the picker.
- No bitrate/sample-rate/quality setting applies to this device family. The
  `hsd_sdcard` "Recording bitrate" item is a different product and writes a
  local SD-card config file, not a BLE command.

## 1. Command and payload — CONFIRMED

- Key: `key_profiles.dart AIREC_SettingKey.AIREC_recordType()` at
  `0x212e8cc` (lines 2395–2421). `Key` id `121` (`0x79`), description
  `设置录音类型` ("set recording type"), name `"AIREC_recordType"`.
- Request class: `setting/manager.dart RecordTypeRequest` at class line 805;
  `RecordTypeRequest.createValue` at `0x259d400`. It allocates a `ByteArray`
  and calls `ByteArray.writeByte(field_f)` once (`0x259d448`), then logs
  `"[录音类型] >>> BLE 实际下发 0x79: type=" + type + ", 完整字节=" + bytes`
  (`0x259d46c`, `0x259d498`). So the payload is exactly one byte and the
  logged `type` is that byte.
- Sender: `ble_mgr.dart AIREC_BleMgr.setRecordType` at `0x212e824`
  (lines 2710–2765). It builds a `RecordTypeRequest`, stores the argument in
  `field_f`, sets the key to `AIREC_recordType`, calls
  `AIREC_CommandHelper.write`, and returns immediately — no `await`, no reply
  matching. It is called from exactly one place (the picker, see §4).
- Framing: `55 aa 02 79 <type>` (LEN = payload+1 = 2).

## 2. Value mapping — INFERRED (strong), probe in §8

The only caller builds a two-item picker (`_showPickerRecordType` at
`0x212e03c` and its closures):

- WAV item: text `"WAV"` (`0x212e204`), `PickerItem.field_f` stored from the
  zero register (`0x212e234`) → value `0`.
- MP3 item: text `"MP3"` (`0x212e2a4`), `PickerItem.field_f` stored from the
  immediate `2` (`0x212e2d8`, `mov x0, #2`). `PickerItem<X0>` stores the value
  in a type-parameter field, which is boxed; both `getSelectedValues`
  (`picker.dart` `0x2123488`, untag of the index at `0x2123538`) and the
  confirm closure (`device_manage.dart` `0x212e960`, untag of the selected
  value at `0x212e9f8`, `LoadInt32Instr`) treat the selection as a boxed int. A
  tagged `2` is the int `1`. → value `1`.
- Corroboration: the same state field (`field_6b`) is used as the picker's
  initial `selecteds` index (`0x212e328`–`0x212e37c`, `BoxInt64Instr(field_6b)`
  into a 2-element list) and as the value sent. Values therefore must equal
  indices `0` and `1`; a value of `2` would index past a two-item column and
  `getSelectedValues` would return an empty list.
- Confirmation path: `_showPickerRecordType`'s confirm closure at `0x212e6d0`
  reads the selected value, `setState`s it into `field_6b` (`0x212e960`),
  calls `setRecordType(field_6b)` (`0x212e7cc`), then persists
  `field_6b` to `SharedPreferences` key `"RecordTypeValue"` (`0x212e904`).
- The row's current-value text likewise treats `field_6b == 0` as WAV and any
  other value as MP3 (`_buildOtherWidget1` `0x2128de0`–`0x2128df8`).

So the app only ever emits `00` (WAV) or `01` (MP3). It never offers
opus/PCM. **The device-side meaning of `00`/`01` is not proven** — two readings
are possible and are exactly what R2 must separate:

- **H-ordinal (most likely):** `0x79` is its own two-value enum
  `{0 = wav, 1 = mp3}` (the app's picker order).
- **H-shared:** `0x79` uses the same enum as `0x20`'s low nibble
  (`0 = opus, 1 = wav, 2 = mp3, 3 = pcm`); then the app's "WAV" would send
  `0` = opus, which would be an app bug, and "MP3" would send `1` = wav.

Note the app's record-type field values elsewhere (`RecordMgr`/`RecordType`
enum) are a **third, unrelated** scale; they are not what `0x79` carries.
`RecordType` is a Dart enum of at least 6 values (one constant,
`RecordType.import`, has index 5 in `out/objs.txt`); the values the parent
ticket calls "0 opus, 2 wav, 4 mp3" are that enum, not the `0x79` payload.

## 3. Reply — CONFIRMED (none handled)

- There is no `RecordTypeResponse` class, and `setting/setting_command.dart`
  (the opcode→response registry built in `SettingCommand` at `0x18175e8`) does
  **not** register `AIREC_recordType`. An `aa 55 .. 79` frame is therefore not
  matched to a handler; the app ignores it.
- `setRecordType` does not await or inspect any reply (see §1), and the only
  caller does not either.
- Expected behaviour: same class as the other setters — a same-opcode empty ack
  may or may not arrive; a missing ack is not failure. Treat the write as
  unconfirmed until `0x20` (and a recording) is checked.

## 4. Gating by chip type / work mode — CONFIRMED

The `录音类型` row is built inside the chip-type check in
`device_manage.dart _buildOtherWidget1` (`0x2127840`):

- `0x2128da4`: `RecordMgr.field_13` loaded.
- `0x2128db0`: `cmp w1, #0x10` (`= 8`) then `0x2128db4: b.ne 0x2128ffc`
  (skip the whole block when it is not 8).
- `0x2128e1c`–`0x2128e30`: the `airec_ic_dialog_record_select` /
  `录音类型` `_buildItem`, whose onTap closure `0x212dff4` calls
  `_showPickerRecordType`. This is the only construction of the row in the app
  (one occurrence of the `录音类型` string besides the picker title).

`RecordMgr.field_13` is `chipType`: `ChipTypeResponse.unpack` (`0x2718914`)
stores the derived `chipType` into `RecordMgr.field_13` (`0x27189fc`) and
`audioFormat` into `RecordMgr.field_17` (`0x2718a70`); the chip-type getter
`workModeDesc` (`0x267912c`) labels value 8 as `"jl + ble + wifi工作站"`.
`chipType == workMode` except work mode 10 → chipType 4 and work modes 11/13 →
chipType 20.

Consequence: on the project's recorder family (work mode 1 / chipType 1, the
`0x10` seen in `protocol.md`) the row is hidden and `0x79` is never sent. The
picker belongs to the chipType-8 product. Whether the chipType-1 firmware
implements `0x79` at all is UNKNOWN and is the point of R2.

## 5. Persistence and read-back — CONFIRMED app-side

- **Persistence (phone):** on confirm, the selected value is written to
  `SharedPreferences` `"RecordTypeValue"` as an int (`0x212e93c`–`0x212e944`).
  On device-management init it is read back with `getInt` and defaulted to `0`
  (`0x24c37ac`–`0x24c37e4`); the constructor default is also `0`
  (`0x254bc80`). Value `0` displays as WAV.
- **No device read-back into the picker:** the only stores to the picker state
  `field_6b` are the picker confirm (`0x212ea08`), the prefs load
  (`0x24c37e4`), and the state reset (`0x254bc80`). Nothing reads `0x26` or
  `0x20` into it.
- **`0x20` (chip/work mode):** `ChipTypeResponse.unpack` (`0x2718914`) splits
  the one byte: `workMode = b >> 4`, `audioFormat = b & 0x0f`.
  `ChipTypeResponse.audioFormatDesc` (`0x2679058`) maps `0 opus, 1 wav,
  2 mp3, 3 pcm`; only `chipType` is saved to prefs (`"chipType"`, closure
  `0x27192e8`), `audioFormat` stays in memory (`RecordMgr.field_17`). So the
  device's current format is observable via `0x20`, but the app does not use it
  to correct the picker.
- **`0x26`:** `GetDeviceInitParamResponse.unpack` (`0x2719390`) reads,
  in order: noise byte, LED byte, **one byte read and discarded**
  (`0x2719424`), segment duration (BE s16), idle-shutdown (u8 or u32), then the
  remaining fields. There is no format/record-type field in the app's model.
  The discarded third byte is the only `0x26` byte that could conceivably carry
  a format; R2 should diff it before/after (see §8).

Net: the setting is persistent app-side only. If the device persists the
format internally, nothing in the app confirms it, and `0x26` does not expose
it to the app.

## 6. Other app behaviour tied to format — CONFIRMED (format-sensitive, but driven by `0x20` not `0x79`)

The download path/extension/offset are chosen from the **reported**
`audioFormat` (`RecordMgr.field_17`), not from `"RecordTypeValue"`:

- `AudioFileMgr.getAudioPath(uid, ext)` (`0x17beb70`) builds
  `<local>/audio/<uid>.<ext>`; `AudioFileMgr.start` (`0x17be650`) opens one
  handle per extension (`opus`, `pcm`, `mp3`, `wav`, `0x17be6a4`–`0x17be724`).
- `AudioSyncMgr.start` chooses the resume offset from `chipType` + the reported
  `audioFormat` string (see `findings.md` §4: chipType 0 uses the `.opus` file;
  chipTypes 1,2,4,6,7,8,9 use the file for the reported format; otherwise
  offset 0). A device format change therefore changes which local file is
  appended to and what extension is produced.
- The validated archive path is fixed 80-byte mono Opus. If `0x20` ever reports
  a non-opus `audioFormat`, the current client's Opus assumptions would not
  hold; this client never sets the format, so it is only exposed if a user
  changes it from the app.

The `0x79` write itself changes no other app state: the picker only sends the
byte and saves `"RecordTypeValue"`.

## 7. Bitrate / sample-rate / quality — CONFIRMED none for this family

- No `AIREC_*` key or request class exists for bitrate, sample rate or quality,
  and `key_profiles.dart` has no such key. The `0x79` request is the only
  format control.
- The only UI "bitrate" item is
  `airec_module/record/hsd_sdcard_device_manage.dart` (`ic_sdcard_device_manage_bitrate`,
  line 8776). Its picker closure (`0x223f740`) calls
  `SdcardConfigUtils.updateConfigValue("BIT RATE", value)` (`0x223f7ec`) —
  it writes a **local SD-card config file**, not a BLE frame. This is the
  `hsd_sdcard` product family referenced in the ticket, not this protocol.
- Sample-rate/quality code elsewhere (`OpusDecodeService`, `AudioRecorderService`,
  `gtcrn_denoise_engine`, TTS DAOs) is phone-side decode/playback or cloud TTS,
  not device recording control.

## 8. Proposed R2 live probe (`0x79`)

**Goal:** determine whether the work-mode-1 firmware honours `0x79`, and if so
whether a single payload moves the recording format away from Opus, then make a
single restore attempt. This section is aligned with ticket `oa-kyysf`, which the
architect runs with the user present. The client never sends `0x03`/`0x04`
(start/stop): the **user starts and stops the recording with the device button**.
The probe runs in phases because it needs the user's go-ahead and button
presses, and the one-off script must not prompt. The script lives under
`artifacts/re/` and is never committed; audio stays local.

**Risk acceptance gate — do not run the write phase without it**

- Work-mode-1 firmware may not be able to return to Opus through `0x79`. The
  app's picker only ever sends `00`/`01`, and the picker is only built when
  `RecordMgr.chipType == 8` (§2, §4), so there is no app-verified Opus payload
  for this device family.
- Under the H-shared reading (§2), `00` is Opus and `01` is WAV; under the
  H-ordinal reading, `00` is WAV, `01` is MP3, and **no known byte means Opus**.
  If a write leaves the device on a non-Opus format and the single `00` restore
  write below does not bring it back, the only known recovery is the vendor app
  on a chipType-8 device or a vendor factory/default reset, which this project
  must not perform (§ "Never expose… reset/erase").
- The probe therefore runs **only** with the user's explicit acceptance of that
  risk, on an expendable recorder, and with the user aware the device may be
  left on WAV/MP3.

**Preconditions**
- Device stopped (`0x0f`), phone app disconnected, fresh/expendable recording.
- Read `0x20` first and record work mode/chipType. Because the app gates the
  picker to chipType 8 (§4), expect a no-op on the project recorder; that is a
  useful negative result, not a failure.

**Baseline (read-only, save the originals)**
```
55 aa 01 20            -> 0x20 one byte: high nibble work mode, low nibble audioFormat
55 aa 01 26            -> 0x26 settings payload (record raw bytes)
55 aa 01 0f            -> 0x0f recording status; require stopped
```

**Single candidate write (exactly one, ≥2 s settle, then re-read)**
```
55 aa 02 79 01         -> candidate: non-Opus under both R1 readings
                          (H-ordinal MP3 / H-shared WAV); see §2
wait >= 2 s
55 aa 01 20            -> compare low nibble with baseline
55 aa 01 26            -> diff all bytes, especially byte[2] (the discarded one)
```
Only this one candidate value is sent: there is no second, unconditional
candidate. `00` is deliberately not the candidate because under H-shared it is
Opus, so it could be a no-op and teach nothing, whereas `01` is non-Opus under
either reading. (The picker's "WAV" byte is `00`; `01` is its "MP3" byte and,
under H-shared, the byte for WAV.)

**Record with the device button (client sends no `0x03`/`0x04`)**
- The user starts and stops one short recording (about 10-20 s) with the device
  button; firmware may auto-discard very short recordings.
- Fetch the catalog and download only that new recording, then inspect its size
  and header. For the same duration Opus vs WAV differ ~8x in bytes/second, so
  the catalog byte-rate is a second, authoritative signal on top of `0x20`.

**Success criteria**
- `0x20` low nibble changes after the write → `0x79` is honoured; the observed
  new format gives the mapping for the value sent.
- `0x20` unchanged, `0x26` unchanged and recording byte-rate unchanged after
  the write → `0x79` is ignored on this firmware/device (expected for work
  mode 1); the mapping stays UNKNOWN.

**Restore step — exactly one write, no exploration**
```
55 aa 02 79 00         -> the single restore attempt (only byte that can mean
                          Opus, under H-shared; §2)
wait >= 2 s
55 aa 01 20
55 aa 01 26
```
- Confirm the final `0x20`/`0x26` equal the saved originals.
- Do **not** try `02`/`03` or any other payload: those are not known Opus
  values, would change the format again, and are exactly the exploratory
  restore values this probe must avoid. If `00` does not restore the baseline
  low nibble, stop and report that `0x79` cannot restore Opus on this firmware;
  the device may need the vendor app or a factory/default reset, which this
  project must not perform. Record the exact byte sequence used so the next
  attempt is reproducible.
- Do not delete any recording unless the user approves it in the session. Do
  not leave the device recording; end any test recording and verify stopped
  status.

**Capture for the report:** every raw request/response frame (with `aa 55 ..`
replies if any), the `0x20`/`0x26` diffs, and the downloaded recording's
catalog size / duration / first bytes. State explicitly whether the candidate
changed the format and whether the single `00` write restored it, plus the
residual risk if it did not.

## 9. Status summary

| Question | Status |
| --- | --- |
| `0x79` payload is one byte, frame `55 aa 02 79 <type>` | CONFIRMED |
| App values: WAV `00`, MP3 `01` (picker ordinals) | INFERRED (strong; §2) |
| Device meaning of `00`/`01` (wav/mp3 vs shared opus/wav/mp3/pcm enum) | UNKNOWN (R2) |
| Reply: none handled, fire-and-forget | CONFIRMED |
| Row gated to `RecordMgr.chipType == 8` (not work mode 1) | CONFIRMED |
| Persisted app-side (`"RecordTypeValue"`) | CONFIRMED |
| Reflected in `0x26` | CONFIRMED no app field; byte[2] candidate, R2 diffs |
| Reflected in `0x20` | CONFIRMED (`audioFormat` low nibble; not fed back to picker) |
| Format drives download extension/offset | CONFIRMED (via `0x20`) |
| Bitrate/sample-rate/quality command for this family | CONFIRMED none (`hsd_sdcard` is local config) |
| Device persists format across power cycles | UNKNOWN (R2) |
