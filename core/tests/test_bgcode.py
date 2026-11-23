"""Binary files from PrusaSlicer and small independent examples of each codec."""

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api, bgcode, gcode, gcode_rewrite, thermal_opt

FIXTURES = Path(__file__).parent / "gcode"


def block(kind, encoding, raw, compression=0, payload=None):
    data = struct.pack("<HHI", kind, compression, len(raw))
    if compression:
        data += struct.pack("<I", len(payload))
    data += struct.pack("<H", encoding) + (raw if payload is None else payload)
    return data + struct.pack("<I", zlib.crc32(data))


HEADER = struct.pack("<4sIH", b"GCDE", 1, 1)


@pytest.mark.parametrize("compression", [0, 1, 2, 3])
def test_blocks_and_codecs(compression):
    raw = b"G1 X1\nG1 X1\n"
    if compression in (2, 3):
        window = 11 if compression == 2 else 12
        # Literal "G1 X1\n", followed by a backreference of distance and length 6.
        bits = "".join(f"1{byte:08b}" for byte in raw[:6]) + f"0{5:0{window}b}0101"
        bits += "0" * (-len(bits) % 8)
        payload = int(bits, 2).to_bytes(len(bits) // 8, "big")
    else:
        payload = zlib.compress(raw) if compression else None
    data = HEADER + block(1, 0, raw, compression, payload)
    assert b"".join(bgcode.blocks(data)) == raw
    assert b"".join(bgcode.blocks(HEADER[:8] + b"\0\0" + data[10:-4])) == raw
    with pytest.raises(ValueError, match="checksum"):
        list(bgcode.blocks(data[:-1] + bytes([data[-1] ^ 1])))
    with pytest.raises(ValueError, match="cut short"):
        list(bgcode.blocks(data[:-3]))


@pytest.mark.parametrize("encoding", [1, 2])
def test_meatpack_nibbles_and_literal_characters(encoding):
    # 'G1X0E1\n' in no-spaces mode, then two literal characters and raw comments.
    data = bytes.fromhex("ff ff f7 ff ff fb 1d 0e 1b 0c ff 4d 38 c3 ff ff fa") + b";TYPE:Perimeter\n"
    assert b"".join(bgcode.blocks(HEADER + block(1, encoding, data))) == b"G1 X0 E1\nM83\n;TYPE:Perimeter\n"


def test_prusaslicer_binary_file_loads_and_writes_plain_gcode():
    data = (FIXTURES / "prusa-2.8.bgcode").read_bytes()
    api.load((FIXTURES / "part.stl").read_bytes(), "fixture")
    info = api.load_gcode(data, [0, 0, 1], {"printer": "prusa_mk4s"})
    assert info["slicer"] == "prusa" and info["firmware"] == "marlin"
    tp = gcode.parse(data)
    # Checked against an ASCII export of the same slice, including its compact G92 resets.
    assert len(tp.p0) == 1985 and tp.volume.sum() == pytest.approx(921.56167)
    assert tp.total_s == 289 and tp.settings["layer_height"] == "0.3"
    plan = thermal_opt.Plan.none(len(tp.layer_z))
    plan.slow[2:5] = 0.85
    new = gcode_rewrite.rewrite(data, tp, plan)
    assert new.text.startswith("; FDM DFM Tool") and "G92 E0" in new.text
    assert new.changed_lines > 0 and gcode_rewrite.check(tp, new.text, new.toolpath) == []
    assert np.allclose(gcode.parse(new.text).volume, tp.volume)
