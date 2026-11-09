import math
import os
import struct
import tempfile
import unittest
import wave

from livecli import audiofile


def _ext80(x):
    """float -> IEEE 754 80-bit extended (positive integers, enough for sample rates)."""
    e = int(math.floor(math.log(x, 2)))
    mant = int(x * 2 ** (63 - e))
    return struct.pack(">HQ", e + 16383, mant)


class AudioFileTest(unittest.TestCase):
    def setUp(self):
        self.sig = [int(20000 * math.sin(2 * math.pi * k / 48.0)) for k in range(4800)]
        self.dir = tempfile.mkdtemp()

    def test_wav_16_stereo(self):
        p = os.path.join(self.dir, "t.wav")
        with wave.open(p, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"".join(struct.pack("<hh", v, v) for v in self.sig))
        x, sr = audiofile.read_mono(p, 0.01, 0.05)
        self.assertEqual(sr, 48000)
        self.assertEqual(len(x), 2400)
        self.assertAlmostEqual(x[0], self.sig[480] / 32768.0, places=6)

    def test_aiff_24(self):
        p = os.path.join(self.dir, "t.aif")
        pcm = b"".join((v * 256).to_bytes(3, "big", signed=True) for v in self.sig)
        comm = struct.pack(">hIh", 1, len(self.sig), 24) + _ext80(44100.0)
        ssnd = struct.pack(">II", 0, 0) + pcm
        body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
        with open(p, "wb") as f:
            f.write(b"FORM" + struct.pack(">I", len(body)) + body)
        x, sr = audiofile.read_mono(p)
        self.assertEqual(sr, 44100)
        self.assertEqual(len(x), len(self.sig))
        self.assertAlmostEqual(x[100], self.sig[100] / 32768.0, places=5)

    def test_not_audio(self):
        p = os.path.join(self.dir, "t.txt")
        with open(p, "w") as f:
            f.write("hello")
        with self.assertRaises(audiofile.AudioFileError):
            audiofile.read_mono(p)


if __name__ == "__main__":
    unittest.main()
