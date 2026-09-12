"""The JSON report and the stdout view."""

from __future__ import annotations

import json
from pathlib import Path

from diskstack import report, stack
from diskstack.candidates import SourceInfo
from diskstack._vendor.greaseweazle.tools.util import TrackSet


def make_result(bad: dict) -> stack.StackResult:
    """A 2-cylinder, 2-head, 4-sector disk; ``bad`` maps (cyl, head) to ids."""
    source = Path('a.scp')
    resolutions = []
    for cyl in range(2):
        for head in range(2):
            for sec in range(1, 5):
                unresolved = sec in bad.get((cyl, head), ())
                resolutions.append(stack.Resolution(
                    cyl=cyl, head=head, sec_id=sec, size=512,
                    data=bytes(512),
                    status=stack.UNRESOLVED if unresolved else stack.CLEAN,
                    attempts=2, good=0 if unresolved else 2, agreement=2,
                    verified=not unresolved,
                    sources=[] if unresolved
                            else [stack.Contribution(source, 0)]))
    supplied = sum(1 for r in resolutions if r.resolved)
    return stack.StackResult(resolutions=resolutions, ranking=[source],
                             supplied={source: supplied},
                             sole_source={source: supplied})


def sources() -> list:
    return [SourceInfo(path=Path('a.scp'), kind='scp', revolutions=3,
                       candidates=48, good=44)]


def test_ranges():
    assert report._ranges([]) == ''
    assert report._ranges([4]) == '4'
    assert report._ranges([17, 18, 19]) == '17-19'
    assert report._ranges([0, 3, 4, 5, 9]) == '0,3-5,9'


def test_trackspecs_name_only_the_bad_tracks():
    result = make_result({(0, 1): [2], (1, 1): [3]})
    assert report.reread_trackspecs(result) == ['c=0-1:h=1']


def test_heads_with_different_damage_get_their_own_spec():
    result = make_result({(0, 0): [1], (1, 1): [3]})
    specs = report.reread_trackspecs(result)
    assert specs == ['c=0:h=0', 'c=1:h=1']
    assert report.reread_commands(result) == [
        'gw read retry1.scp --tracks=c=0:h=0',
        'gw read retry2.scp --tracks=c=1:h=1',
    ]


def test_every_trackspec_parses_as_greaseweazle_syntax():
    for bad in ({(0, 1): [2], (1, 1): [3]}, {(0, 0): [1], (1, 1): [3]},
                {(1, 0): [1, 2, 3, 4]}):
        for spec in report.reread_trackspecs(make_result(bad)):
            assert str(TrackSet(spec)) == spec


def test_a_clean_disk_has_nothing_to_re_read():
    result = make_result({})
    assert report.reread_trackspecs(result) == []
    assert report.reread_commands(result) == []
    assert 'Nothing left to re-read' in report.render(
        result, sources(), 'ibm.360', 'test')


def test_summary_line():
    result = make_result({(0, 1): [2]})
    assert report.summary_line(result) == \
        '16 sectors: 15 clean (from 1 file), 1 unresolved'


def test_report_round_trips_through_json(tmp_path: Path):
    result = make_result({(0, 1): [2]})
    built = report.build(result, sources(), 'ibm.360', 'test',
                         tmp_path / 'out.img')
    path = tmp_path / 'diskstack-report.json'
    report.write(built, path)
    loaded = json.loads(path.read_text())

    assert loaded == built
    assert loaded['schema'] == report.SCHEMA_VERSION
    assert loaded['totals'] == {'sectors': 16, 'clean': 15,
                                'recovered_by_vote': 0, 'unresolved': 1,
                                'missing': 0, 'verified': 15,
                                'contested': 0}
    assert len(loaded['sectors']) == 16
    assert loaded['reread']['tracks'] == ['c=0:h=1']
    assert loaded['inputs'][0]['path'] == 'a.scp'


def test_rendered_table_lists_the_bad_sectors():
    text = report.render(make_result({(1, 0): [2, 4]}), sources(),
                         'ibm.360', 'test')
    assert 'Tracks needing attention' in text
    assert '2,4' in text
    assert 'gw read retry.scp --tracks=c=1:h=0' in text


def test_quiet_drops_the_tables_but_keeps_the_point():
    text = report.render(make_result({(1, 0): [2]}), sources(), 'ibm.360',
                         'test', tables=False)
    assert 'Tracks needing attention' not in text
    assert 'gw read retry.scp --tracks=c=1:h=0' in text


def test_weak_bits_are_called_out_next_to_the_re_read_command():
    result = make_result({(0, 0): [1]})
    for res in result.resolutions:
        res.unstable = not res.resolved

    text = report.render(result, [], 'ibm.360', 'given with --format')

    assert 'gw read retry.scp' in text
    assert '1 of those sectors read differently on every pass' in text


def test_sectors_the_dumps_disagreed_about_are_called_out():
    result = make_result({})
    for res in result.resolutions[:2]:
        res.contested = True

    text = report.render(result, sources(), 'ibm.360', 'test')

    assert '2 sectors came out good in more than one dump' in text
    assert 'c0:h0:s1, c0:h0:s2' in text


def test_a_merge_of_sector_images_does_not_claim_a_crc_confirmed_it():
    """Only flux and IMD carry a check value; .img and .adf carry none."""
    result = make_result({})
    for res in result.resolutions:
        res.verified = False

    text = report.render(result, sources(), 'ibm.360', 'test')

    assert 'confirmed by CRC' not in text
    assert '16 of these sectors came from inputs that carry no check value'         in text


def test_a_steady_bad_sector_gets_no_weak_bit_note():
    text = report.render(make_result({(0, 0): [1]}), [], 'ibm.360', 'detail')
    assert 'weak bits' not in text


def test_only_a_sector_image_reports_an_expected_size():
    flux = sources()[0]
    flux.size = 19327821
    img = SourceInfo(path=Path('b.img'), kind='img', size=368640,
                     expected_size=368640)

    built = report.build(make_result({}), [flux, img], 'ibm.360', 'test',
                         Path('out.img'))

    assert [i['bytes'] for i in built['inputs']] == [19327821, 368640]
    assert [i['expected_bytes'] for i in built['inputs']] == [None, 368640]
