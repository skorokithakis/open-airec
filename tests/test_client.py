import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from airec import AirecClient, Frame, ProtocolError, Recording
from airec.client import CONTROL_NOTIFY, CONTROL_WRITE, SERVICE


ROW = b"20261002023328" + bytes.fromhex("000053c0")


class FakeBleak:
    def __init__(self, device, disconnected_callback, timeout=10.0):
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.is_connected = False
        self.writes = []
        self.replies = {1: [Frame(1, b"000000000000001")],
                        14: [Frame(14, b"\x62")],
                        5: [Frame(5, ROW), Frame(6, b"")]}
        self.services = self

    def get_service(self, uuid):
        return self if uuid == SERVICE else None

    def get_characteristic(self, uuid):
        if uuid == CONTROL_WRITE:
            return SimpleNamespace(properties=["write-without-response"], max_write_without_response_size=512)
        if uuid == CONTROL_NOTIFY:
            return SimpleNamespace(properties=["notify"])

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.disconnected_callback(self)

    async def start_notify(self, char, callback):
        self.callback = callback

    async def write_gatt_char(self, uuid, data, *, response):
        assert uuid == CONTROL_WRITE and response is False
        self.writes.append(data)
        for frame in self.replies.get(data[3], []):
            wire = bytes((0xAA, 0x55, len(frame.payload) + 1, frame.command)) + frame.payload
            self.callback(None, wire[:2])
            self.callback(None, wire[2:])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        return AirecClient("fake", timeout=0.01, client_factory=FakeBleak)

    async def asyncSetUp(self):
        self.delay_patch = patch("airec.client.asyncio.sleep", return_value=None)
        self.delay_patch.start()
        self.addCleanup(self.delay_patch.stop)

    async def test_success_and_cleanup(self):
        client = self.make_client()
        async with client:
            self.assertEqual(client.mac_response, b"000000000000001")
            self.assertEqual(await client.battery(), 98)
            rows = await client.list_recordings()
            self.assertEqual(rows[0].size_bytes, 21440)
            self.assertEqual(rows[0].recorded_at.isoformat(), "2026-10-02T02:33:28")
        self.assertFalse(client._client.is_connected)
        self.assertEqual(client._client.writes, [bytes.fromhex(s) for s in ("55aa0101", "55aa010e", "55aa0105")])

    async def test_empty_catalog_requires_marker(self):
        client = self.make_client()
        client._client.replies[5] = [Frame(6, b"")]
        async with client:
            self.assertEqual(await client.list_recordings(), ())

    async def test_missing_end_is_timeout_not_partial_result(self):
        client = self.make_client()
        client._client.replies[5] = [Frame(5, ROW)]
        async with client:
            with self.assertRaisesRegex(TimeoutError, r"catalog: no reply within 0\.01 s"):
                await client.list_recordings()
            with self.assertRaises(ConnectionError):
                await client.battery()

    async def test_initialization_failure_disconnects(self):
        client = self.make_client()
        client._client.replies[1] = []
        with self.assertRaisesRegex(TimeoutError, r"identity: no reply within 0\.01 s"):
            await client.connect()
        self.assertFalse(client._client.is_connected)
        self.assertEqual(client._client.writes, [bytes.fromhex("55aa0101")])

    async def test_direct_timeout_error_passes_through_unchanged(self):
        # A TimeoutError raised in the body (deadline not expired) must keep its
        # own object/message, e.g. the live-audio silence error.
        client = self.make_client()
        sentinel = TimeoutError("live audio produced no data within the timeout")
        with self.assertRaises(TimeoutError) as caught:
            async with client._deadline(5.0, "live audio"):
                raise sentinel
        self.assertIs(caught.exception, sentinel)

    async def test_connect_timeout_is_named(self):
        class HangingBleak(FakeBleak):
            async def connect(self):
                await asyncio.Event().wait()

        client = AirecClient("fake", timeout=0.01, client_factory=HangingBleak)
        with self.assertRaisesRegex(TimeoutError, r"connect: no reply within 0\.01 s"):
            await client.connect()
        self.assertFalse(client._client.is_connected)

    async def test_no_implicit_connection(self):
        with self.assertRaises(ConnectionError):
            await self.make_client().list_recordings()

    async def test_unsolicited_frames_are_not_queued(self):
        client = self.make_client()
        async with client:
            for _ in range(5000):
                client._notification(None, bytes.fromhex("aa55033b0080"))
            self.assertTrue(client._responses.empty())
            self.assertEqual(await client.battery(), 98)

    async def test_malformed_catalog(self):
        client = self.make_client()
        client._client.replies[5] = [Frame(5, b"bad")]
        async with client:
            with self.assertRaises(ProtocolError):
                await client.list_recordings()

    async def test_disconnect_wakes_waiter(self):
        client = self.make_client()
        async with client:
            async def drop(*args, **kwargs):
                await client._client.disconnect()
            client._client.write_gatt_char = drop
            with self.assertRaises(ConnectionError):
                await client.battery()

    async def test_reconnect_after_timeout(self):
        client = self.make_client()
        async with client:
            client._client.replies[5] = []
            with self.assertRaises(TimeoutError):
                await client.list_recordings()
            await client.connect()
            self.assertEqual(await client.battery(), 98)

    async def test_serialized_queries(self):
        client = self.make_client()
        async with client:
            battery, rows = await asyncio.gather(client.battery(), client.list_recordings())
            self.assertEqual(battery, 98)
            self.assertEqual(len(rows), 1)

    async def test_cleanup_preserves_original_error(self):
        client = self.make_client()
        with self.assertLogs("airec.client", level="WARNING"):
            with self.assertRaisesRegex(ValueError, "original failure"):
                async with client:
                    async def fail():
                        raise RuntimeError("cleanup failure")
                    client._client.disconnect = fail
                    raise ValueError("original failure")

    async def test_invalid_timeout(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                AirecClient("fake", timeout=timeout, client_factory=FakeBleak)


class RecordingTests(unittest.TestCase):
    def test_invalid_timestamp(self):
        for stamp in (b"20261302023328", b"abcdefghijklm!", b"\xff" * 14):
            with self.assertRaises(ProtocolError):
                Recording.from_frame(Frame(5, stamp + b"\0" * 4))
