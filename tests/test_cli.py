import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from unittest.mock import AsyncMock, patch

from airec import (DownloadInterrupted, OggOpusWriter, ProtocolError, Recording,
                   Recorder, RecordingStatus, StorageInfo, to_ogg_opus)
from airec.__main__ import _select_device, main
from airec.report import human_size, render_text


def _recorder(name, address, rssi, device=None):
    return Recorder(name=name, address=address, rssi=rssi, device=device or object())


def _recording(recording_id="20260101120000", size_bytes=2_000_000):
    return Recording(recording_id=recording_id, metadata=size_bytes.to_bytes(4, "big"))


class _FakeClient:
    """Minimal async context manager standing in for a connected AirecClient."""

    def __init__(self, **methods):
        for name, method in methods.items():
            setattr(self, name, method)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _run(argv, *, client=None, recorders=None):
    output, error = StringIO(), StringIO()
    with ExitStack() as stack:
        stack.enter_context(patch("sys.argv", ["airec", *argv]))
        if recorders is not None:
            stack.enter_context(patch("airec.__main__.find_recorders",
                                      new=AsyncMock(return_value=recorders)))
        if client is not None:
            stack.enter_context(patch("airec.__main__._select_device",
                                      new=AsyncMock(return_value=object())))
            stack.enter_context(patch("airec.__main__.AirecClient", return_value=client))
        with redirect_stdout(output), redirect_stderr(error):
            code = main()
    return code, output.getvalue(), error.getvalue()


class SelectDeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Scanning tests must not inherit the caller's AIREC_ADDRESS; clear it and
        # fail loudly if the real (Bluetooth) direct-address lookup is ever reached.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("AIREC_ADDRESS", None)
        address_lookup = patch(
            "airec.__main__.BleakScanner.find_device_by_address",
            new=AsyncMock(side_effect=AssertionError(
                "find_device_by_address must not run without an explicit address")),
        )
        address_lookup.start()
        self.addCleanup(address_lookup.stop)

    async def test_unique_scan_match_uses_discovered_device(self):
        marker = object()
        recorder = _recorder("AIREC one", "AA:1", -40, marker)
        with patch("airec.__main__.find_recorders", new=AsyncMock(return_value=[recorder])):
            device = await _select_device(None, 5.0)
        self.assertIs(device, marker)

    async def test_zero_matches_is_an_error(self):
        with patch("airec.__main__.find_recorders", new=AsyncMock(return_value=[])):
            with self.assertRaisesRegex(ConnectionError, "no AIREC recorder"):
                await _select_device(None, 5.0)

    async def test_multiple_matches_list_names_and_addresses(self):
        recorders = [_recorder("AIREC one", "AA:1", -40), _recorder("AIREC two", "BB:2", -50)]
        with patch("airec.__main__.find_recorders", new=AsyncMock(return_value=recorders)):
            with self.assertRaises(ConnectionError) as caught:
                await _select_device(None, 5.0)
        message = str(caught.exception)
        for expected in ("AIREC one", "AA:1", "AIREC two", "BB:2"):
            self.assertIn(expected, message)

    async def test_explicit_address_skips_scanning(self):
        marker = object()
        with patch("airec.__main__.BleakScanner.find_device_by_address",
                   new=AsyncMock(return_value=marker)) as find, \
                patch("airec.__main__.find_recorders", new=AsyncMock()) as scan:
            device = await _select_device("AA:1", 5.0)
        self.assertIs(device, marker)
        find.assert_awaited_once_with("AA:1", timeout=5.0)
        scan.assert_not_awaited()

    async def test_environment_address_is_used_when_flag_absent(self):
        marker = object()
        with patch.dict(os.environ, {"AIREC_ADDRESS": "ENV:1"}), \
                patch("airec.__main__.BleakScanner.find_device_by_address",
                      new=AsyncMock(return_value=marker)) as find, \
                patch("airec.__main__.find_recorders", new=AsyncMock()) as scan:
            device = await _select_device(None, 5.0)
        self.assertIs(device, marker)
        find.assert_awaited_once_with("ENV:1", timeout=5.0)
        scan.assert_not_awaited()

    async def test_explicit_address_precedes_environment(self):
        with patch.dict(os.environ, {"AIREC_ADDRESS": "ENV:1"}), \
                patch("airec.__main__.BleakScanner.find_device_by_address",
                      new=AsyncMock(return_value=object())) as find:
            await _select_device("AA:1", 5.0)
        find.assert_awaited_once_with("AA:1", timeout=5.0)


