"""Discover AIREC recorders by their advertised Bluetooth name."""

import math
from dataclasses import dataclass

from bleak import BLEDevice, BleakScanner

# The "AIREC ..." prefix is unvalidated on hardware beyond one user report.
# Advertising may omit the primary service, so matching never uses service UUIDs.
RECORDER_NAME_PREFIX = "AIREC"


@dataclass(frozen=True, slots=True)
class Recorder:
    """One AIREC device found while scanning."""

    name: str
    address: str
    rssi: int
    device: BLEDevice


async def find_recorders(timeout: float = 10.0) -> list[Recorder]:
    """Scan for devices advertising a name starting with RECORDER_NAME_PREFIX.

    Matching is case-insensitive and prefers the advertised local name, falling
    back to the device's resolved name. The discovered BLEDevice is returned so
    a caller can connect without scanning or resolving the address again. This
    only observes advertisements and never connects to a device.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matches = []
    for device, advertisement in discovered.values():
        name = advertisement.local_name or device.name
        if name and name.upper().startswith(RECORDER_NAME_PREFIX):
            matches.append(Recorder(name=name, address=device.address,
                                    rssi=advertisement.rssi, device=device))
    return matches
