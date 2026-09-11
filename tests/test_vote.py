"""Tier 2: a sector no read got right, rebuilt byte by byte."""

from __future__ import annotations

import struct
from pathlib import Path

from diskstack import stack
from diskstack.candidates import IBM_MFM, Candidate, verify_sector
from diskstack._vendor.greaseweazle.codec.ibm import ibm

MARK = ibm.Mark.DAM


def crc_bytes(data: bytes, mark: int = MARK) -> bytes:
    crc = ibm.crc16.new(b'\xa1\xa1\xa1' + bytes([mark]) + data).crcValue
    return struct.pack('>H', crc)


def candidate(data: bytes, check: bytes, source: str, rev: int = 0,
              ok: bool = False) -> Candidate:
    return Candidate(cyl=3, head=0, sec_id=5, data=data, id_crc_ok=True,
                     data_crc_ok=ok, source=Path(source), rev=rev,
                     codec=IBM_MFM, check=check, mark=MARK)


def flip_bit(data: bytes, offset: int, bit: int) -> bytes:
    out = bytearray(data)
    out[offset] ^= 1 << bit
    return bytes(out)


def test_check_bytes_are_right():
    data = bytes(range(256)) * 2
    assert verify_sector(IBM_MFM, MARK, data, crc_bytes(data))
    assert not verify_sector(IBM_MFM, MARK, flip_bit(data, 0, 0),
                             crc_bytes(data))


def test_three_single_bit_errors_vote_back_to_a_passing_crc():
    good = bytes(range(256)) * 2
    check = crc_bytes(good)
    group = [candidate(flip_bit(good, offset, bit), check, f'dump_{i}.scp')
             for i, (offset, bit) in enumerate([(0, 0), (100, 3), (511, 7)])]
    assert not any(verify_sector(IBM_MFM, MARK, c.data, c.check) for c in group)

    res = stack.resolve((3, 0, 5), 512, group)
    assert res.status == stack.VOTED
    assert res.data == good
    assert len(res.sources) == 3


def test_the_check_bytes_are_voted_on_too():
    """The CRC comes off the same damaged track, so it votes like the data."""
    good = bytes(range(256)) * 2
    check = crc_bytes(good)
    group = [
        candidate(flip_bit(good, 0, 0), flip_bit(check, 0, 1), 'a.scp'),
        candidate(flip_bit(good, 40, 2), flip_bit(check, 1, 1), 'b.scp'),
        candidate(flip_bit(good, 80, 4), flip_bit(check, 0, 3), 'c.scp'),
        candidate(flip_bit(good, 120, 6), flip_bit(check, 1, 3), 'd.scp'),
    ]
    assert not any(c.check == check for c in group)
    res = stack.resolve((3, 0, 5), 512, group)
    assert res.status == stack.VOTED
    assert res.data == good


def test_a_vote_that_still_fails_is_reported_unresolved():
    good = bytes(range(256)) * 2
    check = crc_bytes(good)
    group = [candidate(flip_bit(good, 7, 1), check, f'dump_{i}.scp')
             for i in range(3)]
    res = stack.resolve((3, 0, 5), 512, group)
    assert res.status == stack.UNRESOLVED
    assert res.agreement == 3


def test_one_passing_crc_beats_any_number_of_bad_reads():
    good = bytes(range(256)) * 2
    check = crc_bytes(good)
    group = [candidate(bytes(512), check, 'bad.scp'),
             candidate(bytes(512), check, 'worse.scp'),
             candidate(good, check, 'good.scp', ok=True)]
    res = stack.resolve((3, 0, 5), 512, group)
    assert res.status == stack.CLEAN
    assert res.data == good
    assert [c.source.name for c in res.sources] == ['good.scp']


def test_a_sector_no_input_holds_is_missing():
    res = stack.resolve((3, 0, 5), 512, [])
    assert res.status == stack.MISSING
    assert res.data.startswith(b'-=[BAD SECTOR]=-')
    assert len(res.data) == 512


def test_wrong_sized_reads_are_discarded_not_voted_on():
    good = bytes(range(256)) * 2
    check = crc_bytes(good)
    group = [candidate(good, check, 'good.scp', ok=True),
             candidate(bytes(128), check, 'short.scp')]
    res = stack.resolve((3, 0, 5), 512, group)
    assert res.discarded == 1
    assert res.data == good


def test_ties_break_towards_the_better_source():
    """With two reads and nothing to break the tie, the better dump wins."""
    values = [b'ab', b'cd']
    assert stack.majority_bytes(values) == b'ab'
    assert stack.largest_cluster(values) == (b'ab', 1)
    assert stack.largest_cluster([b'x', b'y', b'y']) == (b'y', 2)


def test_a_sector_that_reads_differently_every_pass_is_flagged_unstable():
    good = bytes(range(256)) * 2
    check = crc_bytes(good)
    group = [candidate(flip_bit(good, 10 * rev, 1), check, 'a.scp', rev=rev)
             for rev in range(3)]

    res = stack.resolve((3, 0, 5), 512, group, vote=False)

    assert res.status == stack.UNRESOLVED
    assert res.unstable


def test_two_captures_that_each_read_the_same_bytes_are_not_unstable():
    bad = bytes(512)
    group = [candidate(bad, crc_bytes(bad + b'x'), src, rev=rev)
             for src in ('a.scp', 'b.scp') for rev in range(2)]

    res = stack.resolve((3, 0, 5), 512, group, vote=False)

    assert res.status == stack.UNRESOLVED
    assert not res.unstable