class ScanCommandTests(unittest.TestCase):
    def test_scan_prints_matches_as_json(self):
        recorders = [_recorder("AIREC one", "AA:1", -40)]
        code, output, _ = _run(["--json", "scan"], recorders=recorders)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output),
                         {"recorders": [{"name": "AIREC one", "address": "AA:1", "rssi": -40}]})

    def test_scan_prints_matches_as_text_by_default(self):
        recorders = [_recorder("AIREC one", "AA:1", -40), _recorder("AIREC two", "BB:2", -70)]
        code, output, _ = _run(["scan"], recorders=recorders)
        self.assertEqual(code, 0)
        self.assertEqual(output, "AIREC one  AA:1  -40 dBm\nAIREC two  BB:2  -70 dBm\n")

    def test_empty_scan_is_reported_in_both_modes(self):
        text = _run(["scan"], recorders=[])[1]
        as_json = _run(["--json", "scan"], recorders=[])[1]
        self.assertEqual(text, "No AIREC recorders found.\n")
        self.assertEqual(json.loads(as_json), {"recorders": []})

    def test_ambiguous_scan_selection_exits_nonzero(self):
        recorders = [_recorder("AIREC one", "AA:1", -40), _recorder("AIREC two", "BB:2", -50)]
        with patch("airec.__main__.find_recorders", new=AsyncMock(return_value=recorders)), \
                patch.dict(os.environ, {}, clear=True), \
                patch("sys.argv", ["airec", "storage"]):
            error = StringIO()
            with redirect_stderr(error):
                code = main()
        self.assertEqual(code, 1)
        self.assertIn("AIREC one", error.getvalue())
        self.assertIn("BB:2", error.getvalue())


