import json
import unittest
from functools import partial
from unittest.mock import AsyncMock, patch

from airec import AirecClient, Frame, ProtocolError, ActiveRecordingError
from test_client import ROW, FakeBleak
from test_cli import _FakeClient, _run
from test_info import PROBE_PAYLOAD, _settings


BASELINE = bytes.fromhex("010101003c0000001e01010101")

# name, requested value, expected write frame, DeviceSettings field changed.
CASES = (
    ("led", False, bytes.fromhex("55aa021800"), "led"),
    ("power-on-record", False, bytes.fromhex("55aa022e00"), "power_on_record"),
    ("mic-gain", 5, bytes.fromhex("55aa022a05"), "mic_gain"),
    ("segment-duration", 5, bytes.fromhex("55aa03220005"), "segment_duration"),
    ("idle-shutdown", 120, bytes.fromhex("55aa053900000078"), "idle_shutdown"),
)


class SettingsBleak(FakeBleak):
    """Fake transport mirroring the S2 hardware behaviour per setter."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = 2
        self.settings = bytearray(BASELINE)
        self.echo_payload = b""

    async def write_gatt_char(self, uuid, data, **kwargs):
        command = data[3]
        if command == 15:
            # State 0 (recording) carries the active ID; 1 paused, 2 stopped.
            self.replies[15] = [
                Frame(15, bytes([self.state]) + (ROW[:14] if self.state == 0 else b""))]
        elif command == 0x26:
            self.replies[0x26] = [Frame(0x26, bytes(self.settings))]
        elif command == 0x18:
            self.settings[1] = data[4]
            self.replies[0x18] = [Frame(0x18, self.echo_payload)]  # echo, no ack needed
        elif command == 0x2E:
            self.settings[11] = data[4]
            self.replies[0x2E] = []  # no reply
        elif command == 0x2A:
            self.settings[10] = data[4]
            self.replies[0x2A] = []  # no reply
        elif command == 0x22:
            self.settings[3:5] = data[4:6]
            self.replies[0x22] = [Frame(0x22, self.echo_payload)]
        elif command == 0x39:
            self.settings[5:9] = data[4:8]
            self.replies[0x39] = [Frame(0x39, self.echo_payload)]
        await super().write_gatt_char(uuid, data, **kwargs)


class ShortIdleBleak(SettingsBleak):
    """Settings fake whose 0x26 read-back truncates to the short u8 idle layout.

    The 0x39 write still stores the requested 32-bit value in ``settings[5:9]``,
    so a client that wrongly parsed four bytes from the short payload would read
    the request back and pass; the real u8 field (payload[5]) is what matters.
    """

    def __init__(self, *args, short_length, **kwargs):
        super().__init__(*args, **kwargs)
        self.short_length = short_length

    async def write_gatt_char(self, uuid, data, **kwargs):
        if data[3] == 0x26:
            self.replies[0x26] = [Frame(0x26, bytes(self.settings[:self.short_length]))]
            await FakeBleak.write_gatt_char(self, uuid, data, **kwargs)
        else:
            await super().write_gatt_char(uuid, data, **kwargs)


class SetSettingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch("airec.client.asyncio.sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = AirecClient("fake", timeout=0.05, client_factory=SettingsBleak)
        await self.client.connect()
        self.addAsyncCleanup(self.client.disconnect)

    def commands(self):
        return [data[3] for data in self.client._client.writes]

    async def test_each_setter_writes_once_and_verifies_readback(self):
        for index, (name, value, frame, field) in enumerate(CASES):
            with self.subTest(name=name):
                settings = await self.client.set_setting(name, value)
                self.assertEqual(getattr(settings, field), value)
                self.assertIn(frame, self.client._client.writes)
                self.assertEqual(self.commands().count(frame[3]), 1)
                self.assertEqual(self.commands().count(0x26), index + 1)

    async def test_missing_ack_still_succeeds_via_readback(self):
        # 0x2a and 0x2e never reply on the tested firmware.
        settings = await self.client.set_setting("mic-gain", 3)
        self.assertEqual(settings.mic_gain, 3)
        self.assertEqual(self.commands().count(0x2A), 1)
        self.assertTrue(self.client._ready)

    async def test_echo_frame_does_not_break_readback(self):
        # 0x18/0x22/0x39 echo an empty same-opcode frame before the 0x26 read.
        settings = await self.client.set_setting("led", False)
        self.assertFalse(settings.led)
        self.assertEqual(self.commands().count(0x18), 1)
        self.assertEqual(self.commands().count(0x26), 1)

    async def test_nonempty_echo_payload_is_ignored(self):
        self.client._client.echo_payload = b"\xaa\xbb"
        settings = await self.client.set_setting("led", False)
        self.assertFalse(settings.led)
        self.assertEqual(self.commands().count(0x26), 1)

    async def test_idle_shutdown_max_value_via_u32_readback(self):
        settings = await self.client.set_setting("idle-shutdown", 525600)
        self.assertEqual(settings.idle_shutdown, 525600)
        self.assertEqual(len(settings.raw), 13)
        self.assertIn(bytes.fromhex("55aa053900080520"), self.client._client.writes)
        self.assertEqual(self.commands().count(0x39), 1)
        self.assertEqual(self.commands().count(0x26), 1)

    async def test_readback_mismatch_raises_and_does_not_retry(self):
        fake = self.client._client
        original = fake.write_gatt_char

        async def ignore_write(uuid, data, **kwargs):
            if data[3] == 0x2A:
                fake.writes.append(data)  # record the single attempt, change nothing
                fake.replies[0x2A] = []
                return
            await original(uuid, data, **kwargs)

        fake.write_gatt_char = ignore_write
        with self.assertRaisesRegex(ProtocolError, "did not apply"):
            await self.client.set_setting("mic-gain", 5)
        self.assertEqual(self.commands().count(0x2A), 1)
        self.assertFalse(self.client._ready)

    async def test_only_target_field_changes(self):
        settings = await self.client.set_setting("segment-duration", 5)
        self.assertEqual(settings.segment_duration, 5)
        self.assertTrue(settings.noise_reduction)
        self.assertEqual(settings.mic_gain, 1)
        self.assertEqual(settings.idle_shutdown, 30)
        self.assertTrue(settings.power_on_record)

    async def test_setting_requires_stopped_state(self):
        for state in (0, 1):
            self.client._client.state = state
            with self.assertRaises(ActiveRecordingError):
                await self.client.set_setting("led", False)
            await self.client.connect()
        for command in (0x18, 0x22, 0x2A, 0x2E, 0x39):
            self.assertNotIn(command, self.commands())

    async def test_invalid_name_and_value_rejected_before_io(self):
        bad = (
            ("noise-reduction", False),  # dropped after the S2 probe
            ("settings", False),
            ("led", 1), ("led", "on"), ("led", None),
            ("power-on-record", 0),
            ("mic-gain", 0), ("mic-gain", 8), ("mic-gain", "4"), ("mic-gain", True),
            ("segment-duration", 0), ("segment-duration", 601), ("segment-duration", True),
            ("idle-shutdown", 0), ("idle-shutdown", 525601), ("idle-shutdown", 2.0),
        )
        for name, value in bad:
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    await self.client.set_setting(name, value)
        self.assertEqual(self.commands(), [1])

    async def test_unshipped_setter_opcodes_stay_blocked(self):
        for command, payload in ((0x19, b"\x00"), (0x23, b"\x01"),
                                 (0x24, b""), (0x2B, b"\x00"), (0x3A, b"\x00")):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    await self.client._write(command, payload)

    async def test_write_rejects_out_of_range_setter_payload(self):
        for command, payload in ((0x18, b"\x02"), (0x2A, b"\x00"), (0x2A, b"\x08"),
                                 (0x22, b"\x00\x00"), (0x39, b"\xff\xff\xff\xff")):
            with self.subTest(command=command, payload=payload):
                with self.assertRaises(ValueError):
                    await self.client._write(command, payload)


class IdleShutdownShortLayoutTests(unittest.IsolatedAsyncioTestCase):
    """A short (u8) 0x26 layout must be read as u8, so a 4-byte request mismatches."""

    async def asyncSetUp(self):
        patcher = patch("airec.client.asyncio.sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_short_u8_readback_mismatch_raises_without_retry(self):
        for length in (9, 10):
            with self.subTest(length=length):
                client = AirecClient(
                    "fake", timeout=0.05,
                    client_factory=partial(ShortIdleBleak, short_length=length))
                await client.connect()
                self.addAsyncCleanup(client.disconnect)
                with self.assertRaisesRegex(ProtocolError, "did not apply"):
                    await client.set_setting("idle-shutdown", 525600)
                commands = [data[3] for data in client._client.writes]
                self.assertEqual(commands.count(0x39), 1)
                self.assertEqual(commands.count(0x26), 1)
                self.assertFalse(client._ready)


class SetSettingCommandTests(unittest.TestCase):
    def test_text_prints_result_and_settings(self):
        client = _FakeClient(set_setting=AsyncMock(return_value=_settings(led=False)))
        code, text, error = _run(["set-setting", "led", "off"], client=client)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(text,
                         "Set led to off.\n"
                         "Device settings (raw 010101003c0000001e01010101):\n"
                         "  Noise reduction: yes\n"
                         "  LED: no\n"
                         "  Segment duration: 60 (unit unconfirmed)\n"
                         "  Idle shutdown: 30 (unit unconfirmed)\n"
                         "  USB support: yes\n"
                         "  Mic gain: 1\n"
                         "  Power-on recording: yes\n"
                         "  Disk format supported: yes\n"
                         "  Default Wi-Fi on: unknown\n"
                         "  Default monitor on: unknown\n")
        client.set_setting.assert_awaited_once_with("led", False)

    def test_json_is_compact_and_prints_snapshot(self):
        client = _FakeClient(set_setting=AsyncMock(return_value=_settings(mic_gain=5)))
        code, output, error = _run(["--json", "set-setting", "mic-gain", "5"], client=client)
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("\n  ", output)
        self.assertEqual(json.loads(output), {
            "setting": "mic-gain", "value": 5,
            "device_settings": {
                "noise_reduction": True, "led": True, "segment_duration": 60,
                "idle_shutdown": 30, "usb_support": True, "mic_gain": 5,
                "power_on_record": True, "disk_format_supported": True,
                "default_wifi_on": None, "default_monitor_on": None,
                "raw": PROBE_PAYLOAD.hex(),
            },
        })
        client.set_setting.assert_awaited_once_with("mic-gain", 5)

    def test_invalid_name_or_value_never_connects(self):
        for argv in (["set-setting", "led", "yes"], ["set-setting", "mic-gain", "8"],
                     ["set-setting", "mic-gain", "abc"], ["set-setting", "bogus", "1"]):
            with self.subTest(argv=argv):
                client = _FakeClient(set_setting=AsyncMock())
                code, output, error = _run(argv, client=client)
                self.assertEqual((code, output), (1, ""))
                self.assertIn("ValueError", error)
                client.set_setting.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
