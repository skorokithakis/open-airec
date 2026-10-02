# Research and hardware validation

## Provenance and method

The supplied Android app identified itself as `com.record.airec.google`, version
2.1.3 (code 223), minimum SDK 24, target SDK 36. It was Flutter/Dart 3.8.1,
ARM64 Android with compressed pointers; snapshot hash:
`830f4f59e7969c70b595182826435c19`.

These findings came from the extracted manifest/resources and native app snapshot,
not execution of the APK. The original APK signature/provenance was not verified.
[Blutter](https://github.com/worawit/blutter) produced annotated Dart assembly and
object pools. Build dependencies were isolated locally; the Android app was not
installed or executed by this research.

The experimental [PyPI airec 0.1.0 package](https://pypi.org/project/airec/0.1.0/)
provided initial framing/UUID clues and packet captures. It did not communicate
successfully with the tested recorder before the real app initialization was
recovered. Its command interpretations are not authoritative for this firmware.
The new client implements the narrow app-derived behavior in `src/airec/`;
it does not require the upstream package or Android application at runtime.

## Recovered app evidence

Paths below are relative to the app's `airec_bluetooth/xlx_link/` assembly output.
Addresses are research references for this app snapshot, not device addresses or
stable APIs. Decompilation output is no longer distributed with this repository.

| Finding | App evidence |
| --- | --- |
| Delay identity query 200 ms after connection | `bluetooth_manager.dart`, `connectSuccess` at `0x188e55c`, callback at `0x188ee5c`; duration pool object contained `0x30d40` microseconds |
| Identity initialization opcode `0x01` | `protocol/command/key_profiles.dart`, `AIREC_getMac` at `0x181b4c0` |
| Request framing and special download/delete lengths | `protocol/command/request.dart`, `pack` at `0x17a0e38` |
| Catalog timestamp and big-endian size | `protocol/command/setting/file.dart`, `GetFilesResponse.unpack` at `0x2717d20`; `byte_array.dart`, `readInt` at `0x2717f90` |
| Download ID and offset | `setting/file.dart`, `SyncFileRequest.createValue` at `0x259c314` |
| Start `0x03`, then `0x21` after 100 ms | `ble_mgr.dart`, `startRecord` at `0x1886a60` and callback at `0x1886be8`; `StartRecRequest2.createValue` chooses `R` for ordinary recording |
| Delete payload is filename only | `setting/file.dart`, `DeleteRequest.createValue` at `0x259d0f8` |
| Clock sync formats local `yyyyMMddHHmmss` | `setting/setting.dart`, `SyncTimeRequest.createValue` at `0x259dbb4`; acknowledgement handler reads clock afterward |
| Storage integer widths | `setting/manager.dart`, `StorageTotalResponse.unpack` at `0x2718218` reads short for two bytes, otherwise integer |
| Storage display units | `airec_module/my/device_manage.dart` around `0x24bff04` and `0x24bff7c` divides reported values by 1000 and labels them `G` |
| Firmware version query and UTF-8 reply | `ble_mgr.dart`, `getVersion` at `0x2249308`; `setting/manager.dart`, `VersionResponse.unpack` at `0x27185c0` |
| Chip work-mode and audio-format nibbles | `setting/manager.dart`, `ChipTypeResponse.unpack` at `0x2718914`; `workModeDesc` at `0x267912c`; `audioFormatDesc` at `0x2679058` |
| `0x26` device init parameter layout | `setting/manager.dart`, `GetDeviceInitParamResponse.unpack` at `0x2719390`; SharedPreferences closure `0x27196d0` |
| Download offset and resume | `airec_module/record/audio_sync_mgr.dart`, `start` at `0x17badbc`, offset selection around `0x17bb9d0`; append mode in `audio_file_mgr.dart` `0x17be650`; `syncFile` `0x17bce68` |
| `0x07` ack payload is ignored | `SyncFileResponse` (class 8578) has no unpack; handler branch `0x18124e4` only sets state |
| `0x08` stop semantics and `0xfd` tolerance | `ble_mgr.dart`, `pauseSyncFile` at `0x17ab258`; `EndSyncResponse` closure `0x1811c34`; `0xfd` within 3000 ms accepted at `0x181289c` |
| `0x09` accepted from 50% of expected | `SyncComplete` branch `0x1813850`, BLE check around `0x1814780`-`0x1814930` |
| App deletes recordings under 5 s | `record_home.dart`, `_initRecords` closure around `0x207a7e4`-`0x207ac88`; duration `_computeDeviceDuration` at `0x207d5ac` |
| Per-file retry after `0xfd` | `SyncFail` branch `0x1812830`; count and 2 s retry closure `0x1815288` |
| Excluded/dangerous opcodes | `ota/fireware_upgrade.dart` (`0x25`/`0x27`/`0x28`); `device_manage.dart` (`0x3c`, `0x34`); `WifiGetParamResponse.unpack` at `0x271a49c` (`0x64`) |
| App opcode-table collisions | `protocol/command/key_profiles.dart`: `0x71` is upload-all/translation right, `0x02` is clock sync/custom key |
| Device-originated frames | `key_profiles.dart` descriptions: `0x1a`/`0x1b`/`0x1c` device-button recording, `0x35`, `0x37` reminder, `0x3d` call state; `0x3b` realtime duration (BE s16). Unsolicited behaviour of `0x36`, `0x3b`, `0x3d` and one-key events is inferred |
| `0x18`/`0x19` LED/noise setters | `ble_mgr.dart`, `ledSw` at `0x212ba5c` and `noiseSw` at `0x212c0c8` convert a bool to `1`/`0` in `field_f`; request classes `LedSwRequest`/`NoiseSwRequest` (manager.dart `1053`/`1066`) have no own `createValue`; the only plain `writeByte` body is `SetDefaultMonitorRequest.createValue` at `0x259e20c`; response classes `LedSwResponse` mapped to `AIREC_ledSw` at `0x18182c4`, `NoiseSwResponse` to `AIREC_noiseSw` at `0x1818344` |
| `0x22` segment duration | `setting/manager.dart`, `DeviceRecordDurationRequest.createValue` at `0x259d2b4` writes BE `short` and clamps `<= 0` to 60; `ble_mgr.dart`, `deviceRecordDurationRequest` at `0x1850d68`; auto-sent at record start by `record_mgr.dart` `startRecordDevice` `0x1865acc` and `start` `0x1885de0` (600 = no segmentation) and on connect by `record_home.dart` `0x24950bc`; response `DeviceRecordDurationResponse` mapped at `0x1818444` |
| `0x2a` mic gain | `ble_mgr.dart`, `setRecordVolumeRequest` at `0x212381c`; UI `_showPickerNumber` confirm closure calls it with an `int.parse` result at `0x21237a8`; default SP `"recordVolumeValue" = "4"` set by `record_home.dart` `0x2495498`; `initRecordVolumeList` (`device_manage.dart` `0x24bf76c`) builds 1..N items (N is 6 or 10 depending on `RecordMgr.field_13 == 0x10`, unresolved); key description gives range 1-7; response `SetRecordVolumeResponse` mapped at `0x181894c` |
| `0x2e` power-on recording | `ble_mgr.dart`, `powerOnRecordRequest` at `0x212f204`; UI `_showPickerRecordType` calls it at `0x212f190`; `record_home.dart` default sequence forces 0 at `0x24957f4`; response `PowerOnRecordResponse` mapped at `0x1818e5c` |
| `0x39` idle shutdown | `setting/manager.dart`, `IdleShutdownDurationNewRequest.createValue` at `0x259d884` writes BE `int`; `ble_mgr.dart`, `idleShutdownDurationRequest` at `0x21245f0` sends `0x23` then `0x39` after 100 ms (525600 -> `0x23` gets 240, `0x39` keeps 525600); picker option values at `0x254d5d8`-`0x254e2f0` (30/60/120/180/240/480/525600), default 60 set at `0x24c128c`; response `IdleShutdownDurationNewResponse` mapped at `0x1818544` |
| Setter acks are unparsed | `setting_command.dart` maps each key to a `*Response` (e.g. `LedSwResponse` `0x18182c4`), but the six setter responses have no `unpack`; `SettingCommand.read` at `0x18a6e8c` dispatches `unpack`; `AIREC_CommandHelper.write` at `0x179733c` packs and awaits only the BLE write, and UI callers are fire-and-forget |
| Setter sent while starting a recording | `record_mgr.dart` `startRecordDevice` `0x1865acc` and `start` `0x1885de0` send `0x22` (value 600) during record start; `realtime_monitor_page.dart` `_buildCmdItems` exposes `0x18` on/off (`0x2248eb8`/`0x2248f04`) from the live page command sheet |

The app's command table corrects two upstream assumptions: `0x02` is **clock
synchronization**, not download selection; `0x04` is **stop/finalize recording**,
not a harmless current-file query. Do not reintroduce those older sequences.
The app labels `6a` as OTA. This client never writes it, or any characteristic
other than primary control `2a`.

The six single-value settings setters (`0x18`, `0x19`, `0x22`, `0x2a`, `0x2e`,
`0x39`) are documented in [protocol](protocol.md). The binary uses
`dedup_instructions`, so the request classes for `0x18`, `0x19`, `0x2a` and `0x2e`
have no `createValue` body of their own in the function table; the only plain
single-byte writer is shared with `SetDefaultMonitorRequest`. Their payloads were
therefore inferred. They were subsequently exercised on hardware (see the validation
log): `0x18`, `0x2a`, `0x22`, `0x2e` and `0x39` changed the expected `0x26` field,
while `0x19` did not. The app ignores their acknowledgements.

The app is deliberately lenient where this client is strict. Its `0x07` handler
never inspects the acknowledgement payload, so the app cannot tell us what the
reported size means for a nonzero offset. It accepts `0x09` once the local file
reaches 50% of the expected size and adds stall/no-convergence/timeout heuristics.
This client keeps exact byte-count and ack ID/size validation instead.

The app also performs its own device mutation: `record_home` deletes recordings
shorter than 5 s (once they are at least 10 s old and not failed or interrupted).
That cleanup is a plausible alternative explanation for short recordings vanishing,
alongside firmware-side discard; which one caused the observed unsolicited `0x0a`
is unconfirmed.

## Hardware validation on 2026-10-02

Environment: one owner's recorder, Linux/BlueZ, Python 3.12, Bleak 0.22.3,
phone app disconnected. No claim of compatibility with all AIREC models is made.

1. **Initialization, battery and catalog:** the missing `55aa0101` query unlocked
   communication. Battery returned 98%, then 97%; two initial sessions returned
   the same six catalog rows and explicit end marker. No pause, pairing, explicit
   MTU override, login or cloud request was required for listing.
2. **Audio download:** active and paused states rejected downloads (`0xfd`).
   Explicitly stopping/finalizing allowed transfer. Two raw archives contained
   21,440 and 118,720 bytes, matching catalog and acknowledgement sizes and the
   completion marker. Ogg-wrapped Opus files decoded with FFmpeg without errors;
   FFprobe reported mono, 48 kHz, durations 5.36 s and 29.68 s.
3. **Recording controls and clock:** after a restart restored communication, the
   restart's active recording was finalized, not deleted. Clock setting was
   acknowledged and read back correctly. The initial start test waited for a
   `0x21` acknowledgement that this firmware never sends; cleanup finalized and
   preserved that recording. The client now verifies active status instead.
4. **Targeted deletion:** a new test recording ran approximately six seconds,
   paused, resumed with the same ID, ran another six seconds and finalized.
   Its catalog size was 43,968 bytes. Deleting only that new ID received an empty
   `0x0a` acknowledgement, and the final catalog matched the pre-test baseline.
   A preceding shorter recording was absent after stop and emitted an unsolicited
   `0x0a` response, suggesting firmware discard of short recordings; the threshold
   is not established. The app separately deletes any recording shorter than 5 s
   (see the app-evidence table), so firmware discard and app cleanup cannot yet be
   distinguished as the cause.
5. **Storage:** after an initial timeout, traced and CLI sessions reported
   59,638 MB total, 59,616 MB free, 22 MB used. Raw responses were
   `aa55050c0000e8e0` (free) and `aa55050d0000e8f6` (total).
6. **App 2.1.3 protocol probe (read-only):** empty-payload queries returned `0x12`
   `2.0.0`, `0x29` `A3AA`, `0x20` `10` (work mode 1 `jl + ble`, audio format 0
   opus), `0x36` `00` (not charging) and a 13-byte `0x26` payload whose
   power-on-recording flag was set, matching the observed power-on recording
   behaviour. During recording the device also emitted unsolicited `0x36` and
   `0x3b` frames; the `0x3b` two-byte value is likely elapsed seconds but
   unconfirmed. Against one catalog archive, offset 800 (80-byte aligned) and offset
   801 (unaligned) each returned exactly `size - offset` bytes, byte-identical to
   the corresponding slice of the offset-0 download. The `0x07` acknowledgement
   carried the full catalog size at every offset, and each stream ended with
   `0x09`. A `0x08` stop was answered with `0x08`; no `0xfd` stop reply was seen.
   No setting, Wi-Fi, OTA, format or delete command was sent.
7. **Settings setters (ticket S2):** with the recorder stopped and the phone app
   disconnected, each app-derived single-value setter was written once, `0x26` was
   read back, and the original restored with one write (except power-on recording,
   which was left off). `0x2e` (`55 aa 02 2e 00`) turned power-on recording off and
   was left off, with no reply. `0x18` (`00`) cleared `[1]` and returned `aa 55 01 18`;
   restoring `01` returned the same frame. `0x2a` (`02`) moved mic gain `[10]`
   `01` -> `02` with no reply. `0x22` (`003d`) moved segment duration `[3:5]`
   `003c` -> `003d` and returned `aa 55 01 22`. `0x39` (`0000003c`) moved idle
   shutdown `[5:9]` `0000001e` -> `0000003c` and returned `aa 55 01 39`, with no
   `0x23` sent. `0x19` (`00`) produced no reply and no `0x26` change after a 4 s
   settle, so it is dropped pending re-investigation. The final `0x26` had power-on
   recording off and every other value equal to the baseline. Missing acks occurred
   for writes that did take effect, so a missing ack is not a failure.

Connections were closed after validation. No existing archived recording was
explicitly deleted, and no firmware-update, reset or disk-format command was sent
in the successful validation sessions. Earlier failed experiments included
querying an additional characteristic before app analysis identified its OTA
role; that obsolete probe has been removed and must not be reused.

## Retained versus removed material

The implementation, offline tests, and documented protocol findings are the
maintained project. Private local JSON/JSONL session evidence and downloaded audio
remain in ignored `artifacts/` and `recordings/`; they are not pushed to Git.
Public tests use fake transport fixtures and do not contain audio.

The original extracted APK, upstream source archive/package copy, earlier Blutter
output, app string dumps, manifest analysis ZIP, failed packet captures and build
logs were removed after documenting their findings. For the 2.1.3 investigation a
fresh APK snapshot was acquired and Blutter (with a locally built capstone) was
rebuilt under ignored `artifacts/re/`; neither the APK nor the decompilation output
is distributed. Neither is required for installation or testing. Further reverse
engineering, including any new command, requires acquiring a compatible app
snapshot again; these summaries are not a replacement for full decompiled evidence.

See [protocol](protocol.md), [technical limits](technical-limits.md),
[library](../library.md) and [limitations](../limitations.md).
