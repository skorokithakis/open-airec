import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from airec import Recorder, find_recorders


def _device(address, name=None):
    return SimpleNamespace(address=address, name=name)


def _advertisement(local_name, rssi):
    return SimpleNamespace(local_name=local_name, rssi=rssi)


class FindRecordersTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_by_case_insensitive_name_prefix(self):
        devices = {
            "a": (_device("AA:1"), _advertisement("airec one", -40)),
            "b": (_device("BB:2"), _advertisement("Other", -50)),
            "c": (_device("CC:3"), _advertisement(None, -60)),
            "d": (_device("DD:4", "AIREC cached name"), _advertisement(None, -70)),
            "e": (_device("EE:5"), _advertisement("AIREC two", -80)),
        }
        with patch("airec.scan.BleakScanner.discover", new_callable=AsyncMock,
                   return_value=devices) as discover:
            recorders = await find_recorders(3.0)
        self.assertEqual([r.name for r in recorders], ["airec one", "AIREC cached name", "AIREC two"])
        self.assertEqual([r.address for r in recorders], ["AA:1", "DD:4", "EE:5"])
        self.assertEqual([r.rssi for r in recorders], [-40, -70, -80])
        self.assertIs(recorders[0].device, devices["a"][0])
        discover.assert_awaited_once_with(timeout=3.0, return_adv=True)

    async def test_no_matches_returns_empty(self):
        with patch("airec.scan.BleakScanner.discover", new_callable=AsyncMock,
                   return_value={"b": (_device("BB:2"), _advertisement("Other", -50))}):
            self.assertEqual(await find_recorders(3.0), [])

    async def test_invalid_timeout(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                await find_recorders(timeout)