class OutputFormatTests(unittest.TestCase):
    def test_storage_text_and_json(self):
        client = _FakeClient(storage=AsyncMock(return_value=StorageInfo(100, 60)))
        text_code, text, _ = _run(["storage"], client=client)
        json_code, as_json, _ = _run(["--json", "storage"], client=client)
        self.assertEqual(text_code, 0)
        self.assertEqual(text, "Total: 100 MB\nFree: 60 MB\nUsed: 40 MB\n")
        self.assertEqual(json.loads(as_json), {"total_mb": 100, "free_mb": 60, "used_mb": 40})

    def test_status_text_and_json(self):
        status = RecordingStatus("recording", "20260101120000")
        client = _FakeClient(recording_status=AsyncMock(return_value=status))
        _, text, _ = _run(["status"], client=client)
        _, as_json, _ = _run(["--json", "status"], client=client)
        self.assertEqual(text, "State: recording\nRecording ID: 20260101120000\n")
        self.assertEqual(json.loads(as_json), {"state": "recording", "recording_id": "20260101120000"})

    def test_list_text_includes_battery_and_table(self):
        client = _FakeClient(battery=AsyncMock(return_value=87),
                             list_recordings=AsyncMock(return_value=(_recording(),)))
        code, text, _ = _run(["list"], client=client)
        self.assertEqual(code, 0)
        self.assertEqual(text, "Battery: 87%\n"
                               "ID              Recorded at          Size\n"
                               "20260101120000  2026-01-01 12:00:00  2.0 MB\n")

    def test_list_json_shape_is_unchanged(self):
        client = _FakeClient(battery=AsyncMock(return_value=87),
                             list_recordings=AsyncMock(return_value=(_recording(),)))
        _, as_json, _ = _run(["--json", "list"], client=client)
        self.assertEqual(json.loads(as_json), {
            "battery_percent": 87,
            "recordings": [{"recording_id": "20260101120000",
                            "recorded_at": "2026-01-01T12:00:00",
                            "size_bytes": 2_000_000}],
        })

    def test_list_with_no_recordings(self):
        client = _FakeClient(battery=AsyncMock(return_value=100),
                             list_recordings=AsyncMock(return_value=()))
        _, text, _ = _run(["list"], client=client)
        self.assertEqual(text, "Battery: 100%\nNo recordings.\n")

    def test_stop_text_and_compact_json(self):
        client = _FakeClient(stop_recording=AsyncMock(return_value=_recording()))
        _, text, _ = _run(["stop"], client=client)
        _, as_json, _ = _run(["--json", "stop"], client=client)
        self.assertEqual(text, "Finalized recording ID: 20260101120000\n")
        self.assertEqual(as_json, '{"finalized_recording_id": "20260101120000"}\n')

    def test_delete_text_and_compact_json(self):
        client = _FakeClient(delete_recording=AsyncMock(return_value=None))
        _, text, _ = _run(["delete", "20260101120000", "--yes"], client=client)
        _, as_json, _ = _run(["--json", "delete", "20260101120000", "--yes"], client=client)
        self.assertEqual(text, "Deleted recording ID: 20260101120000\n")
        self.assertEqual(as_json, '{"deleted_recording_id": "20260101120000"}\n')

    def test_clock_text(self):
        client = _FakeClient(clock=AsyncMock(return_value=datetime(2026, 1, 1, 12, 0, 0)))
        _, text, _ = _run(["clock"], client=client)
        self.assertEqual(text, "Device local time: 2026-01-01T12:00:00\n")

    def test_download_text_includes_human_and_raw_sizes(self):
        payload = {"recording_id": "20260101120000", "path": "recordings/20260101120000.opus",
                   "raw_size_bytes": 2_000_000, "file_size_bytes": 1_000_000}
        self.assertEqual(render_text("download", payload),
                         "Recording ID: 20260101120000\n"
                         "Path: recordings/20260101120000.opus\n"
                         "Raw size: 2.0 MB (2000000 bytes)\n"
                         "File size: 1.0 MB (1000000 bytes)")

    def test_human_size_uses_decimal_units(self):
        self.assertEqual([human_size(n) for n in (0, 999, 1000, 1500, 10 * 1000 * 1000)],
                         ["0 B", "999 B", "1.0 KB", "1.5 KB", "10.0 MB"])


