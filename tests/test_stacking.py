"""The merge itself, against three genuinely damaged flux captures."""

from __future__ import annotations

from pathlib import Path

from diskstack import candidates, formats, report, stack
from diskstack._vendor.greaseweazle.image.scp import SCP

from test_images import write_tracks


def test_three_captures_rebuild_the_original(decoded, tmp_path: Path):
    result = decoded.stacked()
    out = tmp_path / 'merged.img'
    formats.write_image(out, decoded.fmt, result.sector_data(),
                        result.bad_sectors())
    assert out.read_bytes() == decoded.source_image.read_bytes()


def test_every_tier_is_exercised(decoded):
    counts = decoded.stacked().counts()
    assert counts[stack.UNRESOLVED] == 0
    assert counts[stack.MISSING] == 0
    assert counts[stack.VOTED] > 0, 'fixtures no longer need the byte vote'
    assert counts[stack.CLEAN] > 0


def test_the_recovered_sectors_say_how_they_were_rebuilt(decoded):
    recovered = [r for r in decoded.stacked().resolutions
                 if r.status == stack.VOTED]
    assert recovered
    assert {r.method for r in recovered} <= set(stack.METHODS)
    assert all(r.method for r in recovered)


def test_no_sector_good_in_an_input_comes_out_bad(decoded):
    """A sector any input read cleanly must survive the merge untouched."""
    known = {}
    for cand in decoded.candidates:
        if cand.data_crc_ok:
            known.setdefault(cand.key, cand.data)
    result = decoded.stacked()
    for res in result.resolutions:
        if res.key in known:
            assert res.resolved, f'{res.key} was clean in an input'
            assert res.data == known[res.key], f'{res.key} changed'


def test_two_captures_leave_a_short_re_read_list(decoded):
    """The loop the tool exists for: two dumps, then re-read only what is bad."""
    two = decoded.stacked('capture_a.scp', 'capture_b.scp')
    assert two.counts()[stack.UNRESOLVED] > 0
    specs = report.reread_trackspecs(two)
    assert specs == ['c=17-19:h=0']

    bad_tracks = set(two.bad_sectors())
    three = decoded.stacked()
    assert three.counts()[stack.UNRESOLVED] == 0
    for (cyl, head) in bad_tracks:
        assert (cyl, head) not in three.bad_sectors()


def test_a_three_track_retry_capture_finishes_the_merge(decoded, tmp_path: Path):
    """The other half of the loop, and the reason the tool prints a trackspec.

    A re-read of only the bad tracks has to stack with the full captures the
    same way another full dump would.
    """
    names = ('capture_a.scp', 'capture_b.scp')
    bad = sorted(set(decoded.stacked(*names).bad_sectors()))
    assert bad == [(17, 0), (18, 0), (19, 0)]

    full = next(p for p in decoded.paths if p.name == 'capture_c.scp')
    retry = write_tracks(SCP, tmp_path / 'retry.scp', full, bad)
    assert retry.stat().st_size * 10 < full.stat().st_size

    extra, _ = candidates.load(retry, decoded.fmt)
    assert {(c.cyl, c.head) for c in extra} == set(bad)

    result = decoded.stacked(*names, extra=extra)
    assert result.counts()[stack.UNRESOLVED] == 0
    out = tmp_path / 'repaired.img'
    formats.write_image(out, decoded.fmt, result.sector_data(),
                        result.bad_sectors())
    assert out.read_bytes() == decoded.source_image.read_bytes()


def test_the_re_read_command_is_valid_greaseweazle_syntax(decoded):
    from diskstack._vendor.greaseweazle.tools.util import TrackSet

    two = decoded.stacked('capture_a.scp', 'capture_b.scp')
    for spec in report.reread_trackspecs(two):
        assert str(TrackSet(spec)) == spec
    commands = report.reread_commands(two)
    assert commands == ['gw read retry.scp --tracks=c=17-19:h=0']


def test_no_vote_leaves_the_hard_sectors_unresolved(decoded):
    voted = decoded.stacked().counts()
    plain = decoded.stacked(vote=False).counts()
    assert plain[stack.UNRESOLVED] == voted[stack.VOTED]
    assert plain[stack.CLEAN] == voted[stack.CLEAN]


def test_every_revolution_is_used(decoded):
    """Two revolutions is upstream's default; diskstack wants all of them."""
    revs = {c.rev for c in decoded.candidates}
    assert revs == {0, 1, 2}
