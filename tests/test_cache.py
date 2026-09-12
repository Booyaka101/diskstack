"""The decode cache: same answer as decoding again, or no answer at all."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from diskstack import cache, candidates, cli, formats
from diskstack._vendor.greaseweazle.track import PLL
from diskstack.cli import main

from test_filler import FORMAT, write_img
from test_images import write_hfe

SPEC = cache.settings(FORMAT, None, None, True)


@pytest.fixture()
def fmt():
    return formats.get_format(FORMAT)


def run(*args) -> object:
    return CliRunner().invoke(main, [str(a) for a in args])


def candidate(sec_id: int, **kw) -> candidates.Candidate:
    fields = dict(cyl=3, head=1, sec_id=sec_id, data=bytes([sec_id]) * 512,
                  id_crc_ok=True, data_crc_ok=False, source=Path('a.scp'),
                  rev=2, codec=candidates.IBM_MFM, check=b'\x12\x34',
                  mark=0xfb)
    return candidates.Candidate(**{**fields, **kw})


def source_info() -> candidates.SourceInfo:
    return candidates.SourceInfo(path=Path('a.scp'), kind='scp', revolutions=3,
                                 candidates=3, good=1, size=4096,
                                 unexpected=[(1, 0, 9)])


def entries(directory: Path) -> list:
    return sorted(p.stem for p in directory.glob('*' + cache.SUFFIX))


def _refuse(*args, **kwargs):
    raise AssertionError('detected again')


def test_an_entry_round_trips_every_field_of_every_attempt(tmp_path: Path):
    cands = [candidate(1),
             candidate(2, codec=candidates.AMIGA, check=bytes(4), rev=0,
                       id_crc_ok=False, data_crc_ok=True, mark=0),
             candidate(3, data=b'', check=b'')]
    info = source_info()

    cache.store(tmp_path, 'k', cands, info)
    back, back_info = cache.load(tmp_path, 'k', Path('a.scp'))

    assert back == cands
    assert back_info == dataclasses.replace(info, cached=True)
    assert not list(tmp_path.glob('*.tmp'))


def test_a_renamed_capture_still_hits_and_takes_its_new_name(tmp_path: Path):
    """The entry is named for the bytes, so moving the file does not lose it."""
    cache.store(tmp_path, 'k', [candidate(1)], source_info())

    back, info = cache.load(tmp_path, 'k', Path('sub/renamed.scp'))

    assert info.path == Path('sub/renamed.scp')
    assert {c.source for c in back} == {Path('sub/renamed.scp')}


def test_a_missing_entry_is_simply_a_miss(tmp_path: Path):
    assert cache.load(tmp_path, 'nothing-here', Path('a.scp')) is None


def test_the_key_follows_the_bytes_not_the_name(tmp_path: Path):
    path = tmp_path / 'a.scp'
    path.write_bytes(b'flux')
    first = cache.key([path], SPEC)

    assert cache.key([path], SPEC) == first
    path.write_bytes(b'flux!')
    assert cache.key([path], SPEC) != first


def test_every_setting_that_changes_the_decode_changes_the_key(tmp_path: Path):
    path = tmp_path / 'a.scp'
    path.write_bytes(b'flux')
    specs = [SPEC,
             cache.settings('ibm.720', None, None, True),
             cache.settings(FORMAT, PLL('period=1:phase=10'), None, True),
             cache.settings(FORMAT, PLL('period=1:phase=10:lowpass=1.5'),
                            None, True),
             cache.settings(FORMAT, None, 2, True),
             cache.settings(FORMAT, None, None, False)]

    assert len({cache.key([path], s) for s in specs}) == len(specs)


def test_a_kryoflux_key_covers_every_file_of_the_set(tmp_path: Path):
    """One .raw names a set, so a changed track file has to be a new entry."""
    first = tmp_path / 'dump00.0.raw'
    first.write_bytes(b'one')
    second = tmp_path / 'dump00.1.raw'
    second.write_bytes(b'two')
    before = cache.key(formats.input_files(first, 'kryoflux'), SPEC)

    second.write_bytes(b'three')

    assert cache.key(formats.input_files(first, 'kryoflux'), SPEC) != before


def test_a_damaged_entry_is_ignored_rather_than_trusted(tmp_path: Path):
    cache.store(tmp_path, 'k', [candidate(1)], source_info())
    entry = tmp_path / ('k' + cache.SUFFIX)
    whole = entry.read_bytes()

    for damaged in (b'', b'nonsense', b'X' + whole[1:],
                    whole[:len(whole) // 2], whole[:-1]):
        entry.write_bytes(damaged)
        assert cache.load(tmp_path, 'k', Path('a.scp')) is None


def test_a_cache_that_will_not_write_is_not_an_error(tmp_path: Path):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('x')

    cache.store(blocked, 'k', [candidate(1)], source_info())

    assert cache.load(blocked, 'k', Path('a.scp')) is None
    assert blocked.read_text() == 'x'


def test_the_cache_drops_its_oldest_entries_when_it_fills(tmp_path: Path):
    for age, name in enumerate(['old', 'middle', 'new']):
        cache.store(tmp_path, name, [candidate(1)], source_info())
        os.utime(tmp_path / (name + cache.SUFFIX), (age, age))
    total = sum(p.stat().st_size for p in tmp_path.glob('*' + cache.SUFFIX))

    cache.prune(tmp_path, max_bytes=total * 2 // 3)

    assert entries(tmp_path) == ['middle', 'new']


def test_reading_an_entry_saves_it_from_the_next_prune(tmp_path: Path):
    for age, name in enumerate(['old', 'new']):
        cache.store(tmp_path, name, [candidate(1)], source_info())
        os.utime(tmp_path / (name + cache.SUFFIX), (age, age))
    total = sum(p.stat().st_size for p in tmp_path.glob('*' + cache.SUFFIX))

    cache.load(tmp_path, 'old', Path('a.scp'))
    cache.prune(tmp_path, max_bytes=total // 2)

    assert entries(tmp_path) == ['old']


def test_a_sector_image_does_not_get_an_entry(tmp_path: Path, fmt):
    """Reading an .img is a file read; an entry would cost what it saves."""
    image = write_img(tmp_path / 'dump.img', bad=set())
    opts = cli.Decode(fmt=fmt, fmt_name=FORMAT, cache_dir=tmp_path / 'cache')

    assert cli.cache_name(image, opts, None) is None


def test_the_reused_decode_is_the_one_that_was_stored(tmp_path: Path, fmt):
    flux = write_hfe(tmp_path / 'disk.hfe', fmt)
    opts = cli.Decode(fmt=fmt, fmt_name=FORMAT, cache_dir=tmp_path / 'cache')

    fresh, fresh_info = cli.read_one(flux, opts, None, None)
    reused, reused_info = cli.read_one(flux, opts, None, None)

    assert len(fresh) == 720
    assert reused == fresh
    assert reused_info.cached and not fresh_info.cached
    assert dataclasses.replace(reused_info, cached=False) == fresh_info


def test_a_re_dump_over_the_same_name_gets_its_own_entry(tmp_path: Path, fmt):
    """The loop re-reads a disk to the same filename; that has to miss."""
    flux = write_hfe(tmp_path / 'disk.hfe', fmt)
    opts = cli.Decode(fmt=fmt, fmt_name=FORMAT, cache_dir=tmp_path / 'cache')
    before = cli.cache_name(flux, opts, None)

    flux.write_bytes(flux.read_bytes() + b'\0')

    assert cli.cache_name(flux, opts, None) != before
    assert cli.cache_name(flux, opts, PLL('period=1:phase=10')) != \
        cli.cache_name(flux, opts, None)


def test_the_second_run_of_the_loop_reuses_the_decode(tmp_path: Path, fmt):
    flux = write_hfe(tmp_path / 'disk.hfe', fmt)
    image = write_img(tmp_path / 'dump.img', bad={3})
    out = tmp_path / 'merged.img'

    first = run(flux, image, '-o', out)
    merged = out.read_bytes()
    second = run(flux, image, '-o', out)

    assert (first.exit_code, second.exit_code) == (0, 0)
    assert 'came out of the cache' not in first.output
    assert '1 of 2 inputs came out of the cache' in second.output
    assert out.read_bytes() == merged
    assert (tmp_path / cache.DIR_NAME).is_dir()


def test_no_cache_decodes_the_capture_again(tmp_path: Path, fmt):
    flux = write_hfe(tmp_path / 'disk.hfe', fmt)
    image = write_img(tmp_path / 'dump.img', bad={3})
    out = tmp_path / 'merged.img'

    run(flux, image, '-o', out)
    again = run(flux, image, '-o', out, '--no-cache')

    assert again.exit_code == 0
    assert 'came out of the cache' not in again.output


def test_the_report_says_which_inputs_were_reused(tmp_path: Path, fmt):
    flux = write_hfe(tmp_path / 'disk.hfe', fmt)
    image = write_img(tmp_path / 'dump.img', bad={3})
    out = tmp_path / 'merged.img'

    run(flux, image, '-o', out)
    run(flux, image, '-o', out)
    data = json.loads((tmp_path / 'diskstack-report.json').read_text())

    assert [i['cached'] for i in data['inputs']] == [True, False]


def test_the_detected_format_is_remembered_between_runs(
        tmp_path: Path, fmt, monkeypatch):
    """Detection decodes the first cylinders, so a repeat run should not."""
    flux = write_hfe(tmp_path / 'disk.hfe', fmt)
    image = write_img(tmp_path / 'dump.img', bad={3})
    out = tmp_path / 'merged.img'
    first = run(flux, image, '-o', out)

    monkeypatch.setattr(formats, 'detect_format', _refuse)
    second = run(flux, image, '-o', out)

    assert (first.exit_code, second.exit_code) == (0, 0)
    fmt_line = 'Format: ' + FORMAT
    assert fmt_line in first.output and fmt_line in second.output


def test_a_capture_added_to_the_stack_is_detected_again(
        tmp_path: Path, fmt, monkeypatch):
    """A different set of inputs is a different question for detection."""
    flux = write_hfe(tmp_path / 'disk.hfe', fmt)
    image = write_img(tmp_path / 'dump.img', bad={3})
    out = tmp_path / 'merged.img'
    run(flux, image, '-o', out)

    monkeypatch.setattr(formats, 'detect_format', _refuse)
    more = run(flux, image, write_img(tmp_path / 'third.img', bad=set()),
               '-o', out)

    assert 'detected again' in str(more.exception)


def test_junk_where_a_note_should_be_is_ignored(tmp_path: Path):
    cache.note(tmp_path, 'k', {'format': FORMAT})

    assert cache.recall(tmp_path, 'k') == {'format': FORMAT}
    assert cache.recall(tmp_path, 'never-written') is None
    for junk in ('not json at all', '[1, 2]', '"ibm.360"'):
        (tmp_path / ('k' + cache.NOTE_SUFFIX)).write_text(junk)
        assert cache.recall(tmp_path, 'k') is None


def test_cache_pointing_at_a_file_is_rejected(tmp_path: Path, fmt):
    image = write_img(tmp_path / 'dump.img', bad=set())
    other = write_img(tmp_path / 'other.img', bad={3})
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('x')

    result = run(image, other, '-o', tmp_path / 'merged.img',
                 '--cache', blocked)

    assert result.exit_code == 1
    assert 'wants a directory' in result.output
