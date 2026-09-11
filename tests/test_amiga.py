"""The AmigaDOS side, through real MFM flux and through .adf images.

The .scp fixtures are a PC disk, so the Amiga codec needs its own coverage:
its sync pattern, its odd/even interleave and its 32-bit checksums are all
different, and upstream's decoder throws away every sector whose checksum
fails, which is the material diskstack exists to vote on.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Dict, List, Tuple

from bitarray import bitarray
from click.testing import CliRunner

from diskstack import candidates, stack
from diskstack._vendor.greaseweazle.codec.amiga import amigados
from diskstack.cli import main
from test_filler import write_img

CYL, HEAD, NSEC = 3, 1, 11
SECSZ = 512
DATA_OFFSET = 60          # bytes of sync, header, label and checksums
WINDOW = 24               # bytes of each sector scrambled per capture


def payload(seed: int = 7) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(NSEC * SECSZ))


def encode_track(data: bytes):
    track = amigados.AmigaDOS_DD(CYL, HEAD)
    track.set_img_track(data)
    return track.master_track()


def data_starts(bits: bitarray) -> Dict[int, int]:
    """Bit offset of each sector's data area, by sector id."""
    out: Dict[int, int] = {}
    for offs in bits.search(amigados.sync):
        if offs + 1088 * 8 > len(bits):
            continue
        sec = bits[offs:offs + 1088 * 8].tobytes()
        fmt_byte, _, sec_id, _ = tuple(amigados.decode(sec[4:12]))
        if fmt_byte == 0xff:
            out[sec_id] = offs + DATA_OFFSET * 8
    return out


def read_back(bits: bitarray, master, source: Path
              ) -> Tuple[List[candidates.Candidate], candidates.SourceInfo]:
    master.bits = bits
    proto = amigados.AmigaDOS_DD(CYL, HEAD)
    raw = candidates._pll_track(master.flux(revs=2), proto.clock,
                                proto.time_per_rev, None, None)
    info = candidates.SourceInfo(path=source, kind='scp')
    got = list(candidates._amiga_flux_candidates(raw, proto, CYL, HEAD,
                                                 source, info))
    return got, info


def scramble(bits: bitarray, start: int, count: int,
             rng: random.Random) -> None:
    bits[start:start + count] = bitarray(
        [rng.randint(0, 1) for _ in range(count)], endian='big')


def test_a_clean_amiga_track_decodes_every_sector():
    data = payload()
    master = encode_track(data)
    got, info = read_back(bitarray(master.bits, endian='big'), master,
                          Path('clean.scp'))

    assert sorted({c.sec_id for c in got}) == list(range(NSEC))
    assert sorted({c.rev for c in got}) == [0, 1]
    assert info.unexpected == []
    assert all(c.data_crc_ok and c.id_crc_ok for c in got)
    for cand in got:
        assert cand.data == data[cand.sec_id * SECSZ:(cand.sec_id + 1) * SECSZ]


def test_a_failed_amiga_checksum_is_kept_as_an_attempt():
    data = payload()
    master = encode_track(data)
    bits = bitarray(master.bits, endian='big')
    start = data_starts(bits)[4]
    scramble(bits, start, WINDOW * 8, random.Random(1))

    got, _ = read_back(bits, master, Path('hurt.scp'))
    hurt = [c for c in got if c.sec_id == 4]

    assert len(hurt) == 2, 'both revolutions of the bad sector are kept'
    assert not any(c.data_crc_ok for c in hurt)
    assert all(c.check for c in hurt), 'the on-disk checksum comes back too'
    assert sum(c.data_crc_ok for c in got) == (NSEC - 1) * 2


def damaged_capture(data: bytes, index: int) -> List[candidates.Candidate]:
    """One capture whose sector 4 is scrambled in its own 24-byte window."""
    master = encode_track(data)
    bits = bitarray(master.bits, endian='big')
    start = data_starts(bits)[4] + index * 120 * 8
    scramble(bits, start, WINDOW * 8, random.Random(index))
    got, _ = read_back(bits, master, Path(f'capture_{index}.scp'))
    return got


def test_three_amiga_captures_vote_a_checksum_back():
    data = payload()
    cands: List[candidates.Candidate] = []
    for index in range(3):
        cands += damaged_capture(data, index)

    sources = [Path(f'capture_{i}.scp') for i in range(3)]
    expected = [(CYL, HEAD, sec) for sec in range(NSEC)]
    sizes = {key: SECSZ for key in expected}
    result = stack.stack(cands, expected, sizes, sources)

    voted = [r for r in result.resolutions if r.status == stack.VOTED]
    assert [(r.cyl, r.head, r.sec_id) for r in voted] == [(CYL, HEAD, 4)]
    assert voted[0].data == data[4 * SECSZ:5 * SECSZ]
    assert result.counts()[stack.CLEAN] == NSEC - 1


ADF_SECTORS = 80 * 2 * NSEC


def write_adf(path: Path, bad: set) -> Path:
    return write_img(path, bad, sectors=ADF_SECTORS)


def test_two_adfs_merge_into_one(tmp_path: Path):
    a = write_adf(tmp_path / 'a.adf', {17, 900})
    b = write_adf(tmp_path / 'b.adf', {900, 12000})
    out = tmp_path / 'merged.adf'
    result = CliRunner().invoke(
        main, [str(a), str(b), '-o', str(out), '--no-report'])

    assert result.exit_code == 2, result.output
    assert 'amiga.amigados' in result.output
    assert '1760 sectors: 1759 clean (from 2 files), 1 unresolved' \
        in result.output

    clean = write_adf(tmp_path / 'clean.adf', set()).read_bytes()
    merged = out.read_bytes()
    assert len(merged) == len(clean)
    for index in range(ADF_SECTORS):
        if index == 900:
            continue
        lo = index * SECSZ
        assert merged[lo:lo + SECSZ] == clean[lo:lo + SECSZ]
