"""Incremental control framing validated on the primary AIREC BLE profile.

See docs/protocol.md. No BLE writes are performed by these framing helpers.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Frame:
    command: int
    payload: bytes


def encode_request(command: int, payload: bytes = b"") -> bytes:
    """Encode 55 aa <command-and-payload length> <command> <payload>."""
    if not 0 <= command <= 255:
        raise ValueError("command must fit in one byte")
    payload = bytes(payload)
    if len(payload) > 254:
        raise ValueError("payload must not exceed 254 bytes")
    return bytes((0x55, 0xAA, len(payload) + 1, command)) + payload


class FrameDecoder:
    """Incrementally decode aa55 responses, retaining incomplete fragments.

    Garbage preceding a header is discarded. An invalid zero length is skipped.
    Pending storage is bounded by the one-byte wire length. Feed only a control
    channel: live audio and bulk transfer data are not framed this way.
    """

    def __init__(self) -> None:
        self._pending = bytearray()

    def reset(self) -> None:
        """Discard incomplete data at session boundaries."""
        self._pending.clear()

    def feed(self, data: bytes) -> tuple[Frame, ...]:
        self._pending.extend(data)
        frames = []
        while True:
            start = self._pending.find(b"\xaa\x55")
            if start < 0:
                # Preserve a possible first byte of a split header.
                trailing = self._pending[-1:] == b"\xaa"
                self._pending.clear()
                if trailing:
                    self._pending.append(0xAA)
                break
            if start:
                del self._pending[:start]
            if len(self._pending) < 3:
                break
            length = self._pending[2]
            if length == 0:
                del self._pending[:2]
                continue
            end = 3 + length
            if len(self._pending) < end:
                break
            frames.append(Frame(self._pending[3], bytes(self._pending[4:end])))
            del self._pending[:end]
        return tuple(frames)
