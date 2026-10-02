import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from airec import ActiveRecordingError, AirecClient, DownloadInterrupted, Frame, ProtocolError, Recording
from airec.client import ARCHIVE_NOTIFY
from test_client import FakeBleak


ID = "20261002023328"
AUDIO = b"\xaa\x55opaque audio\x00\xff"
RECORDING = Recording(ID, len(AUDIO).to_bytes(4, "big"))
ACK = Frame(7, ID.encode("ascii") + RECORDING.metadata)
END = Frame(9, b"")


class DownloadBleak(FakeBleak):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = [ACK, AUDIO, END]
        self.bulk_callback = None
        self.replies[5] = [Frame(5, ID.encode() + RECORDING.metadata), Frame(6, b"")]
        self.replies[15] = [Frame(15, b"\x02")]

    def get_characteristic(self, uuid):
        if uuid == ARCHIVE_NOTIFY:
            return SimpleNamespace(uuid=uuid, properties=["notify"])
        return super().get_characteristic(uuid)

    async def start_notify(self, char, callback):
        if getattr(char, "uuid", None) == ARCHIVE_NOTIFY:
            self.bulk_callback = callback
        else:
            await super().start_notify(char, callback)

    async def write_gatt_char(self, uuid, data, *, response):
        await super().write_gatt_char(uuid, data, response=response)
        if data[3] != 7:
            return
        for event in self.events:
            if isinstance(event, bytes):
                self.bulk_callback(None, event)
            elif event is None:
                await self.disconnect()
            else:
                wire = bytes((0xAA, 0x55, len(event.payload) + 1, event.command)) + event.payload
                self.callback(None, wire)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delay_patch = patch("airec.client.asyncio.sleep", return_value=None)
        self.delay_patch.start()
        self.addCleanup(self.delay_patch.stop)
        self.client = AirecClient("fake", timeout=0.01, client_factory=DownloadBleak)
        await self.client.connect()
        self.addAsyncCleanup(self.client.disconnect)

    async def test_download_exact_frame_and_repeat(self):
        for _ in range(2):
            self.assertEqual(await self.client.download_recording(RECORDING), AUDIO)
        self.assertEqual(self.client._client.writes[-1], bytes.fromhex("55aa1307") + ID.encode() + b"\0" * 4)
        self.assertTrue(self.client._ready)

    async def test_string_resolved_from_catalog(self):
        self.assertEqual(await self.client.download_recording(ID), AUDIO)

    async def test_cross_channel_ordering(self):
        for events in ([AUDIO, ACK, END], [ACK, END, AUDIO], [END, AUDIO, ACK]):
            self.client._client.events = events
            self.assertEqual(await self.client.download_recording(RECORDING), AUDIO)

    async def test_chunked_audio(self):
        self.client._client.events = [ACK, *[bytes([byte]) for byte in AUDIO], END]
        self.assertEqual(await self.client.download_recording(RECORDING), AUDIO)

    async def test_empty_file(self):
        recording = Recording(ID, b"\0" * 4)
        self.client._client.events = [Frame(7, ID.encode() + b"\0" * 4), END]
        self.assertEqual(await self.client.download_recording(recording), b"")

    async def test_interruption_carries_contiguous_partial(self):
        # Missing bytes/marker/timeout after the transfer started: the exception
        # carries exactly the received prefix, still cancelling and disconnecting.
        for events, expected_partial in (
            ([ACK], b""),
            ([ACK, AUDIO[:5]], AUDIO[:5]),
            ([ACK, AUDIO], AUDIO),
            ([ACK, END, AUDIO[:-1]], AUDIO[:-1]),
        ):
            self.client._client.events = events
            with self.assertRaises(DownloadInterrupted) as caught:
                await self.client.download_recording(RECORDING, timeout=0.01)
            self.assertEqual(caught.exception.partial, expected_partial)
            self.assertEqual(self.client._client.writes[-1], bytes.fromhex("55aa0108"))
            self.assertFalse(self.client._client.is_connected)
            await self.client.connect()

    async def test_timeout_message_names_the_download_deadline(self):
        self.client._client.events = []
        with self.assertRaisesRegex(DownloadInterrupted, r"download: no reply within 0\.01 s"):
            await self.client.download_recording(RECORDING, timeout=0.01)
        self.assertFalse(self.client._client.is_connected)

    async def test_inner_status_timeout_keeps_its_name_under_longer_overall_deadline(self):
        # The client's query timeout (0.01 s) expiring before the 5 s overall
        # download deadline must keep the inner step name, not be relabelled
        # as the outer "download" deadline.
        self.client._client.replies[15] = []
        with self.assertRaisesRegex(TimeoutError, r"^download status: no reply within 0\.01 s$"):
            await self.client.download_recording(RECORDING, timeout=5.0)
        self.assertFalse(self.client._client.is_connected)

    async def test_partial_is_empty_without_a_matching_ack(self):
        # Pre-ack bytes cannot be attributed to this archive, so they are dropped.
        # A rejection invalidates any prefix even if an ack was already seen.
        for events in ([AUDIO], [AUDIO, END], [AUDIO, Frame(0xFD, b"")], [ACK, AUDIO, Frame(0xFD, b"")]):
            self.client._client.events = events
            with self.assertRaises(DownloadInterrupted) as caught:
                await self.client.download_recording(RECORDING, timeout=0.01)
            self.assertEqual(caught.exception.partial, b"")
            await self.client.connect()

    async def test_partial_is_empty_when_ack_mismatches_after_bytes(self):
        for bad_ack in (Frame(7, b"wrong"), Frame(7, ID.encode() + (RECORDING.size_bytes - 1).to_bytes(4, "big"))):
            self.client._client.events = [AUDIO, bad_ack]
            with self.assertRaises(DownloadInterrupted) as caught:
                await self.client.download_recording(RECORDING)
            self.assertEqual(caught.exception.partial, b"")
            await self.client.connect()

    async def test_offsets_are_sent_and_stream_size_follows_offset(self):
        offset = 5
        for value in (offset, len(AUDIO) - 1):
            self.client._client.events = [ACK, AUDIO[value:], END]
            self.assertEqual(await self.client.download_recording(RECORDING, offset=value), AUDIO[value:])
            self.assertEqual(self.client._client.writes[-1],
                             bytes.fromhex("55aa1307") + ID.encode() + value.to_bytes(4, "big"))

    async def test_nonzero_offset_ack_mismatch_and_interruption(self):
        offset = 5
        remaining = len(AUDIO) - offset
        # The probe proved the ack always carries the full catalog size, so a
        # remaining-size ack must be rejected even though the stream is shorter.
        self.client._client.events = [Frame(7, ID.encode() + remaining.to_bytes(4, "big")), AUDIO[offset:], END]
        with self.assertRaisesRegex(ProtocolError, "acknowledgement"):
            await self.client.download_recording(RECORDING, offset=offset)
        await self.client.connect()
        self.client._client.events = [ACK, AUDIO[offset:offset + 3]]
        with self.assertRaises(DownloadInterrupted) as caught:
            await self.client.download_recording(RECORDING, offset=offset, timeout=0.01)
        self.assertEqual(caught.exception.partial, AUDIO[offset:offset + 3])
        await self.client.connect()

    async def test_offset_out_of_range_sends_nothing(self):
        before = len(self.client._client.writes)
        size = RECORDING.size_bytes
        for value in (-1, size, size + 1, 1.5, True):
            with self.assertRaises(ValueError):
                await self.client.download_recording(RECORDING, offset=value)
        empty = Recording(ID, b"\0" * 4)
        with self.assertRaises(ValueError):
            await self.client.download_recording(empty, offset=1)
        self.assertEqual(len(self.client._client.writes), before)

    async def test_wrong_identity_size_and_malformed_completion(self):
        for event in (Frame(7, b"wrong"), Frame(7, b"20261002023329" + RECORDING.metadata),
                      Frame(7, ID.encode() + b"\0" * 4), Frame(9, b"bad")):
            self.client._client.events = [event, AUDIO, END]
            with self.assertRaises(ProtocolError):
                await self.client.download_recording(RECORDING)
            await self.client.connect()

    async def test_excess_data_including_after_end(self):
        for events in ([ACK, AUDIO + b"x", END], [ACK, AUDIO, END, b"extra"]):
            self.client._client.events = events
            with self.assertRaises(ProtocolError):
                await self.client.download_recording(RECORDING)
            await self.client.connect()

    async def test_queue_overflow(self):
        self.client._client.events = [ACK] + [b""] * 5000 + [AUDIO, END]
        with self.assertRaises(ProtocolError):
            await self.client.download_recording(RECORDING)

    async def test_disconnect_and_cancellation(self):
        self.client._client.events = [ACK, None]
        with self.assertRaises(DownloadInterrupted) as caught:
            await self.client.download_recording(RECORDING)
        self.assertEqual(caught.exception.partial, b"")
        await self.client.connect()
        self.client._client.events = []
        task = asyncio.create_task(self.client.download_recording(RECORDING))
        # Let subscription/write run without relying on a patched sleep.
        await asyncio.get_running_loop().run_in_executor(None, lambda: None)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.client._client.is_connected)

    async def test_limits_and_invalid_ids_send_nothing(self):
        before = len(self.client._client.writes)
        for kwargs in ({"timeout": 0}, {"timeout": float("inf")}, {"max_size": 1}):
            with self.assertRaises(ValueError):
                await self.client.download_recording(RECORDING, **kwargs)
        for identity in ("../bad", "20261301000000"):
            with self.assertRaises(ValueError):
                await self.client.download_recording(identity)
        self.assertEqual(len(self.client._client.writes), before)

    async def test_audio_is_not_traced(self):
        traces = []
        self.client.trace = lambda direction, data: traces.append(data)
        await self.client.download_recording(RECORDING)
        self.assertNotIn(AUDIO, traces)

    async def test_device_rejection_is_immediate(self):
        self.client._client.events = [Frame(0xFD, b"")]
        with self.assertRaisesRegex(ProtocolError, "0xfd"):
            await self.client.download_recording(RECORDING)
        self.assertFalse(self.client._client.is_connected)

    async def test_active_or_paused_recording_requires_explicit_permission(self):
        for payload in (b"\x00" + ID.encode(), b"\x01"):
            self.client._client.replies[15] = [Frame(15, payload)]
            with self.assertRaises(ActiveRecordingError):
                await self.client.download_recording(RECORDING)
            self.assertNotIn(4, [data[3] for data in self.client._client.writes])
            self.assertNotIn(7, [data[3] for data in self.client._client.writes])
            await self.client.connect()

    async def test_stop_opt_in_confirms_stopped_status(self):
        fake = self.client._client
        fake.replies[15] = [Frame(15, b"\x00" + ID.encode())]
        fake.replies[4] = [Frame(4, ID.encode() + RECORDING.metadata)]
        original_write = fake.write_gatt_char
        async def stop_and_write(uuid, data, **kwargs):
            if data[3] == 4:
                fake.replies[15] = [Frame(15, b"\x02")]
            await original_write(uuid, data, **kwargs)
        fake.write_gatt_char = stop_and_write
        self.assertEqual(await self.client.download_recording(RECORDING, stop_if_recording=True), AUDIO)
        self.assertEqual([data[3] for data in fake.writes], [1, 15, 4, 15, 7])

    async def test_malformed_status_never_stops_or_downloads(self):
        self.client._client.replies[15] = [Frame(15, b"\x00")]
        with self.assertRaises(ProtocolError):
            await self.client.download_recording(RECORDING, stop_if_recording=True)
        self.assertEqual([data[3] for data in self.client._client.writes], [1, 15])

    async def test_linux_mtu_workaround_sends_complete_request(self):
        class Backend:
            __module__ = "bleak.backends.bluezdbus.client"
            async def _acquire_mtu(self):
                self._mtu_size = 247
        self.client._client._backend = Backend()
        self.client._control_write_limit = 20
        original_get = self.client._client.get_characteristic
        def get(uuid):
            char = original_get(uuid)
            if hasattr(char, "max_write_without_response_size"):
                char.max_write_without_response_size = 20
            return char
        self.client._client.get_characteristic = get
        self.assertEqual(await self.client.download_recording(RECORDING), AUDIO)
        self.assertEqual(len(self.client._client.writes[-1]), 22)
