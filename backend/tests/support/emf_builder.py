"""A minimal EMF writer for tests and synthetic fixtures: header, a font, text runs with advances, lines."""

from __future__ import annotations

import struct


def rec(kind: int, payload: bytes) -> bytes:
    size = 8 + len(payload)
    pad = (-size) % 4
    return struct.pack("<II", kind, size + pad) + payload + b"\0" * pad


def header() -> bytes:
    # bounds, frame, signature, version, bytes, records, handles, reserved, nDescription, offDescription,
    # nPalEntries, device size, millimeters (88 bytes total with type/size)
    body = struct.pack("<iiii", 0, 0, 1000, 600) + struct.pack("<iiii", 0, 0, 26000, 16000)
    body += struct.pack("<IIIIHHIIIiiii", 0x464D4520, 0x10000, 0, 0, 0, 0, 0, 0, 0, 1920, 1080, 530, 300)
    return rec(1, body)


def font(handle: int, height: int) -> bytes:
    logfont = struct.pack("<iiiii", -height, 0, 0, 0, 400) + b"\0" * 8 + "Arial".encode("utf-16le").ljust(64, b"\0")
    return rec(82, struct.pack("<I", handle) + logfont)


def select(handle: int) -> bytes:
    return rec(37, struct.pack("<I", handle))


def text(x: int, y: int, s: str, char_w: int = 8) -> bytes:
    n = len(s)
    fixed = 8 + 16 + 12 + 40  # type/size, bounds, mode+scales, EMRTEXT
    off_string = fixed
    string = s.encode("utf-16le")
    string += b"\0" * ((-len(string)) % 4)
    off_dx = off_string + len(string)
    emrtext = struct.pack("<iiIII", x, y, n, off_string, 0) + struct.pack("<iiii", 0, 0, -1, -1) + struct.pack("<I", off_dx)
    body = struct.pack("<iiii", x, y, x + n * char_w, y + 15) + struct.pack("<Iff", 1, 1.0, 1.0) + emrtext
    body += string + struct.pack(f"<{n}i", *([char_w] * n))
    return struct.pack("<II", 84, 8 + len(body)) + body


def line(x0: int, y0: int, x1: int, y1: int) -> bytes:
    return rec(27, struct.pack("<ii", x0, y0)) + rec(54, struct.pack("<ii", x1, y1))


def eof() -> bytes:
    return rec(14, struct.pack("<III", 0, 16, 16))


def emf(*records: bytes) -> bytes:
    return header() + font(1, 15) + select(1) + b"".join(records) + eof()


def grid(xs: list[int], ys: list[int]) -> list[bytes]:
    return [line(x, ys[0], x, ys[-1]) for x in xs] + [line(xs[0], y, xs[-1], y) for y in ys]
