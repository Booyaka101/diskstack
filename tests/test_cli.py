"""The command line, including what happens when the input is wrong."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from diskstack import __version__
from diskstack.cli import main
from test_filler import SECTORS, write_img

FLAGS = ['-o', '--output', '-r', '--report', '--no-report', '-f', '--format',
         '--list-formats', '--pll', '--revs', '--keep-filler',
         '--fill-unresolved', '--no-vote', '-j', '--jobs', '--cache',
         '--no-cache', '--retry-name', '-q', '--quiet', '-V', '--version',
         '-h', '--help']


def run(*args) -> object:
    return CliRunner().invoke(main, [str(a) for a in args])


def test_help_documents_every_flag():
    result = run('--help')
    assert result.exit_code == 0
    for flag in FLAGS:
        assert flag in result.output, f'{flag} missing from --help'


def test_version():
    result = run('--version')
    assert result.exit_code == 0
    assert __version__ in result.output


def test_list_formats():
    result = run('--list-formats')
    assert result.exit_code == 0
    names = result.output.split()
    assert 'ibm.360' in names and 'amiga.amigados' in names
    assert 'ibm.scan' not in names


def test_one_input_is_not_a_stack(tmp_path: Path):
    path = write_img(tmp_path / 'a.img', set())
    result = run(path, '-o', tmp_path / 'out.img')
    assert result.exit_code != 0
    assert 'two or more' in result.output
    assert 'Traceback' not in result.output


def test_missing_file(tmp_path: Path):
    path = write_img(tmp_path / 'a.img', set())
    result = run(path, tmp_path / 'nope.img', '-o', tmp_path / 'out.img')
    assert result.exit_code != 0
    assert 'no such file' in result.output
    assert 'Traceback' not in result.output


def test_the_same_dump_twice_is_refused(tmp_path: Path):
    path = write_img(tmp_path / 'a.img', set())
    result = run(path, path, '-o', tmp_path / 'out.img')
    assert result.exit_code != 0
    assert 'listed twice' in result.output


def test_unknown_format_lists_the_known_ones(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = write_img(tmp_path / 'b.img', set())
    result = run(a, b, '-o', tmp_path / 'out.img', '-f', 'c64.gcr')
    assert result.exit_code != 0
    assert 'ibm.360' in result.output
    assert 'Traceback' not in result.output


def test_unreadable_extension(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = tmp_path / 'b.xyz'
    b.write_bytes(b'nonsense')
    result = run(a, b, '-o', tmp_path / 'out.img', '-f', 'ibm.360')
    assert result.exit_code != 0
    assert 'unrecognised extension' in result.output


def test_diskstack_does_not_write_flux(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = write_img(tmp_path / 'b.img', set())
    result = run(a, b, '-o', tmp_path / 'out.scp')
    assert result.exit_code != 0
    assert 'cannot write that format' in result.output
    assert 'Reading' not in result.output, 'rejected only after the work'


def test_an_unwritable_output_directory_is_caught_before_reading(tmp_path):
    a = write_img(tmp_path / 'a.img', set())
    b = write_img(tmp_path / 'b.img', set())
    result = run(a, b, '-o', tmp_path / 'nope' / 'out.img')
    assert result.exit_code != 0
    assert 'output directory does not exist' in result.output
    assert 'Reading' not in result.output


def test_a_truncated_image_contributes_only_what_it_holds(tmp_path: Path):
    """The tail of a short image is the reader padding it, not disk contents."""
    a = write_img(tmp_path / 'a.img', set())
    b = tmp_path / 'b.img'
    b.write_bytes(a.read_bytes()[:1000])
    result = run(a, b, '-o', tmp_path / 'out.img', '-f', 'ibm.360',
                 '--no-report')
    assert 'Traceback' not in result.output
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines()
               if line.strip().startswith('b.img'))
    assert row.split()[2:5] == ['1', '1', '1'], row
    assert (tmp_path / 'out.img').read_bytes() == a.read_bytes()


def test_merging_two_images_and_writing_the_report(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', {3, 19})
    b = write_img(tmp_path / 'b.img', {19, 500})
    out = tmp_path / 'merged.img'
    result = run(a, b, '-o', out)

    assert result.exit_code == 2, result.output
    assert '720 sectors: 719 clean (from 2 files), 1 unresolved' in result.output
    assert 'gw read retry.scp --tracks=c=1:h=0' in result.output

    report = json.loads((tmp_path / 'diskstack-report.json').read_text())
    assert report['format'] == 'ibm.360'
    assert report['totals']['unresolved'] == 1
    assert report['reread']['tracks'] == ['c=1:h=0']

    merged = out.read_bytes()
    clean = write_img(tmp_path / 'clean.img', set()).read_bytes()
    assert len(merged) == len(clean)
    for index in range(SECTORS):
        if index == 19:
            continue
        assert merged[index * 512:(index + 1) * 512] == \
            clean[index * 512:(index + 1) * 512]


def test_clean_merge_exits_zero_and_says_so(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', {3})
    b = write_img(tmp_path / 'b.img', {500})
    result = run(a, b, '-o', tmp_path / 'merged.img', '--no-report')
    assert result.exit_code == 0, result.output
    assert 'Nothing left to re-read' in result.output
    assert not (tmp_path / 'diskstack-report.json').exists()


def test_fill_unresolved_zero(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', {19})
    b = write_img(tmp_path / 'b.img', {19})
    out = tmp_path / 'merged.img'
    run(a, b, '-o', out, '--fill-unresolved', 'zero', '--no-report')
    assert out.read_bytes()[19 * 512:20 * 512] == bytes(512)


def test_quiet_still_prints_the_re_read_line(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', {19})
    b = write_img(tmp_path / 'b.img', {19})
    result = run(a, b, '-o', tmp_path / 'merged.img', '-q', '--no-report')
    assert 'Sources' not in result.output
    assert 'gw read retry.scp --tracks=' in result.output


def test_bad_pll_spec(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = write_img(tmp_path / 'b.img', set())
    result = run(a, b, '-o', tmp_path / 'out.img', '--pll', 'wobble')
    assert result.exit_code != 0
    assert 'Bad --pll' in result.output


def test_writing_over_an_input_is_refused(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = write_img(tmp_path / 'b.img', set())
    result = run(a, b, '-o', a)
    assert result.exit_code != 0
    assert 'one of the inputs' in result.output
    assert a.read_bytes() == b.read_bytes(), 'the input is untouched'


def test_the_report_may_not_overwrite_an_input(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = write_img(tmp_path / 'b.img', set())
    result = run(a, b, '-o', tmp_path / 'out.img', '-r', b)
    assert result.exit_code != 0
    assert 'one of the inputs' in result.output


def test_an_input_of_the_wrong_size_is_called_out(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = write_img(tmp_path / 'b.img', set(), sectors=SECTORS * 2)
    result = run(a, b, '-o', tmp_path / 'out.img', '--no-report')
    assert 'b.img: 737280 bytes where ibm.360 is 368640' in result.output
    assert 'extra sectors were ignored' in result.output


def test_jobs_is_accepted(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', {3})
    b = write_img(tmp_path / 'b.img', {500})
    result = run(a, b, '-o', tmp_path / 'merged.img', '--no-report',
                 '--jobs', '2')
    assert result.exit_code == 0, result.output


def test_the_report_may_not_overwrite_the_merged_image(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = write_img(tmp_path / 'b.img', set())
    out = tmp_path / 'out.img'
    result = run(a, b, '-o', out, '-r', out)
    assert result.exit_code != 0
    assert 'that is the merged image' in result.output
    assert not out.exists()


def test_two_images_that_disagree_are_flagged_contested(tmp_path: Path):
    """Nothing in a sector image can arbitrate this, so say so out loud."""
    a = write_img(tmp_path / 'a.img', set())
    raw = bytearray(a.read_bytes())
    raw[19 * 512:20 * 512] = bytes(512)
    b = tmp_path / 'b.img'
    b.write_bytes(bytes(raw))
    out = tmp_path / 'merged.img'
    result = run(a, b, '-o', out)

    assert result.exit_code == 0, result.output
    assert '1 sector came out good in more than one dump' in result.output
    assert 'c1:h0:s2' in result.output
    assert 'confirmed by CRC' not in result.output

    report = json.loads((tmp_path / 'diskstack-report.json').read_text())
    assert report['totals'] == {'sectors': 720, 'clean': 720,
                                'recovered_by_vote': 0, 'unresolved': 0,
                                'missing': 0, 'verified': 0, 'contested': 1}
    sector = next(s for s in report['sectors'] if s['contested'])
    assert (sector['cyl'], sector['head'], sector['sec_id']) == (1, 0, 2)
    assert out.read_bytes() == a.read_bytes(), 'the best-ranked dump wins'


def test_a_bad_kryoflux_name_is_caught_before_any_input_is_read(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = tmp_path / 'dump.raw'
    b.write_bytes(b'not a stream file')
    result = run(a, b, '-o', tmp_path / 'out.img', '-f', 'ibm.360')
    assert result.exit_code != 0
    assert 'named for one track of a set' in result.output
    assert 'Reading' not in result.output


def test_an_unreadable_input_is_caught_before_any_input_is_read(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', set())
    b = tmp_path / 'b.xyz'
    b.write_bytes(b'nonsense')
    result = run(a, b, '-o', tmp_path / 'out.img', '-f', 'ibm.360')
    assert 'unrecognised extension' in result.output
    assert 'Reading' not in result.output, 'rejected only after the work'


def test_a_second_run_says_what_the_new_dump_recovered(tmp_path: Path):
    """The loop's stop condition: did the last re-read actually help?"""
    a = write_img(tmp_path / 'a.img', {19})
    b = write_img(tmp_path / 'b.img', {19})
    c = write_img(tmp_path / 'c.img', {3})
    out = tmp_path / 'merged.img'

    first = run(a, b, '-o', out)
    assert first.exit_code == 2, first.output
    assert 'Since the last report' not in first.output
    assert json.loads((tmp_path / 'diskstack-report.json').read_text()
                      )['since_last_report'] is None

    again = run(a, b, '-o', out)
    assert 'No change since the last report: the same 1 sectors' in again.output

    better = run(a, b, c, '-o', out)
    assert better.exit_code == 0, better.output
    assert 'Since the last report: 1 recovered.' in better.output
    assert json.loads((tmp_path / 'diskstack-report.json').read_text()
                      )['since_last_report'] == {'recovered': 1, 'lost': 0,
                                                 'still_bad': 0}


def test_no_report_leaves_the_previous_one_alone(tmp_path: Path):
    a = write_img(tmp_path / 'a.img', {19})
    b = write_img(tmp_path / 'b.img', {19})
    run(a, b, '-o', tmp_path / 'merged.img')
    before = (tmp_path / 'diskstack-report.json').read_bytes()

    result = run(a, b, '-o', tmp_path / 'merged.img', '--no-report')
    assert 'Since the last report' not in result.output
    assert (tmp_path / 'diskstack-report.json').read_bytes() == before
