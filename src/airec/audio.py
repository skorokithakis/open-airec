"""Losslessly wrap the verified 80-byte mono Opus archive profile in Ogg.

This does not decode, transcribe or re-encode audio. Packet timing follows RFC
6716; Ogg encapsulation follows RFC 3533 and RFC 7845. Original encoder delay is
unknown, so pre-skip is zero rather than inventing a trimming value.
"""

import io
import os
import struct
import tempfile
from pathlib import Path


_OPUS_HEAD = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 0, 0, 0, 0)
_OPUS_VENDOR = b"airec"
_OPUS_TAGS = (b"OpusTags" + struct.pack("<I", len(_OPUS_VENDOR)) + _OPUS_VENDOR
              + struct.pack("<I", 0))


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

    Rejects empty data, inputs shorter than one full packet, unsupported TOC
    values and invalid trailing remainders. This is not a general-purpose Opus
    validator: BLE size checks and an end marker must be verified separately.
    Other device profiles can be saved as raw .airec data.
    """
    if len(raw) < 80:
        raise ValueError("this audio profile requires at least one 80-byte Opus packet")
    # The firmware sometimes finalizes an archive with a truncated last 80-byte
    # slot (observed on 6 of 11 archives from one recorder, with remainders of
    # 32, 48 and 64 bytes). That remainder starts with a plausible TOC byte but can never
    # form a complete Opus packet, so it is dropped rather than emitted as an
    # undecodable page. We still require its TOC byte to satisfy the same mono
    # and duration checks as a full packet. Raw output keeps these bytes.
    usable = len(raw) - len(raw) % 80
    tail = raw[usable:]
    if tail:
        try:
            _packet_samples(tail)
        except (ValueError, IndexError):
            raise ValueError("truncated final packet has an invalid TOC byte") from None
    output = io.BytesIO()
    writer = OggOpusWriter(output)
    for start in range(0, usable, 80):
        writer.write_packet(raw[start:start + 80])
    writer.finish()
    return output.getvalue()


class OggOpusWriter:
    """Incrementally write a mono Ogg Opus stream of 80-byte packets.

    The OpusHead and OpusTags header pages are written on construction, then
    `write_packet(packet)` queues each 80-byte packet and flushes the previous
    one as its own page with a running granule position and sequence number.
    Call `finish()` to mark the last flushed page as end-of-stream. A writer
    that is never finished leaves a stream without an EOS page: every page it
    has flushed is complete and playable, only the most recently queued packet
    is not yet written. This is what makes Ctrl-C output salvageable.
    """

    def __init__(self, output):
        self._output = output
        self._sequence = 2
        self._samples = 0
        self._pending = None
        self._finished = False
        output.write(_page(_OPUS_HEAD, 0, 0, 2))
        output.write(_page(_OPUS_TAGS, 1, 0, 0))

    def write_packet(self, packet: bytes) -> None:
        """Queue one fixed-size packet, flushing the previously queued one."""
        if self._finished:
            raise ValueError("cannot write to a finished Ogg Opus writer")
        if len(packet) != 80:
            raise ValueError("live archive packets must be exactly 80 bytes")
        if self._pending is not None:
            self._output.write(_page(self._pending, self._sequence, self._samples, 0))
            self._sequence += 1
        self._samples += _packet_samples(packet)
        self._pending = packet

    def finish(self) -> None:
        """Flush the queued packet as the EOS page. Safe to call twice."""
        if self._finished:
            return
        self._finished = True
        if self._pending is not None:
            self._output.write(_page(self._pending, self._sequence, self._samples, 4))
            self._pending = None


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
