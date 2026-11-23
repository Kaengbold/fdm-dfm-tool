"""Read the Prusa binary G-code container, one block at a time.

Format: https://github.com/prusa3d/libbgcode/blob/main/doc/specifications.md
MeatPack wire format: https://github.com/prusa3d/libbgcode/blob/main/src/LibBGCode/binarize/meatpack.cpp
Heatshrink bit format: https://github.com/atomicobject/heatshrink
"""

import re
import struct
import zlib

MAGIC = b"GCDE"
MAX_BLOCK = 64 * 1024 * 1024  # bound a single allocation even if a corrupt header claims more
_DIGITS = b"0123456789."
_COMPACT_G = re.compile(rb"^(G\d+)((?:[A-Z][-+]?(?:\d+\.?\d*|\.\d+))+)(?=[ \t\r;\n]|$)", re.MULTILINE)
_G_WORD = re.compile(rb"[A-Z][-+]?(?:\d+\.?\d*|\.\d+)")


def _error(reason: str) -> ValueError:
    return ValueError(f"Can't read this binary G-code: {reason}")


def _heatshrink(data: memoryview, size: int, window: int) -> bytes:
    # Bits are most significant first. A 1 introduces a literal; a 0 introduces
    # a distance and count (both stored minus one). Copies can overlap.
    pos, end = 0, len(data) * 8
    out = bytearray()
    while len(out) < size:
        i, used = divmod(pos, 8)
        literal = data[i] & (0x80 >> used) if pos < end else 0
        width = 9 if literal else 1 + window + 4
        if pos + width > end:
            raise _error("a compressed block ended early")
        bits = int.from_bytes(data[i:i + 3], "big")
        word = (bits >> (min(3, len(data) - i) * 8 - used - width)) & ((1 << (width - 1)) - 1)
        pos += width
        if literal:
            out.append(word)
        else:
            distance, count = (word >> 4) + 1, (word & 15) + 1
            if len(out) + count > size:
                raise _error("a compressed block has the wrong size")
            for _ in range(count):
                out.append(out[-distance] if distance <= len(out) else 0)
    if end - pos > 7 or (end > pos and data[-1] & ((1 << (end - pos)) - 1)):
        raise _error("a compressed block has extra data")
    return bytes(out)


def _decompress(data: memoryview, size: int, compression: int) -> bytes:
    if compression == 0:
        return bytes(data)
    if compression == 1:
        decoder = zlib.decompressobj()
        try:
            out = decoder.decompress(data, size + 1)
        except zlib.error as exc:
            raise _error("a compressed block is damaged") from exc
        if len(out) != size or not decoder.eof or decoder.unused_data:
            raise _error("a compressed block has the wrong size")
        return out
    return _heatshrink(data, size, 11 if compression == 2 else 12)


def _meatpack(data: bytes) -> bytes:
    # Low nibble first. A 15 takes its character from the bytes after the pair.
    packed, table, i = False, _DIGITS + b" \nGX", 0
    out = bytearray()
    while i < len(data):
        pair = data[i]
        i += 1
        if pair == 255 and i < len(data) and data[i] == 255:
            if i + 1 >= len(data):
                raise _error("a MeatPack command ended early")
            command = data[i + 1]
            i += 2
            if command == 251:
                packed = True
            elif command in (249, 250):
                packed = False
            elif command in (246, 247):
                table = _DIGITS + (b"E\nGX" if command == 247 else b" \nGX")
            elif command != 248:
                raise _error("an unknown MeatPack command")
            continue
        if not packed:
            out.append(pair)
            continue
        for n in (pair & 15, pair >> 4):
            if n == 15:
                if i == len(data):
                    raise _error("a MeatPack character ended early")
                out.append(data[i])
                i += 1
            else:
                out.append(table[n])
            if n == 12:  # a newline in the low nibble pads out the pair
                break
    # Older PrusaSlicer versions compact commands such as G92 as well as moves.
    # Restore numeric word boundaries without changing comments or command text.
    return _COMPACT_G.sub(lambda m: m[1] + _G_WORD.sub(rb" \g<0>", m[2]), out)


def blocks(source):
    """Yield plain G-code bytes, including producer, timing and slicer settings as comments."""
    data = memoryview(source)
    if len(data) < 10 or data[:4] != MAGIC:
        raise _error("the file header is missing or cut short")
    version, checksum = struct.unpack_from("<IH", data, 4)
    if version != 1 or checksum not in (0, 1):
        raise _error("the format version or checksum type isn't supported")

    pos, has_gcode = 10, False
    metadata = []
    while pos < len(data):
        head = pos
        if pos + 8 > len(data):
            raise _error("a block header is cut short")
        kind, compression, size = struct.unpack_from("<HHI", data, pos)
        pos += 8
        if kind > 5 or compression > 3:
            raise _error("an unknown block type or compression")
        stored = size
        if compression:
            if pos + 4 > len(data):
                raise _error("a block header is cut short")
            stored, = struct.unpack_from("<I", data, pos)
            pos += 4
        params = 6 if kind == 5 else 2
        end = pos + params + stored
        if end + 4 * checksum > len(data):
            raise _error("a block is cut short")
        if checksum and zlib.crc32(data[head:end]) != struct.unpack_from("<I", data, end)[0]:
            raise _error("a block failed its checksum")
        encoding, = struct.unpack_from("<H", data, pos)
        body = data[pos + params:end]
        pos = end + 4 * checksum
        if kind == 5 or (kind == 2 and encoding == 1):  # thumbnail or optional PrusaSlicer 3 JSON config
            continue
        if max(size, stored) > MAX_BLOCK:
            raise _error("a block is too large")
        payload = _decompress(body, size, compression)
        if kind == 1:
            if encoding not in (0, 1, 2):
                raise _error("an unknown G-code encoding")
            has_gcode = True
            yield _meatpack(payload) if encoding else payload
            continue
        if encoding != 0:
            raise _error("an unknown settings encoding")
        for line in payload.splitlines():
            key, _, value = line.partition(b"=")
            key, value = key.strip(), value.strip()
            if kind == 0 and key.lower() == b"producer":
                yield b"; generated by " + value + b"\n"
            if key:
                metadata.append(b"; " + key + b" = " + value + b"\n")
    if not has_gcode:
        raise _error("the file contains no printing commands")
    if metadata:
        yield b"\n" + b"".join(metadata)