class DownloadCommandTests(unittest.TestCase):
    """Resume-aware download behavior against a fake connected client."""

    ID = "20260101120000"

    def _help(self, *argv):
        output = StringIO()
        with patch("sys.argv", ["airec", *argv]), redirect_stdout(output):
            with self.assertRaises(SystemExit):
                main()
        return output.getvalue()

    def test_download_resumes_from_part_and_reports_json(self):
        audio = b"abcdefghij"
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "out.airec")
            part = os.path.join(directory, f"{self.ID}.part")
            with open(part, "wb") as handle:
                handle.write(audio[:4])
            client = _FakeClient(
                list_recordings=AsyncMock(return_value=(_recording(self.ID, len(audio)),)),
                download_recording=AsyncMock(return_value=audio[4:]),
            )
            code, output, error = _run(
                ["--json", "download", self.ID, "--output", destination, "--format", "raw"],
                client=client)
            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), audio)
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(client.download_recording.await_args.kwargs["offset"], 4)
        self.assertFalse(os.path.exists(part))
        payload = json.loads(output)
        self.assertEqual(payload["resumed_from"], 4)
        self.assertEqual(payload["bytes"], len(audio) - 4)
        self.assertEqual(payload["raw_size_bytes"], len(audio))

    def test_interruption_persists_part_and_asks_to_rerun(self):
        partial = b"abc"
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "out.airec")
            part = os.path.join(directory, f"{self.ID}.part")
            client = _FakeClient(
                list_recordings=AsyncMock(return_value=(_recording(self.ID, 10),)),
                download_recording=AsyncMock(side_effect=DownloadInterrupted(
                    "archive download interrupted: disconnected", partial)),
            )
            code, _, error = _run(
                ["download", self.ID, "--output", destination, "--format", "raw"],
                client=client)
            with open(part, "rb") as handle:
                self.assertEqual(handle.read(), partial)
        self.assertEqual(code, 1)
        self.assertIn("rerun the same command to resume", error)
        self.assertIn("DownloadInterrupted", error)
        self.assertFalse(os.path.exists(destination))

    def test_persistence_failure_exits_nonzero_without_resume_hint(self):
        partial = b"abc"
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "out.airec")
            client = _FakeClient(
                list_recordings=AsyncMock(return_value=(_recording(self.ID, 10),)),
                download_recording=AsyncMock(side_effect=DownloadInterrupted(
                    "archive download interrupted: disconnected", partial)),
            )
            with patch("airec.sync._append_partial", side_effect=OSError("disk full")):
                code, _, error = _run(
                    ["download", self.ID, "--output", destination, "--format", "raw"],
                    client=client)
        self.assertEqual(code, 1)
        self.assertIn("OSError", error)
        self.assertIn("disk full", error)
        self.assertNotIn("rerun the same command to resume", error)

    def test_output_cannot_be_the_reserved_scratch_path(self):
        with tempfile.TemporaryDirectory() as directory:
            reserved = os.path.join(directory, f"{self.ID}.part")
            with open(reserved, "wb") as handle:
                handle.write(b"prior partial")
            output, error = StringIO(), StringIO()
            with patch("sys.argv", ["airec", "download", self.ID, "--output", reserved,
                                    "--format", "raw"]), \
                    patch("airec.__main__._select_device", new=AsyncMock()) as select, \
                    patch("airec.__main__.find_recorders", new=AsyncMock()) as scan, \
                    redirect_stdout(output), redirect_stderr(error):
                code = main()
            self.assertEqual(code, 1)
            self.assertIn("reserved", error.getvalue())
            with open(reserved, "rb") as handle:
                self.assertEqual(handle.read(), b"prior partial")
        select.assert_not_awaited()
        scan.assert_not_awaited()

    def test_oversized_part_is_discarded_and_download_restarts(self):
        audio = b"abcdefghij"
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "out.airec")
            part = os.path.join(directory, f"{self.ID}.part")
            with open(part, "wb") as handle:
                handle.write(audio + b"extra")
            client = _FakeClient(
                list_recordings=AsyncMock(return_value=(_recording(self.ID, len(audio)),)),
                download_recording=AsyncMock(return_value=audio),
            )
            code, _, _ = _run(
                ["download", self.ID, "--output", destination, "--format", "raw"],
                client=client)
            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), audio)
        self.assertEqual(code, 0)
        self.assertEqual(client.download_recording.await_args.kwargs["offset"], 0)
        self.assertFalse(os.path.exists(part))

    def test_download_timeout_defaults_to_six_hundred(self):
        audio = b"abcd"
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "out.airec")
            client = _FakeClient(
                list_recordings=AsyncMock(return_value=(_recording(self.ID, len(audio)),)),
                download_recording=AsyncMock(return_value=audio),
            )
            code, _, _ = _run(
                ["download", self.ID, "--output", destination, "--format", "raw"],
                client=client)
        self.assertEqual(code, 0)
        self.assertEqual(client.download_recording.await_args.kwargs["timeout"], 600.0)

    def test_download_and_sync_format_help_is_present(self):
        download_help = self._help("download", "--help")
        self.assertIn("--format", download_help)
        self.assertIn("--download-timeout", download_help)
        self.assertIn("output audio format", download_help)
        sync_help = self._help("sync", "--help")
        self.assertIn("--format", sync_help)
        self.assertIn("output audio format", sync_help)


