"""Losslessly wrap the verified 80-byte mono Opus archive profile in Ogg.

This does not decode, transcribe or re-encode audio. Packet timing follows RFC
6716; Ogg encapsulation follows RFC 3533 and RFC 7845. Original encoder delay is
unknown, so pre-skip is zero rather than inventing a trimming value.
"""

import os
import struct
import tempfile
from pathlib import Path


def _crc_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        crc = value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ byte) & 255]
    return crc


def _page(packet: bytes, sequence: int, samples: int, flags: int) -> bytes:
    # Every packet in this profile is smaller than 255 bytes, so one lacing
    # entry and one packet per page suffice. No continued packets are emitted.
    if len(packet) >= 255:
        raise ValueError("packet is too large for this profile")
    page = bytearray(struct.pack("<4sBBQIIIBB", b"OggS", 0, flags, samples,
                                 1, sequence, 0, 1, len(packet)) + packet)
    struct.pack_into("<I", page, 22, _ogg_crc(page))
    return bytes(page)


def _packet_samples(packet: bytes) -> int:
    toc = packet[0]
    if toc & 4:
        raise ValueError("stereo packets are not supported by this mono archive profile")
    config = toc >> 3
    if config >= 16:
        per_frame = 120 << (config & 3)
    elif config >= 12:
        per_frame = 480 << (config & 1)
    else:
        per_frame = (480, 960, 1920, 2880)[config & 3]
    code = toc & 3
    frames = 1 if code == 0 else 2 if code in (1, 2) else packet[1] & 63
    samples = per_frame * frames
    if not 0 < samples <= 5760:
        raise ValueError("invalid Opus packet duration")
    return samples


def to_ogg_opus(raw: bytes) -> bytes:
    """Return a playable .opus file from verified, fixed-size archive packets.

    Rejects empty or misaligned data and unsupported TOC values. This is not a
    general-purpose Opus validator: BLE size checks and an end marker must be
    verified separately. Other device profiles can be saved as raw .airec data.
    """
    if not raw or len(raw) % 80:
        raise ValueError("this audio profile requires nonempty 80-byte Opus packets")
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 0, 0, 0, 0)
    vendor = b"airec-client"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    output = bytearray(_page(head, 0, 0, 2) + _page(tags, 1, 0, 0))
    samples = 0
    for index, start in enumerate(range(0, len(raw), 80), 2):
        packet = raw[start:start + 80]
        samples += _packet_samples(packet)
        flags = 4 if start + 80 == len(raw) else 0
        output.extend(_page(packet, index, samples, flags))
    return bytes(output)


def save_audio(raw: bytes, destination: str | Path, *, format: str = "opus") -> Path:
    """Publish a complete file atomically, never overwriting an existing path.

    The parent directory must exist. A temporary file is written/fsynced next
    to the destination, then hard-linked into place. Unsupported filesystems
    raise an error rather than falling back to a racy overwrite.
    """
    if format not in ("opus", "raw"):
        raise ValueError("audio format must be 'opus' or 'raw'")
    destination = Path(destination)
    data = to_ogg_opus(raw) if format == "opus" else raw
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".airec-", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink()
    return destination
