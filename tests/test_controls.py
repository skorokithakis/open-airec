import unittest
from datetime import datetime, UTC
from unittest.mock import patch

from airec import ActiveRecordingError, AirecClient, Frame, ProtocolError
from test_client import FakeBleak, ROW

ID = ROW[:14].decode()


class ControlBleak(FakeBleak):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = 2
        self.stamp = b"20261002023328"
        self.deleted = False
        self.fail = None

    async def write_gatt_char(self, uuid, data, **kwargs):
        command = data[3]
        if command == self.fail:
            self.replies[command] = []
            self.writes.append(data)
            return
        if command == 15:
            self.replies[15] = [Frame(15, bytes([self.state]) + (self.stamp if self.state == 0 else b""))]
        elif command in (3, 33):
            self.state = 0
            self.replies[command] = [Frame(command, self.stamp)]
        elif command == 16:
            self.state = 1 if self.state == 0 else 0
            self.replies[16] = [Frame(16, b"")]
        elif command == 4:
            self.state = 2
            self.replies[4] = [Frame(4, ROW)]
        elif command == 10:
            self.deleted = True
            self.replies[10] = [Frame(10, b"")]
        elif command == 5:
            self.replies[5] = ([Frame(5, ROW)] if not self.deleted else []) + [Frame(6, b"")]
        elif command == 2:
            self.stamp = data[4:]
            self.replies[2] = [Frame(2, b"")]
        elif command == 48:
            self.replies[48] = [Frame(48, self.stamp)]
        await super().write_gatt_char(uuid, data, **kwargs)


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch("airec.client.asyncio.sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = AirecClient("fake", timeout=0.05, client_factory=ControlBleak)
        await self.client.connect()
        self.addAsyncCleanup(self.client.disconnect)

    def commands(self):
        return [data[3] for data in self.client._client.writes]

    async def test_start_pause_resume_stop(self):
        self.assertEqual((await self.client.start_recording()).state, "recording")
        self.assertIn(bytes.fromhex("55aa022152"), self.client._client.writes)
        self.assertEqual((await self.client.pause_recording()).state, "paused")
        self.assertEqual((await self.client.resume_recording()).recording_id, ID)
        self.assertEqual((await self.client.stop_recording()).recording_id, ID)
        self.assertEqual((await self.client.recording_status()).state, "stopped")

    async def test_no_blind_toggles_or_duplicate_starts(self):
        await self.client.start_recording()
        await self.client.start_recording()
        await self.client.resume_recording()
        await self.client.pause_recording()
        await self.client.pause_recording()
        self.assertEqual(self.commands().count(3), 1)
        self.assertEqual(self.commands().count(16), 1)

    async def test_start_mode_hint_needs_no_ack(self):
        self.client._client.fail = 33
        self.assertEqual((await self.client.start_recording()).state, "recording")
        self.assertEqual(self.commands(), [1, 15, 3, 33, 15])

    async def test_stopped_pause_resume_rejected(self):
        for method in (self.client.pause_recording, self.client.resume_recording):
            with self.assertRaises(ProtocolError):
                await method()
            await self.client.connect()
        self.assertNotIn(16, self.commands())

    async def test_paused_start_rejected(self):
        self.client._client.state = 1
        with self.assertRaises(ActiveRecordingError):
            await self.client.start_recording()
        self.assertNotIn(3, self.commands())

    async def test_stopped_stop_is_noop(self):
        self.assertIsNone(await self.client.stop_recording())
        self.assertNotIn(4, self.commands())

    async def test_delete_checks_catalog_and_absence(self):
        await self.client.delete_recording(ID)
        self.assertIn(bytes.fromhex("55aa0f0a") + ID.encode(), self.client._client.writes)
        self.assertEqual(self.commands(), [1, 15, 5, 10, 5])

    async def test_absent_id_not_deleted(self):
        with self.assertRaises(ValueError):
            await self.client.delete_recording("20261002000000")
        self.assertNotIn(10, self.commands())

    async def test_invalid_id_sends_nothing(self):
        for value in ("../all", "20261301000000", "２０２６１００２０２３３２８"):
            with self.assertRaises(ValueError):
                await self.client.delete_recording(value)
        self.assertEqual(self.commands(), [1])

    async def test_mutations_require_stopped_state(self):
        for state in (0, 1):
            for method in (lambda: self.client.delete_recording(ID), self.client.set_clock):
                self.client._client.state = state
                with self.assertRaises(ActiveRecordingError):
                    await method()
                await self.client.connect()
        self.assertNotIn(10, self.commands())
        self.assertNotIn(2, self.commands())

    async def test_clock_write_and_readback(self):
        value = datetime(2026, 10, 2, 3, 4, 5)
        self.assertEqual(await self.client.set_clock(value), value)
        self.assertEqual(await self.client.clock(), value)
        self.assertIn(bytes.fromhex("55aa0f02") + b"20261002030405", self.client._client.writes)

    async def test_aware_clock_rejected_before_io(self):
        with self.assertRaises(ValueError):
            await self.client.set_clock(datetime.now(UTC))
        self.assertEqual(self.commands(), [1])

    async def test_timeout_never_retries_mutation(self):
        self.client._client.fail = 10
        with self.assertRaises(TimeoutError):
            await self.client.delete_recording(ID)
        self.assertEqual(self.commands().count(10), 1)
        with self.assertRaises(ConnectionError):
            await self.client.clock()

    async def test_failure_opcodes(self):
        fake = self.client._client
        original = fake.write_gatt_char
        async def reject(uuid, data, **kwargs):
            if data[3] in (10, 16):
                fake.writes.append(data)
                failure = 252 if data[3] == 10 else 255
                fake.callback(None, bytes((0xAA, 0x55, 1, failure)))
            else:
                await original(uuid, data, **kwargs)
        fake.write_gatt_char = reject
        with self.assertRaisesRegex(ProtocolError, "rejected"):
            await self.client.delete_recording(ID)
        await self.client.connect()
        fake.state = 0
        with self.assertRaisesRegex(ProtocolError, "rejected"):
            await self.client.pause_recording()

    async def test_duplicate_catalog_id_prevents_delete(self):
        fake = self.client._client
        original = fake.write_gatt_char
        async def duplicate(uuid, data, **kwargs):
            if data[3] == 5:
                fake.replies[5] = [Frame(5, ROW), Frame(5, ROW), Frame(6, b"")]
                await FakeBleak.write_gatt_char(fake, uuid, data, **kwargs)
            else:
                await original(uuid, data, **kwargs)
        fake.write_gatt_char = duplicate
        with self.assertRaises(ValueError):
            await self.client.delete_recording(ID)
        self.assertNotIn(10, self.commands())

    async def test_delete_not_reported_success_when_row_remains(self):
        fake = self.client._client
        original = fake.write_gatt_char
        async def ignore_delete(uuid, data, **kwargs):
            await original(uuid, data, **kwargs)
            if data[3] == 10:
                fake.deleted = False
        fake.write_gatt_char = ignore_delete
        with self.assertRaisesRegex(ProtocolError, "still present"):
            await self.client.delete_recording(ID)
        self.assertEqual(self.commands().count(10), 1)

    async def test_clock_mismatch_and_malformed_ack(self):
        fake = self.client._client
        original = fake.write_gatt_char
        async def wrong_clock(uuid, data, **kwargs):
            await original(uuid, data, **kwargs)
            if data[3] == 2:
                fake.stamp = b"20260101000000"
        fake.write_gatt_char = wrong_clock
        with self.assertRaisesRegex(ProtocolError, "read-back"):
            await self.client.set_clock(datetime(2026, 10, 2))
        self.assertFalse(self.client._ready)

    async def test_malformed_control_reply_invalidates_session(self):
        fake = self.client._client
        original = fake.write_gatt_char
        async def malformed(uuid, data, **kwargs):
            if data[3] == 16:
                fake.replies[16] = [Frame(16, b"bad")]
                await FakeBleak.write_gatt_char(fake, uuid, data, **kwargs)
            else:
                await original(uuid, data, **kwargs)
        fake.write_gatt_char = malformed
        fake.state = 0
        with self.assertRaisesRegex(ProtocolError, "acknowledgement"):
            await self.client.pause_recording()
        self.assertFalse(self.client._ready)

    async def test_toggle_ack_without_state_change_is_failure(self):
        fake = self.client._client
        original = fake.write_gatt_char
        async def ignore_toggle(uuid, data, **kwargs):
            if data[3] == 16:
                fake.replies[16] = [Frame(16, b"")]
                await FakeBleak.write_gatt_char(fake, uuid, data, **kwargs)
            else:
                await original(uuid, data, **kwargs)
        fake.write_gatt_char = ignore_toggle
        fake.state = 1
        with self.assertRaisesRegex(ProtocolError, "transition"):
            await self.client.resume_recording()
        self.assertEqual(self.commands().count(16), 1)

    async def test_dangerous_opcodes_stay_blocked(self):
        for command, payload in ((0x34, b""), (0x67, b"\x01"), (0x21, b"D"),
                                 (0x0A, b"all"), (0x02, b"20261301000000")):
            with self.assertRaises(ValueError):
                await self.client._write(command, payload)
        self.assertEqual(self.commands(), [1])