class SyncCommandTests(unittest.TestCase):
    def _sync_client(self, *, status="stopped", recordings=(), downloaded=b"\x01\x02"):
        return _FakeClient(
            recording_status=AsyncMock(return_value=RecordingStatus(status)),
            list_recordings=AsyncMock(return_value=tuple(recordings)),
            download_recording=AsyncMock(return_value=downloaded),
            delete_recording=AsyncMock(return_value=None),
        )

    def test_sync_text_prints_progress_and_summary(self):
        recording = _recording("20260101120000", 2)
        client = self._sync_client(recordings=(recording,))
        with tempfile.TemporaryDirectory() as directory:
            code, output, error = _run(["sync", directory, "--format", "raw"], client=client)
            self.assertTrue(os.path.exists(os.path.join(directory, "20260101120000.airec")))
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("Downloaded 20260101120000 (2 bytes)", output)
        self.assertIn(f"Synced to {directory}: 1 downloaded, 0 resumed, 0 skipped, 0 deleted.", output)

    def test_sync_json_shape(self):
        recording = _recording("20260101120000", 2)
        client = self._sync_client(recordings=(recording,))
        with tempfile.TemporaryDirectory() as directory:
            code, output, _ = _run(["--json", "sync", directory, "--format", "raw"], client=client)
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["directory"], directory)
        self.assertEqual(payload["format"], "raw")
        self.assertEqual(payload["events"], [{
            "action": "downloaded", "recording_id": "20260101120000",
            "bytes": 2, "resumed_from": None, "error": None,
        }])

    def test_sync_json_reports_failure_with_events_and_nonzero_exit(self):
        recordings = (_recording("20260101120000", 2), _recording("20260101130000", 2))
        client = _FakeClient(
            recording_status=AsyncMock(return_value=RecordingStatus("stopped")),
            list_recordings=AsyncMock(return_value=recordings),
            download_recording=AsyncMock(side_effect=[
                b"\x01\x02", DownloadInterrupted("simulated interruption", b"")]),
            delete_recording=AsyncMock(return_value=None),
        )
        with tempfile.TemporaryDirectory() as directory:
            code, output, _ = _run(["--json", "sync", directory, "--format", "raw"],
                                   client=client)
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual([event["action"] for event in payload["events"]],
                         ["downloaded", "failed"])
        self.assertEqual(payload["events"][1]["recording_id"], "20260101130000")
        self.assertIn("DownloadInterrupted", payload["error"])

    def test_sync_refuses_recording_without_stop_flag(self):
        recording = _recording("20260101120000", 2)
        client = self._sync_client(status="recording", recordings=(recording,))
        with tempfile.TemporaryDirectory() as directory:
            code, _, error = _run(["sync", directory, "--format", "raw"], client=client)
        self.assertEqual(code, 1)
        self.assertIn("ActiveRecordingError", error)
        client.list_recordings.assert_not_awaited()

    def test_sync_stop_flag_finalizes_first(self):
        recording = _recording("20260101120000", 2)
        client = self._sync_client(status="recording", recordings=(recording,))
        client.stop_recording = AsyncMock(return_value=recording)
        with tempfile.TemporaryDirectory() as directory:
            code, _, _ = _run(["sync", directory, "--format", "raw", "--stop-recording"], client=client)
        self.assertEqual(code, 0)
        client.stop_recording.assert_awaited_once()

    def test_sync_download_timeout_is_forwarded(self):
        recording = _recording("20260101120000", 2)
        client = self._sync_client(recordings=(recording,))
        with tempfile.TemporaryDirectory() as directory:
            code, _, _ = _run(["sync", directory, "--format", "raw",
                               "--download-timeout", "42"], client=client)
        self.assertEqual(code, 0)
        self.assertEqual(client.download_recording.await_args.kwargs["timeout"], 42.0)

    def test_sync_delete_after_requires_yes(self):
        client = self._sync_client()
        with tempfile.TemporaryDirectory() as directory:
            code, _, error = _run(["sync", directory, "--delete-after"], client=client)
        self.assertEqual(code, 1)
        self.assertIn("--yes", error)

    def test_sync_delete_after_deletes_published_recording(self):
        recording = _recording("20260101120000", 2)
        client = self._sync_client(recordings=(recording,))
        with tempfile.TemporaryDirectory() as directory:
            code, output, _ = _run(["sync", directory, "--format", "raw", "--delete-after", "--yes"],
                                   client=client)
        self.assertEqual(code, 0)
        client.delete_recording.assert_awaited_once_with("20260101120000")
        self.assertIn("Deleted 20260101120000 from recorder", output)


