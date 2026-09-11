"""The merge itself, against three genuinely damaged flux captures."""

from __future__ import annotations

from pathlib import Path

from diskstack import formats, report, stack


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
