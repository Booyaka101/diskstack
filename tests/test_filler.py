"""Sector images have no 'this sector was unreadable' flag, so look for filler."""

from __future__ import annotations

from pathlib import Path

from diskstack import candidates, formats, stack
from diskstack.filler import FILLER, is_filler, make_filler

FORMAT = 'ibm.360'
SECTORS = 720
SECSZ = 512


def sector_key(index: int) -> tuple:
    return index // 18, (index % 18) // 9, index % 9 + 1


def write_img(path: Path, bad: set, seed: int = 0,
              sectors: int = SECTORS) -> Path:
    """A sector image whose bad sectors hold Greaseweazle's filler."""
    out = bytearray()
    for index in range(sectors):
        if index in bad:
            out += make_filler(SECSZ)
        else:
            out += bytes([(index + seed + i) & 0xff for i in range(SECSZ)])
    path.write_bytes(bytes(out))
    return path


def test_is_filler():
    assert is_filler(FILLER)
    assert is_filler(FILLER * 32)
    assert not is_filler(FILLER * 32 + b'\0')
    assert not is_filler(b'')
    assert not is_filler(bytes(512))


def test_filler_sectors_are_treated_as_failed_reads(tmp_path: Path):
    bad = {0, 5, 17, 400}
    path = write_img(tmp_path / 'dump.img', bad)
    fmt = formats.get_format(FORMAT)

    cands, info = candidates.load(path, fmt)
    assert len(cands) == SECTORS
    failed = {c.key for c in cands if not c.data_crc_ok}
    assert failed == {sector_key(i) for i in bad}
    assert info.good == SECTORS - len(bad)


def test_keep_filler_trusts_them_again(tmp_path: Path):
    path = write_img(tmp_path / 'dump.img', {0, 5})
    fmt = formats.get_format(FORMAT)
    cands, info = candidates.load(path, fmt, detect_filler=False)
    assert info.good == SECTORS
    assert all(c.data_crc_ok for c in cands)


def test_a_second_image_supplies_what_the_filler_hid(tmp_path: Path):
    bad_a, bad_b = {0, 5, 17}, {17, 400}
    a = write_img(tmp_path / 'a.img', bad_a)
    b = write_img(tmp_path / 'b.img', bad_b)
    fmt = formats.get_format(FORMAT)

    cands = []
    for path in (a, b):
        got, _ = candidates.load(path, fmt)
        cands += got
    expected = candidates.expected_sectors(fmt)
    sizes = {key: candidates.sector_size(fmt, *key) for key in expected}
    result = stack.stack(cands, expected, sizes, [a, b])

    counts = result.counts()
    assert counts[stack.CLEAN] == SECTORS - 1
    # Sector 17 is filler in both dumps, so nothing can confirm it.
    assert counts[stack.UNRESOLVED] == 1
    assert result.bad_sectors() == {sector_key(17)[:2]: [sector_key(17)[2]]}

    data = result.sector_data()
    for index in bad_a - bad_b:
        cyl, head, sec = sector_key(index)
        assert not is_filler(data[cyl, head][sec])