class ListenCommandTests(unittest.TestCase):
    """Live-audio streaming to a file or stdout against a fake live_audio()."""

    # Config 9 (SILK wideband, 16 kHz), mono, code 3 with a one-frame count byte.
    PACKET = b"\x4b\x41" + b"\x00" * 78

    def _client(self, packets):
        async def stream():
            for packet in packets:
                yield packet
        return _FakeClient(live_audio=stream)

    def _help(self, *argv):
        output = StringIO()
        with patch("sys.argv", ["airec", *argv]), redirect_stdout(output):
            with self.assertRaises(SystemExit):
                main()
        return output.getvalue()

    def test_listen_writes_playable_file_and_summary_to_stderr(self):
        packets = [self.PACKET] * 50
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "live.opus")
            code, output, error = _run(["listen", "-o", destination],
                                       client=self._client(packets))
            with open(destination, "rb") as handle:
                written = handle.read()
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertEqual(written, to_ogg_opus(self.PACKET * 50))
        self.assertIn("50 packets", error)
        self.assertIn("1.0 s", error)
        self.assertIn("stream ended", error)

    def test_listen_dash_puts_only_audio_on_stdout(self):
        stream = io.BytesIO()
        packets = [self.PACKET] * 5
        with patch("airec.__main__._binary_stdout", return_value=stream):
            code, output, error = _run(["listen", "-o", "-"], client=self._client(packets))
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertEqual(stream.getvalue(), to_ogg_opus(self.PACKET * 5))
        self.assertIn("5 packets", error)

    def test_listen_interrupt_finishes_the_file(self):
        async def interrupted():
            for packet in [self.PACKET] * 4:
                yield packet
            raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "live.opus")
            code, _, error = _run(["listen", "-o", destination],
                                  client=_FakeClient(live_audio=interrupted))
            with open(destination, "rb") as handle:
                written = handle.read()
        self.assertEqual(code, 0)
        self.assertEqual(written, to_ogg_opus(self.PACKET * 4))
        self.assertIn("interrupted", error)
        # The summary is newline-terminated, so an interactive shell's missing-
        # newline marker after Ctrl-C is the terminal's ^C echo, not our output.
        self.assertTrue(error.endswith("interrupted.\n"))

    def test_listen_error_keeps_flushed_prefix_and_exits_nonzero(self):
        async def failing():
            for packet in [self.PACKET] * 3:
                yield packet
            raise ProtocolError("stream broke")

        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "live.opus")
            code, output, error = _run(["listen", "-o", destination],
                                       client=_FakeClient(live_audio=failing))
            with open(destination, "rb") as handle:
                written = handle.read()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("ProtocolError", error)
        self.assertIn("stream broke", error)
        # No EOS page on error: the last queued packet is withheld, as documented.
        buffer = io.BytesIO()
        expected = OggOpusWriter(buffer)
        for _ in range(3):
            expected.write_packet(self.PACKET)
        self.assertEqual(written, buffer.getvalue())

    def test_listen_broken_pipe_ends_quietly(self):
        class _ClosedPipe(io.BytesIO):
            def __init__(self):
                super().__init__()
                self.flushes = 0

            def flush(self):
                self.flushes += 1
                if self.flushes > 1:
                    raise BrokenPipeError("player closed")
                super().flush()

        stream = _ClosedPipe()
        with patch("airec.__main__._binary_stdout", return_value=stream):
            code, output, error = _run(["listen", "-o", "-"],
                                       client=self._client([self.PACKET] * 5))
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("output closed", error)

    def test_listen_broken_pipe_on_the_first_write_ends_quietly(self):
        class _BrokenOnWrite(io.BytesIO):
            def write(self, data):
                raise BrokenPipeError("player closed")

        stream = _BrokenOnWrite()
        with patch("airec.__main__._binary_stdout", return_value=stream):
            code, output, error = _run(["listen", "-o", "-"],
                                       client=self._client([self.PACKET] * 5))
        # The header write happens inside the protected block, so the break is
        # handled quietly instead of escaping from the writer construction.
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("output closed", error)
        self.assertIn("0 packets", error)

    def test_listen_first_write_break_closes_the_opened_file(self):
        class _BrokenOnWrite(io.BytesIO):
            def write(self, data):
                raise BrokenPipeError("player closed")

        target = _BrokenOnWrite()
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "live.opus")
            with patch("builtins.open", return_value=target):
                code, _, error = _run(["listen", "-o", destination],
                                      client=self._client([self.PACKET] * 5))
        self.assertEqual(code, 0)
        self.assertTrue(target.closed)
        self.assertIn("output closed", error)

    def test_listen_refuses_existing_file_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "live.opus")
            with open(destination, "wb") as handle:
                handle.write(b"prior contents")
            output, error = StringIO(), StringIO()
            with patch("sys.argv", ["airec", "listen", "-o", destination]), \
                    patch("airec.__main__._select_device", new=AsyncMock()) as select, \
                    patch("airec.__main__.find_recorders", new=AsyncMock()) as scan, \
                    redirect_stdout(output), redirect_stderr(error):
                code = main()
            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), b"prior contents")
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", error.getvalue())
        select.assert_not_awaited()
        scan.assert_not_awaited()

    def test_listen_rejects_json(self):
        code, output, error = _run(["--json", "listen", "-o", "-"],
                                   client=self._client([self.PACKET]))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("--json", error)

    def test_listen_help_describes_stderr_and_exclusive_output(self):
        help_text = self._help("listen", "--help")
        self.assertIn("--output", help_text)
        self.assertIn("stderr", help_text)


