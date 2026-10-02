import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from airec import AirecClient, Frame, ProtocolError, Recording
from airec.client import ARCHIVE_NOTIFY, LIVE_NOTIFY
from test_client import FakeBleak


ID = "20261002124720"
RECORDING = b"\x00" + ID.encode("ascii")
PAUSED = b"\x01"
STOPPED = b"\x02"
ARCHIVE_AUDIO = b"\xaa\x55opaque archive\x00\xff"

# Config 9 (SILK wideband, 16 kHz), mono; code 3 with a one-frame count byte.
PACKET = b"\x4b\x41" + b"\x00" * 78
# Config 0 is not the fixedKA80 profile, so it must break alignment/validation.
BAD_PACKET = b"\x00" * 80
END = Frame(4, ID.encode("ascii") + b"\x00\x02\x47\xc0")


def wire(frame: Frame) -> bytes:
    return bytes((0xAA, 0x55, len(frame.payload) + 1, frame.command)) + frame.payload


class LiveBleak(FakeBleak):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []
        self.status_replies = []
        self.live_callback = None
        self.live_subscribed = False
        self.stop_notify_calls = 0
        self.block_live_start = False
        self.live_started = asyncio.Event()
        self.fail_live_stop = False
        self.bulk_callback = None
        self.archive_events = []
        self.inject_live_on_download = b""

    def get_characteristic(self, uuid):
        if uuid in (LIVE_NOTIFY, ARCHIVE_NOTIFY):
            return SimpleNamespace(uuid=uuid, properties=["notify"])
        return super().get_characteristic(uuid)

    async def start_notify(self, char, callback):
        uuid = getattr(char, "uuid", None)
        if uuid == LIVE_NOTIFY:
            self.live_callback = callback
            self.live_subscribed = True
            self.live_started.set()
            if self.block_live_start:
                await asyncio.Event().wait()
            # Deliver the queued script once subscribed. The client arms its
            # audio callback before calling start_notify, so nothing is dropped.
            for event in self.events:
                if isinstance(event, bytes):
                    callback(None, event)
                elif event is None:
                    await self.disconnect()
                else:
                    self.callback(None, wire(event))
            return
        if uuid == ARCHIVE_NOTIFY:
            self.bulk_callback = callback
            return
        await super().start_notify(char, callback)

    async def stop_notify(self, char):
        if getattr(char, "uuid", None) == LIVE_NOTIFY:
            self.stop_notify_calls += 1
            if self.fail_live_stop:
                raise RuntimeError("stop_notify failed")
            self.live_subscribed = False

    async def write_gatt_char(self, uuid, data, *, response):
        if data[3] == 0x0F:
            self.writes.append(data)
            frame = self.status_replies.pop(0)
            self.callback(None, wire(frame))
            return
        await super().write_gatt_char(uuid, data, response=response)
        if data[3] != 7:
            return
        # A stale live callback (left armed by a failed unsubscribe) must not
        # contaminate the archive transfer.
        if self.inject_live_on_download and self.live_callback is not None:
            self.live_callback(None, self.inject_live_on_download)
        for event in self.archive_events:
            if isinstance(event, bytes):
                self.bulk_callback(None, event)
            else:
                self.callback(None, wire(event))


class LiveAudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delay_patch = patch("airec.client.asyncio.sleep", return_value=None)
        self.delay_patch.start()
        self.addCleanup(self.delay_patch.stop)
        self.client = AirecClient("fake", timeout=0.05, client_factory=LiveBleak)
        self.client._client.status_replies = [Frame(15, RECORDING)]
        await self.client.connect()
        self.addAsyncCleanup(self.client.disconnect)

    async def drain(self, client=None):
        return [packet async for packet in (client or self.client).live_audio()]

    async def test_streams_aligned_packets(self):
        self.client._client.events = [PACKET * 6, END]
        self.assertEqual(await self.drain(), [PACKET] * 6)
        self.assertFalse(self.client._client.live_subscribed)
        self.assertFalse(self.client._lock.locked())

    async def test_alignment_nonzero_offset_across_notification_boundaries(self):
        fake = self.client._client
        for offset in (16, 32):
            with self.subTest(offset=offset):
                stream = b"\x00" * offset + PACKET * 4
                fake.status_replies = [Frame(15, RECORDING)]
                fake.events = [stream[:7], stream[7:150], stream[150:161],
                               stream[161:], END]
                self.assertEqual(await self.drain(), [PACKET] * 4)

    async def test_bad_toc_after_alignment_raises(self):
        self.client._client.events = [PACKET * 3 + BAD_PACKET + PACKET, END]
        received = []
        with self.assertRaises(ProtocolError):
            async for packet in self.client.live_audio():
                received.append(packet)
        self.assertEqual(received, [PACKET] * 3)

    async def test_refuses_stopped_and_paused(self):
        fake = self.client._client
        for payload in (STOPPED, PAUSED):
            with self.subTest(status=payload.hex()):
                fake.status_replies = [Frame(15, payload)]
                fake.events = [PACKET * 6, END]
                with self.assertRaises(ProtocolError):
                    await self.drain()
                self.assertFalse(fake.live_subscribed)
                await self.client.connect()

    async def test_silence_with_stopped_status_ends_cleanly(self):
        fake = self.client._client
        fake.status_replies = [Frame(15, RECORDING), Frame(15, STOPPED)]
        fake.events = []
        self.assertEqual(await self.drain(), [])
        self.assertFalse(fake.live_subscribed)

    async def test_silence_with_recording_status_raises_timeout(self):
        fake = self.client._client
        fake.status_replies = [Frame(15, RECORDING), Frame(15, RECORDING)]
        fake.events = []
        with self.assertRaises(TimeoutError):
            await self.drain()

    async def test_0x04_ends_cleanly_and_drops_partial_tail(self):
        partial = b"\x4b\x41" + b"\x00" * 20
        self.client._client.events = [PACKET * 4 + partial, END]
        self.assertEqual(await self.drain(), [PACKET] * 4)

    async def test_unsolicited_0x03_ends_the_stream(self):
        self.client._client.events = [PACKET * 4, Frame(3, ID.encode("ascii"))]
        self.assertEqual(await self.drain(), [PACKET] * 4)

    async def test_queue_overflow(self):
        self.client._client.events = [PACKET] * 5000
        with self.assertRaises(ProtocolError):
            await self.drain()

    async def test_disconnect_raises(self):
        self.client._client.events = [PACKET * 6, None]
        with self.assertRaises(ConnectionError):
            await self.drain()

    async def test_early_exit_unsubscribes_and_releases_lock(self):
        fake = self.client._client
        fake.events = [PACKET * 6]
        stream = self.client.live_audio()
        self.assertEqual(await anext(stream), PACKET)
        self.assertTrue(fake.live_subscribed)
        await stream.aclose()
        self.assertFalse(fake.live_subscribed)
        self.assertEqual(fake.stop_notify_calls, 1)
        self.assertFalse(self.client._lock.locked())
        # A clean early exit leaves the connection reusable.
        fake.status_replies = [Frame(15, RECORDING)]
        fake.events = [PACKET * 6, END]
        self.assertEqual(await self.drain(), [PACKET] * 6)

    async def test_aclosing_break_unsubscribes(self):
        fake = self.client._client
        fake.events = [PACKET * 6]
        async with contextlib.aclosing(self.client.live_audio()) as stream:
            async for _packet in stream:
                break
        self.assertFalse(fake.live_subscribed)
        self.assertFalse(self.client._lock.locked())

    async def test_cancellation_unsubscribes_and_releases_lock(self):
        client = AirecClient("fake", timeout=5.0, client_factory=LiveBleak)
        fake = client._client
        fake.status_replies = [Frame(15, RECORDING)]
        await client.connect()
        self.addAsyncCleanup(client.disconnect)

        async def consume():
            async for _packet in client.live_audio():
                pass

        async def wait(seconds):
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            loop.call_later(seconds, future.set_result, None)
            await future

        task = asyncio.create_task(consume())
        for _ in range(100):
            if fake.live_subscribed:
                break
            await wait(0.001)
        self.assertTrue(fake.live_subscribed)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(fake.live_subscribed)
        self.assertFalse(client._lock.locked())

    async def test_cancellation_during_start_notify_unsubscribes(self):
        client = AirecClient("fake", timeout=5.0, client_factory=LiveBleak)
        fake = client._client
        fake.status_replies = [Frame(15, RECORDING)]
        fake.block_live_start = True  # start_notify never returns until cancelled
        await client.connect()
        self.addAsyncCleanup(client.disconnect)

        async def consume():
            async for _packet in client.live_audio():
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(fake.live_started.wait(), 1.0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The attempted subscribe is still unsubscribed and both the live flag
        # and the shared queue are reset, so the next operation sees no live bytes.
        self.assertEqual(fake.stop_notify_calls, 1)
        self.assertFalse(client._live_active)
        self.assertFalse(client._lock.locked())

    async def test_failed_unsubscribe_requires_reconnect_and_keeps_live_out(self):
        client = AirecClient("fake", timeout=0.05, client_factory=LiveBleak)
        fake = client._client
        fake.status_replies = [Frame(15, RECORDING)]
        await client.connect()
        self.addAsyncCleanup(client.disconnect)

        fake.fail_live_stop = True
        fake.events = [PACKET * 6, END]
        self.assertEqual([packet async for packet in client.live_audio()], [PACKET] * 6)
        # stop_notify failed, so the live callback may still be armed: further
        # operations must require a reconnect rather than trust the shared queue.
        self.assertFalse(client._ready)
        self.assertFalse(client._live_active)
        self.assertEqual(fake.stop_notify_calls, 1)
        recording = Recording(ID, len(ARCHIVE_AUDIO).to_bytes(4, "big"))
        with self.assertRaises(ConnectionError):
            await client.download_recording(recording)

        # After reconnecting, bytes pushed by the stale live callback during the
        # archive download are ignored; only the archive bytes are returned.
        fake.fail_live_stop = False
        fake.status_replies = [Frame(15, STOPPED)]
        fake.archive_events = [Frame(7, ID.encode() + recording.metadata), ARCHIVE_AUDIO, Frame(9, b"")]
        fake.inject_live_on_download = PACKET * 2
        await client.connect()
        self.assertEqual(await client.download_recording(recording), ARCHIVE_AUDIO)

    async def test_0x3b_counts_as_activity_for_the_silence_timer(self):
        client = AirecClient("fake", timeout=0.5, client_factory=LiveBleak)
        fake = client._client
        # Only the preflight status reply exists: a false silence would try to
        # query status again and pop from an empty list.
        fake.status_replies = [Frame(15, RECORDING)]
        await client.connect()
        self.addAsyncCleanup(client.disconnect)

        async def wait(seconds):
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            loop.call_later(seconds, future.set_result, None)
            await future

        async def consume():
            return [packet async for packet in client.live_audio()]

        task = asyncio.create_task(consume())
        for _ in range(100):
            if fake.live_subscribed:
                break
            await wait(0.001)
        self.assertTrue(fake.live_subscribed)
        loop = asyncio.get_running_loop()
        # 0x3b ticks keep resetting the 0.5 s silence window until 0x04 at 0.6 s.
        loop.call_later(0.15, fake.callback, None, wire(Frame(0x3B, b"\x00\x01")))
        loop.call_later(0.3, fake.callback, None, wire(Frame(0x3B, b"\x00\x02")))
        loop.call_later(0.6, fake.callback, None, wire(END))
        self.assertEqual(await asyncio.wait_for(task, timeout=2.0), [])

    async def test_audio_is_never_traced(self):
        traces = []
        self.client.trace = lambda direction, data: traces.append(data)
        self.client._client.events = [PACKET * 6, END]
        self.assertEqual(await self.drain(), [PACKET] * 6)
        self.assertNotIn(PACKET, traces)


if __name__ == "__main__":
    unittest.main()
