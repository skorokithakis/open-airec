import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airec import save_audio, to_ogg_opus
from airec.audio import _packet_samples


PACKET = b"\x48" + b"\0" * 79
# Real firmware-truncated final slot: TOC 0x4b, frame count 0x41, padding
# length 0x47, then padding to 48 bytes.
TRUNCATED_TAIL = b"\x4b\x41\x47" + b"\0" * 45


def pages(data):
    offset = 0
    while offset < len(data):
        count = data[offset + 26]
        size = 27 + count + sum(data[offset + 27:offset + 27 + count])
        yield data[offset:offset + size]
        offset += size


def reference_crc(data):
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc


class AudioTests(unittest.TestCase):
    def test_container_and_payload_preservation(self):
        result = list(pages(to_ogg_opus(PACKET * 3)))
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0][5], 2)
        self.assertEqual(result[-1][5], 4)
        self.assertEqual(result[0][28:36], b"OpusHead")
        self.assertEqual(result[1][28:36], b"OpusTags")
        self.assertEqual(b"".join(page[28:] for page in result[2:]), PACKET * 3)
        for index, page in enumerate(result):
            self.assertEqual(page[:4], b"OggS")
            self.assertEqual(struct.unpack_from("<I", page, 18)[0], index)
            expected = struct.unpack_from("<I", page, 22)[0]
            zeroed = page[:22] + b"\0" * 4 + page[26:]
            self.assertEqual(reference_crc(zeroed), expected)
        self.assertEqual(struct.unpack_from("<Q", result[-1], 6)[0], 2880)

    def test_packet_timing(self):
        for toc, second, samples in ((0x48, 0, 960), (0x4B, 0x41, 960),
                                      (0x80, 0, 120), (0x60, 0, 480),
                                      (0x4A, 0, 1920)):
            self.assertEqual(_packet_samples(bytes([toc, second])), samples)

    def test_invalid_audio(self):
        for data in (b"", PACKET[:-1], b"\x4c" + PACKET[1:],
                     b"\x4b\x00" + PACKET[2:], b"\x4b\x3f" + PACKET[2:]):
            with self.assertRaises(ValueError):
                to_ogg_opus(data)

    def test_aligned_input_is_unchanged(self):
        result = list(pages(to_ogg_opus(PACKET * 4)))
        self.assertEqual(result[-1][5], 4)
        self.assertEqual(b"".join(page[28:] for page in result[2:]), PACKET * 4)

    def test_truncated_final_packet_is_dropped(self):
        self.assertEqual(len(TRUNCATED_TAIL), 48)
        self.assertEqual(to_ogg_opus(PACKET * 3 + TRUNCATED_TAIL),
                         to_ogg_opus(PACKET * 3))

    def test_truncated_final_packet_invalid_toc_is_rejected(self):
        for tail in (b"\x4c" + b"\0" * 47, b"\x4b\x3f" + b"\0" * 46,
                     b"\x4b\x00" + b"\0" * 46, b"\x4b"):
            with self.assertRaises(ValueError):
                to_ogg_opus(PACKET * 2 + tail)

    def test_only_truncated_final_packet_is_rejected(self):
        with self.assertRaises(ValueError):
            to_ogg_opus(TRUNCATED_TAIL)

    def test_raw_format_preserves_truncated_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.airec"
            save_audio(PACKET * 2 + TRUNCATED_TAIL, path, format="raw")
            self.assertEqual(path.read_bytes(), PACKET * 2 + TRUNCATED_TAIL)

    def test_atomic_output_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.opus"
            save_audio(PACKET, path)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                save_audio(PACKET * 2, path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".airec-*")), [])
            raw_path = path.with_suffix(".airec")
            save_audio(PACKET, raw_path, format="raw")
            self.assertEqual(raw_path.read_bytes(), PACKET)

    def test_failed_publish_and_conversion_leave_no_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.opus"
            with patch("airec.audio.os.link", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    save_audio(PACKET, path)
            self.assertEqual(list(path.parent.iterdir()), [])
            with self.assertRaises(ValueError):
                save_audio(b"bad", path)
            self.assertFalse(path.exists())

    def test_symlink_is_not_followed_or_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_bytes(b"user data")
            link = Path(directory) / "link"
            link.symlink_to(target)
            with self.assertRaises(FileExistsError):
                save_audio(PACKET, link)
            self.assertEqual(target.read_bytes(), b"user data")
            self.assertTrue(link.is_symlink())