class GlobalOptionPlacementTests(unittest.TestCase):
    def test_json_works_before_and_after_the_subcommand(self):
        client = _FakeClient(battery=AsyncMock(return_value=50),
                             list_recordings=AsyncMock(return_value=()))
        _, before, _ = _run(["--json", "list"], client=client)
        _, after, _ = _run(["list", "--json"], client=client)
        self.assertEqual(json.loads(before), {"battery_percent": 50, "recordings": []})
        self.assertEqual(json.loads(after), json.loads(before))

    def test_json_works_for_the_default_no_subcommand_case(self):
        client = _FakeClient(battery=AsyncMock(return_value=50),
                             list_recordings=AsyncMock(return_value=()))
        _, output, _ = _run(["--json"], client=client)
        self.assertEqual(json.loads(output), {"battery_percent": 50, "recordings": []})

    def test_timeout_works_before_and_after_the_subcommand(self):
        for argv in (["--timeout", "3", "scan"], ["scan", "--timeout", "3"]):
            with self.subTest(argv=argv):
                with patch("airec.__main__.find_recorders", new=AsyncMock(return_value=[])) as find, \
                        patch("sys.argv", ["airec", *argv]):
                    with redirect_stdout(StringIO()):
                        self.assertEqual(main(), 0)
                self.assertEqual(find.await_args.args, (3.0,))

    def test_address_works_before_and_after_the_subcommand(self):
        for argv in (["--address", "AA:1", "storage"], ["storage", "--address", "AA:1"]):
            with self.subTest(argv=argv):
                client = _FakeClient(storage=AsyncMock(return_value=StorageInfo(1, 0)))
                with patch("airec.__main__._select_device",
                           new=AsyncMock(return_value=object())) as select, \
                        patch("airec.__main__.AirecClient", return_value=client), \
                        patch("sys.argv", ["airec", *argv]):
                    with redirect_stdout(StringIO()):
                        self.assertEqual(main(), 0)
                self.assertEqual(select.await_args.args, ("AA:1", 10.0))


