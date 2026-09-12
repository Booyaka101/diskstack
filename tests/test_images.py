"""Input containers: what each one can be trusted to have actually held."""

from __future__ import annotations

from pathlib import Path

import pytest

from diskstack import candidates, cli, formats
from diskstack._vendor.greaseweazle.image.hfe import HFE
from diskstack._vendor.greaseweazle.image.imd import IMD
from diskstack._vendor.greaseweazle.image.kryoflux import KryoFlux
from diskstack._vendor.greaseweazle.track import PLL

from test_filler import FORMAT, SECSZ, write_img

KF_CYLS = 4


@pytest.fixture()
def fmt():
    return formats.get_format(FORMAT)


def test_a_short_image_does_not_fabricate_the_sectors_it_lacks(tmp_path, fmt):
    """The reader zero-fills a short file and calls every sector CRC-clean."""
    whole = write_img(tmp_path / 'whole.img', bad=set())
    part = tmp_path / 'part.img'
    part.write_bytes(whole.read_bytes()[:8 * SECSZ])

    cands, info = candidates.load(part, fmt)

    assert (info.candidates, info.good) == (8, 8)
    assert [c.sec_id for c in cands] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert info.wrong_size is False          # only cli.run fills that in
    whole_cands, _ = candidates.load(whole, fmt)
    assert [c.data for c in cands] == [c.data for c in whole_cands[:8]]


def test_an_empty_image_contributes_nothing(tmp_path, fmt):
    empty = tmp_path / 'empty.img'
    empty.write_bytes(b'')

    cands, info = candidates.load(empty, fmt)

    assert cands == []
    assert (info.candidates, info.good) == (0, 0)


def write_imd(path: Path, fmt) -> Path:
    """An IMD of the same disk. Its file size has nothing to do with a format."""
    src, _ = formats.open_image(write_img(path.parent / 'src.img', set()), fmt)
    with IMD.to_file(str(path), fmt, False, {}) as out:
        for cyl, head in candidates.track_list(fmt):
            out.emit_track(cyl, head, src.get_track(cyl, head))
    return path


def test_an_imd_is_not_measured_against_the_flat_image_size(tmp_path, fmt):
    """IMD compresses runs, so its size says nothing about what it holds."""
    path = write_imd(tmp_path / 'disk.imd', fmt)
    cands, info = candidates.load(path, fmt)

    assert info.kind == 'imd'
    assert info.kind not in formats.FLAT_KINDS
    assert info.size != sum(len(c.data) for c in cands)
    assert len(cands) == 720


def write_kryoflux(path: Path, source: Path, cyls: int = KF_CYLS) -> Path:
    """Some of a flux capture, rewritten as KryoFlux stream files."""
    src, _ = formats.open_image(source, None)
    out = KryoFlux(str(path), None)
    for cyl in range(cyls):
        for head in (0, 1):
            out.emit_track(cyl, head, src.get_track(cyl, head))
    return path


def test_kryoflux_streams_decode_to_the_same_sectors_as_the_scp(
        tmp_path, decoded):
    capture = decoded.paths[0]
    stream = write_kryoflux(tmp_path / 'dump00.0.raw', capture)

    got, info = candidates.load(stream, decoded.fmt)
    want = [c for c in decoded.candidates
            if c.source == capture and c.cyl < KF_CYLS]

    assert info.kind == 'kryoflux'
    assert info.revolutions == 3
    assert [(c.key, c.data, c.data_crc_ok) for c in got] == \
           [(c.key, c.data, c.data_crc_ok) for c in want]


def test_the_extent_of_a_flux_dump_is_found_by_probing(tmp_path, decoded):
    stream = write_kryoflux(tmp_path / 'dump00.0.raw', decoded.paths[0])
    image, _ = formats.open_image(stream, None)

    assert formats._flux_extent(image) == (KF_CYLS, 2)


def write_hfe(path: Path, fmt) -> Path:
    src, _ = formats.open_image(write_img(path.parent / 'src.img', set()), fmt)
    with HFE.to_file(str(path), fmt, False, {}) as out:
        for cyl, head in candidates.track_list(fmt):
            out.emit_track(cyl, head, src.get_track(cyl, head))
    return path


def test_an_hfe_bitstream_reads_back_with_its_crcs(tmp_path, fmt, capsys):
    path = write_hfe(tmp_path / 'disk.hfe', fmt)
    capsys.readouterr()

    cands, info = candidates.load(path, fmt)

    assert (info.kind, info.candidates, info.good) == ('hfe', 720, 720)
    # Unlike .img, an HFE carries the on-disk CRC, so it can confirm a vote.
    assert all(c.check and c.recheck(c.data) for c in cands)
    assert formats.detect_format([path])[0] == FORMAT


def test_repeating_pll_decodes_an_hfe_twice(tmp_path, fmt, capsys):
    """Every flux container gets the extra decodes, not just SCP."""
    path = write_hfe(tmp_path / 'disk.hfe', fmt)
    capsys.readouterr()
    plls = [PLL('period=5:phase=60'), PLL('period=10:phase=40')]

    cands, info = cli.load_source(path, fmt, plls, None, detect_filler=True)

    once, _ = candidates.load(path, fmt)
    assert len(once) == 720
    assert (info.candidates, len(cands)) == (1440, 1440)
    assert [c.key for c in cands] == [c.key for c in once] * 2


def test_a_kryoflux_input_is_measured_as_the_whole_set(tmp_path, decoded):
    """One .raw names a file per track; the report should not quote just one."""
    stream = write_kryoflux(tmp_path / 'dump00.0.raw', decoded.paths[0])
    _, info = candidates.load(stream, decoded.fmt)

    files = sorted(tmp_path.glob('dump*.raw'))
    assert len(files) == KF_CYLS * 2
    assert info.size == sum(f.stat().st_size for f in files)
    assert info.size > stream.stat().st_size


def test_a_second_kryoflux_set_in_the_directory_is_not_counted_in(
        tmp_path, decoded):
    write_kryoflux(tmp_path / 'disk00.0.raw', decoded.paths[0])
    write_kryoflux(tmp_path / 'disk_take2_00.0.raw', decoded.paths[0])

    first = formats.input_size(tmp_path / 'disk00.0.raw', 'kryoflux')
    second = formats.input_size(tmp_path / 'disk_take2_00.0.raw', 'kryoflux')

    assert first == second
    assert first * 2 == sum(f.stat().st_size for f in tmp_path.glob('*.raw'))
