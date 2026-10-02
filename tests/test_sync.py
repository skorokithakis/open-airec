import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from airec import (ActiveRecordingError, AirecClient, DownloadInterrupted, Frame, Recording,
                   RecordingStatus, SyncFailed, sync_directory)
from airec.client import ARCHIVE_NOTIFY
from test_client import FakeBleak


ID1 = "20261002010101"
ID2 = "20261002020202"
AUDIO1 = b"\xaa\x55opaque one\x00\xff"
AUDIO2 = b"second recording body"


def _recording(recording_id, data):
    return Recording(recording_id, len(data).to_bytes(4, "big"))


class FakeSyncClient:
    """Connected-client double: records calls and serves configured outcomes."""

    def __init__(self, recordings=(), *, status="stopped", audio=None, fail=None,
                 delete_fail=None):
        self._recordings = tuple(recordings)
        self._status = status
        self._audio = dict(audio or {})
        self._fail = dict(fail or {})
        self._delete_fail = set(delete_fail or ())
        self.calls = []

    async def recording_status(self):
        self.calls.append(("status",))
        return RecordingStatus(self._status)

    async def stop_recording(self):
        self.calls.append(("stop",))
        self._status = "stopped"
        return None

    async def list_recordings(self):
        self.calls.append(("list",))
        return self._recordings

    async def download_recording(self, recording, *, offset=0, timeout=120.0, **kwargs):
        self.calls.append(("download", recording.recording_id, offset, timeout))
        if recording.recording_id in self._fail:
            raise DownloadInterrupted("simulated interruption", self._fail[recording.recording_id])
        return self._audio[recording.recording_id][offset:]

    async def delete_recording(self, recording_id):
        self.calls.append(("delete", recording_id))
        if recording_id in self._delete_fail:
            raise RuntimeError("simulated delete failure")


class SyncDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def part(self, recording_id):
        return self.directory / f"{recording_id}.part"

    def output(self, recording_id, suffix=".airec"):
        return self.directory / f"{recording_id}{suffix}"

    async def test_full_download_in_catalog_order(self):
        rows = (_recording(ID1, AUDIO1), _recording(ID2, AUDIO2))
        client = FakeSyncClient(rows, audio={ID1: AUDIO1, ID2: AUDIO2})
        events = await sync_directory(client, self.directory, format="raw")
        self.assertEqual([event.action for event in events], ["downloaded", "downloaded"])
        self.assertEqual(events[0].bytes, len(AUDIO1))
        self.assertEqual(self.output(ID1).read_bytes(), AUDIO1)
        self.assertEqual(self.output(ID2).read_bytes(), AUDIO2)

    async def test_opus_format_uses_opus_extension(self):
        rows = (_recording(ID1, b"\x00" * 80),)
        client = FakeSyncClient(rows, audio={ID1: b"\x00" * 80})
        await sync_directory(client, self.directory, format="opus")
        self.assertTrue(self.output(ID1, ".opus").exists())

    async def test_download_timeout_is_forwarded(self):
        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1})
        await sync_directory(client, self.directory, format="raw", download_timeout=42.0)
        self.assertIn(("download", ID1, 0, 42.0), client.calls)

    async def test_existing_output_is_skipped(self):
        self.output(ID1).write_bytes(b"local copy")
        rows = (_recording(ID1, AUDIO1), _recording(ID2, AUDIO2))
        client = FakeSyncClient(rows, audio={ID1: AUDIO1, ID2: AUDIO2})
        events = await sync_directory(client, self.directory, format="raw")
        self.assertEqual([event.action for event in events], ["skipped", "downloaded"])
        self.assertEqual(self.output(ID1).read_bytes(), b"local copy")
        self.assertEqual([call for call in client.calls if call[0] == "download"],
                         [("download", ID2, 0, 600.0)])

    async def test_failure_stops_the_run(self):
        rows = (_recording(ID1, AUDIO1), _recording(ID2, AUDIO2))
        client = FakeSyncClient(rows, audio={ID1: AUDIO1, ID2: AUDIO2}, fail={ID1: b""})
        progress = []
        with self.assertRaises(SyncFailed) as caught:
            await sync_directory(client, self.directory, format="raw", progress=progress.append)
        self.assertEqual([event.action for event in caught.exception.events], ["failed"])
        self.assertEqual([event.action for event in progress], ["failed"])
        self.assertNotIn(("download", ID2, 0, 600.0), client.calls)
        self.assertFalse(self.output(ID1).exists())

    async def test_size_mismatch_is_not_published(self):
        rows = (_recording(ID1, AUDIO1),)
        client = FakeSyncClient(rows, audio={ID1: AUDIO1[:3]})
        with self.assertRaises(SyncFailed):
            await sync_directory(client, self.directory, format="raw")
        self.assertFalse(self.output(ID1).exists())

    async def test_interruption_persists_part_then_resumes(self):
        prefix = AUDIO1[:5]
        failing = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1}, fail={ID1: prefix})
        with self.assertRaises(SyncFailed):
            await sync_directory(failing, self.directory, format="raw")
        self.assertEqual(self.part(ID1).read_bytes(), prefix)

        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1})
        events = await sync_directory(client, self.directory, format="raw")
        self.assertEqual(events[0].action, "resumed")
        self.assertEqual(events[0].resumed_from, len(prefix))
        self.assertEqual(events[0].bytes, len(AUDIO1) - len(prefix))
        self.assertIn(("download", ID1, len(prefix), 600.0), client.calls)
        self.assertEqual(self.output(ID1).read_bytes(), AUDIO1)
        self.assertFalse(self.part(ID1).exists())

    async def test_empty_partial_creates_no_scratch_file(self):
        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1}, fail={ID1: b""})
        with self.assertRaises(SyncFailed):
            await sync_directory(client, self.directory, format="raw")
        self.assertFalse(self.part(ID1).exists())

    async def test_partial_persistence_failure_is_reported(self):
        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1}, fail={ID1: AUDIO1[:5]})
        progress = []
        with patch("airec.sync._append_partial", side_effect=OSError("disk full")):
            with self.assertRaises(SyncFailed) as caught:
                await sync_directory(client, self.directory, format="raw",
                                     progress=progress.append)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertIsInstance(caught.exception.__cause__.__context__, DownloadInterrupted)
        self.assertEqual([event.action for event in progress], ["failed"])
        self.assertIn("OSError", progress[-1].error)
        self.assertIn("disk full", str(caught.exception))

    async def test_oversized_part_is_discarded_and_restarts(self):
        self.part(ID1).write_bytes(AUDIO1 + b"extra")
        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1})
        events = await sync_directory(client, self.directory, format="raw")
        self.assertEqual(events[0].action, "downloaded")
        self.assertIn(("download", ID1, 0, 600.0), client.calls)
        self.assertEqual(self.output(ID1).read_bytes(), AUDIO1)
        self.assertFalse(self.part(ID1).exists())

    async def test_non_regular_part_is_refused(self):
        self.part(ID1).mkdir()
        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1})
        with self.assertRaises(SyncFailed) as caught:
            await sync_directory(client, self.directory, format="raw")
        self.assertIn("regular file", str(caught.exception))
        self.assertNotIn(("download", ID1, 0, 600.0), client.calls)

    async def test_symlink_part_is_refused(self):
        target = self.directory / "target.part"
        target.write_bytes(AUDIO1[:2])
        self.part(ID1).symlink_to(target)
        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1})
        with self.assertRaises(SyncFailed) as caught:
            await sync_directory(client, self.directory, format="raw")
        self.assertIn("regular file", str(caught.exception))
        self.assertNotIn(("download", ID1, 0, 600.0), client.calls)

    async def test_delete_after_deletes_preexisting_file(self):
        self.output(ID1).write_bytes(b"already local")
        rows = (_recording(ID1, AUDIO1), _recording(ID2, AUDIO2))
        client = FakeSyncClient(rows, audio={ID1: AUDIO1, ID2: AUDIO2})
        events = await sync_directory(client, self.directory, format="raw", delete_after=True)
        self.assertEqual([event.action for event in events],
                         ["skipped", "deleted", "downloaded", "deleted"])
        self.assertEqual([call for call in client.calls if call[0] == "delete"],
                         [("delete", ID1), ("delete", ID2)])

    async def test_delete_after_skips_empty_file(self):
        self.output(ID1).write_bytes(b"")
        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1})
        events = await sync_directory(client, self.directory, format="raw", delete_after=True)
        self.assertEqual([event.action for event in events], ["skipped"])
        self.assertEqual([call for call in client.calls if call[0] == "delete"], [])
        self.assertEqual(self.output(ID1).read_bytes(), b"")

    async def test_delete_after_skips_symlink(self):
        target = self.directory / "target.airec"
        target.write_bytes(AUDIO1)
        self.output(ID1).symlink_to(target)
        client = FakeSyncClient((_recording(ID1, AUDIO1),), audio={ID1: AUDIO1})
        events = await sync_directory(client, self.directory, format="raw", delete_after=True)
        self.assertEqual([event.action for event in events], ["skipped"])
        self.assertEqual([call for call in client.calls if call[0] == "delete"], [])
        self.assertTrue(self.output(ID1).is_symlink())

    async def test_delete_failure_on_skipped_file_stops_run(self):
        self.output(ID1).write_bytes(b"already local")
        rows = (_recording(ID1, AUDIO1), _recording(ID2, AUDIO2))
        client = FakeSyncClient(rows, audio={ID1: AUDIO1, ID2: AUDIO2}, delete_fail={ID1})
        progress = []
        with self.assertRaises(SyncFailed) as caught:
            await sync_directory(client, self.directory, format="raw", delete_after=True,
                                 progress=progress.append)
        self.assertEqual([event.action for event in caught.exception.events], ["skipped", "failed"])
        self.assertEqual([event.action for event in progress], ["skipped", "failed"])
        self.assertIn("deleting", str(caught.exception))
        self.assertNotIn(("download", ID2, 0, 600.0), client.calls)

    async def test_active_recording_refused_without_permission(self):
        client = FakeSyncClient((_recording(ID1, AUDIO1),), status="recording", audio={ID1: AUDIO1})
        with self.assertRaises(ActiveRecordingError):
            await sync_directory(client, self.directory, format="raw")
        self.assertNotIn(("list",), client.calls)
        self.assertNotIn(("download", ID1, 0, 600.0), client.calls)

    async def test_stop_permission_finalizes_before_catalog(self):
        client = FakeSyncClient((_recording(ID1, AUDIO1),), status="paused", audio={ID1: AUDIO1})
        events = await sync_directory(client, self.directory, format="raw", stop_recording=True)
        self.assertEqual([event.action for event in events], ["downloaded"])
        self.assertEqual([call[0] for call in client.calls[:3]], ["status", "stop", "list"])

    async def test_missing_directory_is_refused(self):
        client = FakeSyncClient()
        with self.assertRaises(NotADirectoryError):
            await sync_directory(client, self.directory / "missing", format="raw")
        self.assertEqual(client.calls, [])

    async def test_invalid_format_is_rejected(self):
        with self.assertRaises(ValueError):
            await sync_directory(FakeSyncClient(), self.directory, format="mp3")


