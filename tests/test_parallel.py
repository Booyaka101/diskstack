"""Decoding across processes has to give exactly what one process gives."""

from __future__ import annotations

from pathlib import Path

from diskstack import candidates, parallel


def test_default_jobs_is_sane():
    jobs = parallel.default_jobs()
    assert 1 <= jobs <= parallel.MAX_JOBS


def test_parallel_decode_matches_the_serial_decode(decoded):
    """The session fixture is decoded in parallel, so this is the baseline."""
    path = decoded.paths[0]
    done = [c for c in decoded.candidates if c.source == path]

    serial, info = candidates.load(path, decoded.fmt, jobs=1)

    assert serial == done
    assert info.candidates == len(serial)
    assert info.good == sum(1 for c in serial if c.data_crc_ok)
    assert info.revolutions == 3


def test_merge_info_folds_one_track_into_the_file(tmp_path: Path):
    whole = candidates.SourceInfo(path=tmp_path / 'a.scp', kind='scp',
                                  revolutions=2, candidates=9, good=8)
    part = candidates.SourceInfo(path=tmp_path / 'a.scp', kind='scp',
                                 revolutions=3, candidates=4, good=3,
                                 unexpected=[(0, 0, 99)])

    parallel.merge_info(whole, part)

    assert (whole.revolutions, whole.candidates, whole.good) == (3, 13, 11)
    assert whole.unexpected == [(0, 0, 99)]
