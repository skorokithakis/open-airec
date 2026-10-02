import asyncio
import unittest
from unittest.mock import patch

from airec_client import AirecClient, Frame, ProtocolError, StorageInfo
from test_client import FakeBleak


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch("airec_client.client.asyncio.sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = AirecClient("fake", timeout=0.02, client_factory=FakeBleak)
        await self.client.connect()
        self.addAsyncCleanup(self.client.disconnect)

    def replies(self, frames):
        self.client._client.replies[11] = frames

    async def test_both_orders_and_integer_widths(self):
        for width in (2, 4):
            frames = [Frame(12, (30000).to_bytes(width, "big")),
                      Frame(13, (32000).to_bytes(width, "big"))]
            for order in (frames, frames[::-1]):
                self.replies(order)
                result = await self.client.storage()
                self.assertEqual(result, StorageInfo(32000, 30000))
                self.assertEqual(result.used_mb, 2000)
                self.assertEqual(self.client._client.writes[-1], bytes.fromhex("55aa010b"))

    async def test_mixed_width_and_large_unsigned_value(self):
        self.replies([Frame(13, (0xFFFFFFFF).to_bytes(4, "big")), Frame(12, b"\0\0")])
        self.assertEqual(await self.client.storage(), StorageInfo(0xFFFFFFFF, 0))

    async def test_full_storage(self):
        self.replies([Frame(12, b"\0\0"), Frame(13, b"\x7d\0")])
        self.assertEqual((await self.client.storage()).used_mb, 32000)

    async def test_missing_either_reply_is_not_partial_success(self):
        for frames in ([], [Frame(12, b"\0\0")], [Frame(13, b"\x7d\0")]):
            self.replies(frames)
            with self.assertRaises(TimeoutError):
                await self.client.storage()
            with self.assertRaises(ConnectionError):
                await self.client.storage()
            await self.client.connect()

    async def test_malformed_sizes(self):
        for payload in (b"", b"\0", b"\0" * 3, b"\0" * 5):
            for command in (12, 13):
                self.replies([Frame(command, payload)])
                with self.assertRaises(ProtocolError):
                    await self.client.storage()
                self.assertFalse(self.client._ready)
                await self.client.connect()

    async def test_device_rejection_even_after_partial_response(self):
        self.replies([Frame(12, b"\0\0"), Frame(251, b"")])
        with self.assertRaisesRegex(ProtocolError, "0xfb"):
            await self.client.storage()

    async def test_impossible_values(self):
        self.replies([Frame(12, b"\0\x02"), Frame(13, b"\0\x01")])
        with self.assertRaisesRegex(ProtocolError, "exceeds"):
            await self.client.storage()

    async def test_identical_duplicate_is_tolerated(self):
        frame = Frame(12, b"\0\x01")
        self.replies([frame, frame, Frame(13, b"\0\x02")])
        self.assertEqual(await self.client.storage(), StorageInfo(2, 1))

    async def test_conflicting_duplicate_is_rejected(self):
        self.replies([Frame(12, b"\0\x01"), Frame(12, b"\0\x02"), Frame(13, b"\0\x03")])
        with self.assertRaisesRegex(ProtocolError, "conflicting"):
            await self.client.storage()

    async def test_no_implicit_connection(self):
        await self.client.disconnect()
        with self.assertRaises(ConnectionError):
            await self.client.storage()

    async def test_disconnect_wakes_query(self):
        self.replies([])
        original = self.client._client.write_gatt_char
        async def drop(*args, **kwargs):
            await original(*args, **kwargs)
            await self.client._client.disconnect()
        self.client._client.write_gatt_char = drop
        with self.assertRaises(ConnectionError):
            await self.client.storage()

    async def test_cancellation_invalidates_session(self):
        self.replies([])
        written = asyncio.Event()
        original = self.client._client.write_gatt_char
        async def notify_written(*args, **kwargs):
            await original(*args, **kwargs)
            written.set()
        self.client._client.write_gatt_char = notify_written
        task = asyncio.create_task(self.client.storage())
        await written.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.client._ready)
        self.assertEqual(self.client._expected, set())
