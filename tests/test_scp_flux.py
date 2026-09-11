"""The SCP flux overflow, which is what a badly damaged track actually holds.

Flux values are 16 bits of 25ns ticks. A gap longer than that is written as
one or more 0x0000 words, each adding 65536 ticks to the value that follows,
so a dropout in the middle of a track does not simply wrap around.
"""

from __future__ import annotations

import struct
from pathlib import Path

from diskstack._vendor.greaseweazle.image.scp import SCP

TLUT_END = 0x2b0
TICKS_NS = 25


def build_scp(values: list, ticks: int = 200_000) -> bytes:
    """A one-track, one-revolution SCP holding exactly ``values``."""
    track = b'TRK' + bytes([0])
    track += struct.pack('<3I', ticks, len(values), 4 + 12)
    for value in values:
        track += struct.pack('>H', value)

    tlut = [0] * 168
    tlut[0] = TLUT_END
    body = struct.pack('<168I', *tlut) + track
    header = struct.pack('<3s9B', b'SCP', 0x18, 0, 1, 0, 0, 1, 0, 0, 0)
    return header + struct.pack('<I', sum(body) & 0xffffffff) + body


def flux_of(values: list, tmp_path: Path):
    path = tmp_path / 'one.scp'
    path.write_bytes(build_scp(values))
    return SCP.from_file(str(path), None, {}).get_track(0, 0)


def test_a_plain_interval(tmp_path: Path):
    flux = flux_of([0x7fff], tmp_path)
    assert flux.list == [0x7fff]
    assert flux.sample_freq == 40e6


def test_two_zero_words_add_two_times_65536(tmp_path: Path):
    """The worked example from the SuperCard Pro specification."""
    flux = flux_of([0x0000, 0x0000, 0x7fff], tmp_path)
    assert flux.list == [163839]
    assert flux.list[0] * TICKS_NS == 4_095_975


def test_the_accumulator_resets_after_each_real_value(tmp_path: Path):
    flux = flux_of([0x0000, 0x0064, 0x0064, 0x0000, 0x0064], tmp_path)
    assert flux.list == [65636, 100, 65636]
