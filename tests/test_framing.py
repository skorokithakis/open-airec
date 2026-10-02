import unittest

from airec_client.framing import Frame, FrameDecoder, encode_request


class FramingTests(unittest.TestCase):
    def test_captured_requests(self):
        self.assertEqual(encode_request(0x0E).hex(), "55aa010e")
        self.assertEqual(encode_request(5).hex(), "55aa0105")

    def test_captured_battery_and_end(self):
        decoder = FrameDecoder()
        self.assertEqual(
            decoder.feed(bytes.fromhex("aa55020e64aa550106")),
            (Frame(14, b"\x64"), Frame(6, b"")),
        )

    def test_every_fragment_boundary(self):
        packet = bytes.fromhex("aa551305") + b"20260312061422" + bytes.fromhex("0000002c")
        for boundary in range(len(packet) + 1):
            with self.subTest(boundary=boundary):
                decoder = FrameDecoder()
                result = decoder.feed(packet[:boundary]) + decoder.feed(packet[boundary:])
                self.assertEqual(result, (Frame(5, packet[4:]),))

    def test_byte_by_byte(self):
        decoder = FrameDecoder()
        result = ()
        for byte in bytes.fromhex("aa55020e64aa550106"):
            result += decoder.feed(bytes([byte]))
        self.assertEqual(result, (Frame(14, b"\x64"), Frame(6, b"")))

    def test_noise_and_invalid_length(self):
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(bytes.fromhex("00ffaaaa5500aa550106")), (Frame(6, b""),))

    def test_header_in_payload(self):
        self.assertEqual(FrameDecoder().feed(bytes.fromhex("aa550305aa55")), (Frame(5, b"\xaa\x55"),))

    def test_reset(self):
        decoder = FrameDecoder()
        decoder.feed(bytes.fromhex("aa55020e"))
        decoder.reset()
        self.assertEqual(decoder.feed(bytes.fromhex("aa550106")), (Frame(6, b""),))

    def test_bounds(self):
        for command in (-1, 256):
            with self.assertRaises(ValueError):
                encode_request(command)
        with self.assertRaises(ValueError):
            encode_request(1, b"x" * 255)
        self.assertEqual(len(encode_request(1, b"x" * 254)), 258)


if __name__ == "__main__":
    unittest.main()
