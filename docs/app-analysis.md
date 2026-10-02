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
The new client implements the narrow app-derived behavior in `src/airec_client/`;
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

The app's command table corrects two upstream assumptions: `0x02` is **clock
synchronization**, not download selection; `0x04` is **stop/finalize recording**,
not a harmless current-file query. Do not reintroduce those older sequences.
The app labels `6a` as OTA. This client never writes it, or any characteristic
other than primary control `2a`.

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
   is not established.
5. **Storage:** after an initial timeout, traced and CLI sessions reported
   59,638 MB total, 59,616 MB free, 22 MB used. Raw responses were
   `aa55050c0000e8e0` (free) and `aa55050d0000e8f6` (total).

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

The extracted APK, upstream source archive/package copy, Blutter output, app
string dumps, manifest analysis ZIP, failed packet captures, build logs and
one-off hardware probes were removed after documenting their findings. They are
not required for installation or testing. Further reverse engineering will
require acquiring a compatible app snapshot again; this summary is not a
replacement for full decompiled evidence when investigating new commands.

See [protocol](protocol.md), [API](api.md), and [limitations](limitations.md).
