"""Read WAV and AIFF/AIFC files with the standard library only (no ffmpeg): integer PCM 8/16/24/32 bit and 32/64-bit
float, any channel count, mixed down to mono. Enough for the files Live records and bounces."""
import array
import math
import struct
import sys


class AudioFileError(Exception):
    pass


def _ext80(b):
    """IEEE 754 80-bit extended (AIFF sample rate) -> float."""
    exp = ((b[0] & 0x7F) << 8) | b[1]
    mant = int.from_bytes(b[2:10], "big")
    if exp == 0 and mant == 0:
        return 0.0
    sign = -1.0 if b[0] & 0x80 else 1.0
    return sign * mant * math.pow(2.0, exp - 16383 - 63)


def _chunks(data, start, big_endian):
    pos, fmt = start, ">4sI" if big_endian else "<4sI"
    while pos + 8 <= len(data):
        cid, size = struct.unpack_from(fmt, data, pos)
        yield cid, data[pos + 8: pos + 8 + size]
        pos += 8 + size + (size & 1)


def _decode(raw, bits, is_float, big_endian, channels):
    """Interleaved frames -> list of mono floats in [-1, 1]."""
    width = bits // 8
    if is_float:
        a = array.array("f" if bits == 32 else "d")
        a.frombytes(raw[: len(raw) // a.itemsize * a.itemsize])
        if big_endian != (sys.byteorder == "big"):
            a.byteswap()
        vals = a
    elif width in (2, 4):
        a = array.array("h" if width == 2 else "i")
        if a.itemsize != width:
            raise AudioFileError("unsupported integer width on this platform")
        a.frombytes(raw[: len(raw) // width * width])
        if big_endian != (sys.byteorder == "big"):
            a.byteswap()
        scale = float(1 << (bits - 1))
        vals = [v / scale for v in a]
    elif width == 3:
        n = len(raw) // 3
        order = "big" if big_endian else "little"
        vals = [int.from_bytes(raw[3 * k: 3 * k + 3], order, signed=True) / 8388608.0 for k in range(n)]
    elif width == 1:
        # WAV 8-bit is unsigned, AIFF 8-bit is signed
        vals = [((v - 128) if not big_endian else (v - 256 if v > 127 else v)) / 128.0 for v in raw]
    else:
        raise AudioFileError("unsupported sample size: %d bits" % bits)
    if channels == 1:
        return list(vals)
    n = len(vals) // channels
    return [sum(vals[k * channels: k * channels + channels]) / channels for k in range(n)]


def read_mono(path, offset_s=0.0, duration_s=None):
    """(samples, sample_rate) of a WAV/AIFF file, mono, from offset_s for duration_s seconds."""
    with open(path, "rb") as f:
        data = f.read()
    head = data[:4]
    if head == b"RIFF" and data[8:12] == b"WAVE":
        fmt = pcm = None
        for cid, body in _chunks(data, 12, False):
            if cid == b"fmt ":
                tag, ch, sr, _, _, bits = struct.unpack_from("<HHIIHH", body, 0)
                if tag == 0xFFFE and len(body) >= 26:          # WAVE_FORMAT_EXTENSIBLE: real tag in the sub-format GUID
                    tag = struct.unpack_from("<H", body, 24)[0]
                fmt = (tag, ch, sr, bits)
            elif cid == b"data":
                pcm = body
        if not fmt or pcm is None:
            raise AudioFileError("WAV without fmt or data chunk")
        tag, ch, sr, bits = fmt
        if tag not in (1, 3):
            raise AudioFileError("unsupported WAV encoding (format tag %d)" % tag)
        is_float, big = tag == 3, False
    elif head == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        comm = ssnd = None
        for cid, body in _chunks(data, 12, True):
            if cid == b"COMM":
                comm = body
            elif cid == b"SSND":
                ssnd = body
        if comm is None or ssnd is None:
            raise AudioFileError("AIFF without COMM or SSND chunk")
        ch, _, bits = struct.unpack_from(">hIh", comm, 0)
        sr = _ext80(comm[8:18])
        comp = comm[18:22] if data[8:12] == b"AIFC" and len(comm) >= 22 else b"NONE"
        if comp in (b"NONE", b"twos"):
            is_float, big = False, True
        elif comp == b"sowt":
            is_float, big = False, False
        elif comp in (b"fl32", b"FL32"):
            is_float, big, bits = True, True, 32
        elif comp in (b"fl64", b"FL64"):
            is_float, big, bits = True, True, 64
        else:
            raise AudioFileError("unsupported AIFC compression %r" % comp)
        off = struct.unpack_from(">I", ssnd, 0)[0]
        pcm = ssnd[8 + off:]
    else:
        raise AudioFileError("not a WAV or AIFF file")
    sr = int(round(sr))
    frame = (bits // 8) * ch
    first = int(round(offset_s * sr)) * frame
    last = len(pcm) if duration_s is None else min(len(pcm), first + int(round(duration_s * sr)) * frame)
    return _decode(pcm[first:last], bits, is_float, big, ch), sr