class SyncBleak(FakeBleak):
    """Fake BLE transport that serves a multi-recording catalog over the wire."""

    def __init__(self, *args, audios, fail=None, status=b"\x02", **kwargs):
        super().__init__(*args, **kwargs)
        self.audios = dict(audios)
        self.fail = dict(fail or {})
        self.bulk_callback = None
        self.replies[5] = [Frame(5, recording_id.encode() + len(data).to_bytes(4, "big"))
                           for recording_id, data in self.audios.items()] + [Frame(6, b"")]
        self.replies[15] = [Frame(15, status)]

    def get_characteristic(self, uuid):
        if uuid == ARCHIVE_NOTIFY:
            return SimpleNamespace(uuid=uuid, properties=["notify"])
        return super().get_characteristic(uuid)

    async def start_notify(self, char, callback):
        if getattr(char, "uuid", None) == ARCHIVE_NOTIFY:
            self.bulk_callback = callback
        else:
            await super().start_notify(char, callback)

    def _control(self, frame):
        wire = bytes((0xAA, 0x55, len(frame.payload) + 1, frame.command)) + frame.payload
        self.callback(None, wire)

    async def write_gatt_char(self, uuid, data, *, response):
        await super().write_gatt_char(uuid, data, response=response)
        if data[3] != 7:
            return
        recording_id = data[4:18].decode("ascii")
        offset = int.from_bytes(data[18:22], "big")
        audio = self.audios[recording_id]
        self._control(Frame(7, recording_id.encode() + len(audio).to_bytes(4, "big")))
        remaining = audio[offset:]
        if recording_id in self.fail:
            self.bulk_callback(None, remaining[:self.fail[recording_id]])
            return
        for start in range(0, len(remaining), 7):
            self.bulk_callback(None, remaining[start:start + 7])
        self._control(Frame(9, b""))


class SyncTransportTests(unittest.IsolatedAsyncioTestCase):
    """Exercise sync through the real client against the fake BLE transport."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        delay = patch("airec.client.asyncio.sleep", return_value=None)
        delay.start()
        self.addCleanup(delay.stop)

    def make_client(self, audios):
        def factory(device, disconnected_callback, **kwargs):
            return SyncBleak(device, disconnected_callback, audios=audios, **kwargs)
        return AirecClient("fake", timeout=0.01, client_factory=factory)

    async def test_full_download_over_fake_transport(self):
        audios = {ID1: AUDIO1, ID2: AUDIO2}
        client = self.make_client(audios)
        await client.connect()
        self.addAsyncCleanup(client.disconnect)
        events = await sync_directory(client, self.directory, format="raw")
        self.assertEqual([event.action for event in events], ["downloaded", "downloaded"])
        self.assertEqual((self.directory / f"{ID1}.airec").read_bytes(), AUDIO1)
        self.assertEqual((self.directory / f"{ID2}.airec").read_bytes(), AUDIO2)

    async def test_resume_over_fake_transport_requests_offset(self):
        prefix = AUDIO1[:6]
        (self.directory / f"{ID1}.part").write_bytes(prefix)
        client = self.make_client({ID1: AUDIO1})
        await client.connect()
        self.addAsyncCleanup(client.disconnect)
        events = await sync_directory(client, self.directory, format="raw")
        self.assertEqual(events[0].action, "resumed")
        self.assertEqual((self.directory / f"{ID1}.airec").read_bytes(), AUDIO1)
        last_request = client._client.writes[-1]
        self.assertEqual(last_request[4:18], ID1.encode("ascii"))
        self.assertEqual(int.from_bytes(last_request[18:22], "big"), len(prefix))
