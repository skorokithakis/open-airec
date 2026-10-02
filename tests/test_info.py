import json
import unittest
from unittest.mock import AsyncMock, patch

from airec import AirecClient, ChipInfo, DeviceSettings, Frame, ProtocolError
from test_client import FakeBleak
from test_cli import _FakeClient, _run


PROBE_PAYLOAD = bytes.fromhex("010101003c0000001e01010101")


def _settings(**overrides):
    values = dict(
        noise_reduction=True, led=True, segment_duration=60, idle_shutdown=30,
        usb_support=True, mic_gain=1, power_on_record=True,
        disk_format_supported=True, default_wifi_on=None, default_monitor_on=None,
        raw=PROBE_PAYLOAD,
    )
    values.update(overrides)
    return DeviceSettings(**values)


class InfoQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch("airec.client.asyncio.sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = AirecClient("fake", timeout=0.02, client_factory=FakeBleak)
        await self.client.connect()
        self.addAsyncCleanup(self.client.disconnect)

    def reply(self, command, payload):
        self.client._client.replies[command] = [Frame(command, payload)]

    async def assert_protocol_error(self, method, *args):
        with self.assertRaises(ProtocolError):
            await method(*args)
        self.assertFalse(self.client._ready)
        await self.client.connect()

    async def test_firmware_version_round_trip(self):
        self.reply(0x12, b"2.0.0")
        self.assertEqual(await self.client.firmware_version(), "2.0.0")
        self.assertEqual(self.client._client.writes[-1], bytes.fromhex("55aa0112"))

    async def test_firmware_type_round_trip(self):
        self.reply(0x29, b"A3AA")
        self.assertEqual(await self.client.firmware_type(), "A3AA")
        self.assertEqual(self.client._client.writes[-1], bytes.fromhex("55aa0129"))

    async def test_text_queries_reject_malformed_or_empty_payloads(self):
        for command, label in ((0x12, "firmware version"), (0x29, "firmware type")):
            for payload in (b"", b"\xff\xfe"):
                with self.subTest(command=command, payload=payload):
                    self.reply(command, payload)
                    method = self.client.firmware_version if command == 0x12 else self.client.firmware_type
                    await self.assert_protocol_error(method)

    async def test_chip_info_probe_values(self):
        self.reply(0x20, b"\x10")
        chip = await self.client.chip_info()
        self.assertEqual(chip, ChipInfo(work_mode=1, audio_format=0))
        self.assertEqual(chip.work_mode_name, "jl + ble")
        self.assertEqual(chip.audio_format_name, "opus")
        self.assertEqual(self.client._client.writes[-1], bytes.fromhex("55aa0120"))

    async def test_chip_info_unknown_names_are_none(self):
        self.reply(0x20, b"\x5f")
        chip = await self.client.chip_info()
        self.assertEqual((chip.work_mode, chip.audio_format), (5, 15))
        self.assertIsNone(chip.work_mode_name)
        self.assertIsNone(chip.audio_format_name)

    async def test_chip_info_rejects_wrong_length(self):
        for payload in (b"", b"\x10\x00"):
            with self.subTest(payload=payload):
                self.reply(0x20, payload)
                await self.assert_protocol_error(self.client.chip_info)

    async def test_is_charging(self):
        self.reply(0x36, b"\x01")
        self.assertTrue(await self.client.is_charging())
        self.reply(0x36, b"\x00")
        self.assertFalse(await self.client.is_charging())
        self.assertEqual(self.client._client.writes[-1], bytes.fromhex("55aa0136"))

    async def test_is_charging_rejects_wrong_length(self):
        for payload in (b"", b"\x01\x00"):
            with self.subTest(payload=payload):
                self.reply(0x36, payload)
                await self.assert_protocol_error(self.client.is_charging)

    async def test_device_settings_probe_13_bytes(self):
        self.reply(0x26, PROBE_PAYLOAD)
        settings = await self.client.device_settings()
        self.assertEqual(settings, _settings())
        self.assertEqual(settings.raw, PROBE_PAYLOAD)
        self.assertEqual(self.client._client.writes[-1], bytes.fromhex("55aa0126"))

    async def test_device_settings_u8_lengths(self):
        base = bytes.fromhex("0000ff003c1e010200")
        self.reply(0x26, base)
        nine = await self.client.device_settings()
        self.assertEqual((nine.segment_duration, nine.idle_shutdown), (0x3C, 0x1E))
        self.assertFalse(nine.noise_reduction)
        self.assertFalse(nine.led)
        self.assertTrue(nine.usb_support)
        self.assertEqual(nine.mic_gain, 2)
        self.assertFalse(nine.power_on_record)
        self.assertIsNone(nine.disk_format_supported)
        self.assertIsNone(nine.default_wifi_on)
        self.assertIsNone(nine.default_monitor_on)
        self.assertEqual(nine.raw, base)

        self.reply(0x26, base + b"\x01")
        ten = await self.client.device_settings()
        self.assertTrue(ten.disk_format_supported)
        self.assertIsNone(ten.default_wifi_on)
        self.assertEqual(ten.raw, base + b"\x01")

    async def test_device_settings_u32_lengths(self):
        base = bytes.fromhex("0100ff003c0000001e010200")
        self.reply(0x26, base)
        twelve = await self.client.device_settings()
        self.assertEqual((twelve.segment_duration, twelve.idle_shutdown), (0x3C, 0x1E))
        self.assertFalse(twelve.led)
        self.assertIsNone(twelve.disk_format_supported)
        self.assertIsNone(twelve.default_wifi_on)

        self.reply(0x26, base + b"\x01")
        thirteen = await self.client.device_settings()
        self.assertTrue(thirteen.disk_format_supported)
        self.assertIsNone(thirteen.default_wifi_on)

    async def test_device_settings_wifi_lengths_and_trailing_bytes(self):
        payload = PROBE_PAYLOAD + b"\x01\x00"
        self.reply(0x26, payload)
        fifteen = await self.client.device_settings()
        self.assertTrue(fifteen.default_wifi_on)
        self.assertFalse(fifteen.default_monitor_on)
        self.assertEqual(fifteen.raw, payload)

        self.reply(0x26, payload + b"\xab\xcd")
        sixteen = await self.client.device_settings()
        self.assertEqual(sixteen.raw, payload + b"\xab\xcd")
        self.assertEqual(sixteen.default_wifi_on, fifteen.default_wifi_on)
        self.assertEqual(sixteen.default_monitor_on, fifteen.default_monitor_on)

    async def test_device_settings_rejects_unfittable_lengths(self):
        for length in (0, 1, 5, 8, 11, 14):
            with self.subTest(length=length):
                self.reply(0x26, b"\x01" * length)
                await self.assert_protocol_error(self.client.device_settings)

    async def test_new_opcodes_allowed_but_wifi_and_format_stay_blocked(self):
        for command in (0x12, 0x20, 0x26, 0x29, 0x36):
            await self.client._write(command)
        for command in (0x64, 0x34, 0x67):
            with self.assertRaises(ValueError):
                await self.client._write(command)
        self.assertEqual([write[3] for write in self.client._client.writes],
                         [1, 0x12, 0x20, 0x26, 0x29, 0x36])


class InfoCommandTests(unittest.TestCase):
    def make_client(self, **overrides):
        methods = dict(
            firmware_version=AsyncMock(return_value="2.0.0"),
            firmware_type=AsyncMock(return_value="A3AA"),
            chip_info=AsyncMock(return_value=ChipInfo(work_mode=1, audio_format=0)),
            is_charging=AsyncMock(return_value=False),
            device_settings=AsyncMock(return_value=_settings()),
        )
        methods.update(overrides)
        return _FakeClient(**methods)

    def test_info_text(self):
        code, text, _ = _run(["info"], client=self.make_client())
        self.assertEqual(code, 0)
        self.assertEqual(text,
                         "Firmware version: 2.0.0\n"
                         "Firmware type: A3AA\n"
                         "Chip work mode: 1 (jl + ble)\n"
                         "Chip audio format: 0 (opus)\n"
                         "Charging: no\n"
                         "Device settings (raw 010101003c0000001e01010101):\n"
                         "  Noise reduction: yes\n"
                         "  LED: yes\n"
                         "  Segment duration: 60 (unit unconfirmed)\n"
                         "  Idle shutdown: 30 (unit unconfirmed)\n"
                         "  USB support: yes\n"
                         "  Mic gain: 1\n"
                         "  Power-on recording: yes\n"
                         "  Disk format supported: yes\n"
                         "  Default Wi-Fi on: unknown\n"
                         "  Default monitor on: unknown\n")

    def test_info_json(self):
        _, output, _ = _run(["--json", "info"], client=self.make_client())
        self.assertEqual(json.loads(output), {
            "firmware_version": "2.0.0",
            "firmware_type": "A3AA",
            "chip_info": {"work_mode": 1, "work_mode_name": "jl + ble",
                          "audio_format": 0, "audio_format_name": "opus"},
            "is_charging": False,
            "device_settings": {
                "noise_reduction": True, "led": True, "segment_duration": 60,
                "idle_shutdown": 30, "usb_support": True, "mic_gain": 1,
                "power_on_record": True, "disk_format_supported": True,
                "default_wifi_on": None, "default_monitor_on": None,
                "raw": "010101003c0000001e01010101",
            },
        })

    def test_info_unknown_chip_name_renders_unknown(self):
        client = self.make_client(chip_info=AsyncMock(return_value=ChipInfo(work_mode=5, audio_format=15)))
        _, text, _ = _run(["info"], client=client)
        self.assertIn("Chip work mode: 5 (unknown)", text)
        self.assertIn("Chip audio format: 15 (unknown)", text)

    def test_one_failing_query_fails_the_whole_command(self):
        client = self.make_client(chip_info=AsyncMock(side_effect=ProtocolError("bad chip reply")))
        code, output, error = _run(["info"], client=client)
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("ProtocolError", error)
        self.assertIn("bad chip reply", error)


if __name__ == "__main__":
    unittest.main()
