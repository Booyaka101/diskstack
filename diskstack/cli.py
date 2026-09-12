"""Command line: read the dumps, stack them, write the image and the report."""

from __future__ import annotations

import sys
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import click

from diskstack import (__version__, candidates, formats, parallel, report,
                       stack)
from diskstack._vendor.greaseweazle.track import PLL
from diskstack.errors import DiskStackError
from diskstack.filler import make_filler

FILL_CHOICES = ('best', 'filler', 'zero')


def _parse_plls(specs: Sequence[str]) -> List[Optional[PLL]]:
    """Turn ``--pll`` strings into PLL objects; no flag means the default PLL."""
    if not specs:
        return [None]
    plls = []
    for spec in specs:
        try:
            plls.append(PLL(spec))
        except (ValueError, AttributeError) as exc:
            raise DiskStackError(
                f"Bad --pll '{spec}'. Expected key=value pairs joined by ':', "
                f"from period=<pct>, phase=<pct>, lowpass=<us>, "
                f"e.g. --pll period=5:phase=60") from exc
    return plls


@contextmanager
def reading(path: Path, steps: int, quiet: bool):
    """Show progress while a file is read. Decoding flux takes real time."""
    if quiet:
        yield None
        return
    with click.progressbar(length=steps, file=sys.stderr,
                           label=f'Reading {path.name}') as bar:
        yield lambda: bar.update(1)


def read_one(path: Path, fmt, pll: Optional[PLL], revs: Optional[int],
             detect_filler: bool, jobs: int, on_track
             ) -> Tuple[List[candidates.Candidate], candidates.SourceInfo]:
    """One pass over one file, falling back to one process if workers fail."""
    try:
        return candidates.load(path, fmt, pll=pll, revs=revs,
                               detect_filler=detect_filler, jobs=jobs,
                               on_track=on_track)
    except (BrokenProcessPool, ImportError, OSError, RuntimeError) as exc:
        if jobs <= 1:
            raise
        click.echo(f'Parallel decode unavailable ({exc}); using one process.',
                   err=True)
        return candidates.load(path, fmt, pll=pll, revs=revs,
                               detect_filler=detect_filler, jobs=1,
                               on_track=on_track)


def load_source(path: Path, fmt, plls: Sequence[Optional[PLL]],
                revs: Optional[int], detect_filler: bool, jobs: int = 1,
                on_track=None
                ) -> Tuple[List[candidates.Candidate], candidates.SourceInfo]:
    """Read one input file, decoding flux once per requested PLL setting.

    Two PLLs disagreeing about a marginal bitcell give two independent
    attempts at the same sector, which is exactly what the vote wants.
    """
    cands, info = read_one(path, fmt, plls[0], revs, detect_filler, jobs,
                           on_track)
    if info.kind not in formats.FLUX_KINDS:
        return cands, info
    for pll in plls[1:]:
        more, extra = read_one(path, fmt, pll, revs, detect_filler, jobs,
                               on_track)
        cands += more
        parallel.merge_info(info, extra)
    return cands, info


def apply_fill(result: stack.StackResult, how: str) -> None:
    """Replace the payload of sectors diskstack could not confirm."""
    if how == 'best':
        return
    for res in result.resolutions:
        if res.resolved:
            continue
        res.data = (bytes(res.size) if how == 'zero'
                    else make_filler(res.size))


def _list_formats(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo('\n'.join(formats.supported_formats()))
    ctx.exit()


@click.command(context_settings={'help_option_names': ['-h', '--help']})
@click.argument('inputs', nargs=-1, required=True,
                type=click.Path(path_type=Path))
@click.option('-o', '--output', required=True, type=click.Path(path_type=Path),
              help='Merged sector image to write (.img, .ima, .st, .adf, '
                   '.imd).')
@click.option('-r', '--report', 'report_path', type=click.Path(path_type=Path),
              help='JSON report path  [default: diskstack-report.json beside '
                   'the output]')
@click.option('--no-report', is_flag=True, help='Do not write the JSON report.')
@click.option('-f', '--format', 'fmt_name',
              help='Disk format, e.g. ibm.720 or amiga.amigados. Detected '
                   'from the inputs when not given.')
@click.option('--list-formats', is_flag=True, is_eager=True, expose_value=False,
              callback=_list_formats,
              help='Print the disk formats diskstack understands and exit.')
@click.option('--pll', 'pll_specs', multiple=True, metavar='SPEC',
              help='Flux PLL settings, e.g. period=5:phase=60:lowpass=1.5. '
                   'Repeat to decode each capture several ways and let the '
                   'results compete.')
@click.option('--revs', type=click.IntRange(min=1), metavar='N',
              help='Use only the first N revolutions of each flux capture '
                   '[default: all of them]')
@click.option('--keep-filler', is_flag=True,
              help="Trust sectors written as '-=[BAD SECTOR]=-' filler "
                   'instead of treating them as failed reads.')
@click.option('--fill-unresolved', type=click.Choice(FILL_CHOICES),
              default='best', show_default=True,
              help='What to write for a sector no input confirmed: its best '
                   'guess, bad-sector filler, or zeroes.')
@click.option('--no-vote', is_flag=True,
              help='Skip the byte-wise majority vote; keep only sectors whose '
                   'own CRC passes.')
@click.option('-j', '--jobs', type=click.IntRange(min=0), metavar='N',
              default=0, help='Worker processes for decoding flux '
                              '[default: one per core, up to '
                              f'{parallel.MAX_JOBS}]')
@click.option('--retry-name', default='retry.scp', show_default=True,
              metavar='NAME', help='Filename used in the printed gw read '
                                   'command.')
@click.option('-q', '--quiet', is_flag=True,
              help='Print the summary and the re-read command, no tables.')
@click.version_option(__version__, '-V', '--version', prog_name='diskstack')
def main(**opts):
    """Merge several dumps of one floppy disk into the best possible image.

    INPUTS are two or more files of the same disk, any mix of flux captures
    (.scp, .raw, .hfe) and decoded sector images (.img, .ima, .st, .adf,
    .imd):

        diskstack disk_a.scp disk_b.scp disk_c.img -o merged.img

    Every sector is taken from whichever read got a passing CRC. Sectors no
    read got right are rebuilt from the attempts and kept only if the result
    satisfies a check value off the disk, and diskstack prints a gw read
    command naming only the tracks that are still bad, so the next pass over
    the disk is short.
    """
    try:
        run(**opts)
    except DiskStackError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        click.echo('Interrupted.', err=True)
        raise SystemExit(130)


def run(inputs, output, report_path, no_report, fmt_name, pll_specs, revs,
        keep_filler, fill_unresolved, no_vote, jobs, retry_name, quiet):
    paths = [Path(p) for p in inputs]
    if len(paths) < 2:
        raise DiskStackError(
            'diskstack needs two or more dumps of the same disk; '
            f'got {len(paths)}.\nWith one dump there is nothing to stack.')
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            raise DiskStackError(f'{path}: listed twice. Each dump can only '
                                 f'count once towards the vote.')
        seen.add(resolved)
        if not path.exists():
            raise DiskStackError(f'{path}: no such file')
    if output.resolve() in seen:
        raise DiskStackError(f'{output}: that is one of the inputs. Writing '
                             f'the merge over a dump would destroy it.')
    if report_path is not None and report_path.resolve() in seen:
        raise DiskStackError(f'{report_path}: that is one of the inputs.')
    if report_path is not None and report_path.resolve() == output.resolve():
        raise DiskStackError(f'{report_path}: that is the merged image. The '
                             f'report would be written over it.')
    formats.check_writable(output)

    plls = _parse_plls(pll_specs)

    if fmt_name:
        fmt, detail = formats.get_format(fmt_name), 'given with --format'
    else:
        fmt_name, detail = formats.detect_format(paths, plls[0])
        fmt = formats.get_format(fmt_name)

    all_cands, sources = [], []
    tracks = len(candidates.track_list(fmt))
    jobs = jobs or parallel.default_jobs()
    for path in paths:
        steps = tracks * len(plls) if formats.is_flux(path) else 1
        with reading(path, steps, quiet) as on_track:
            cands, info = load_source(path, fmt, plls, revs,
                                      detect_filler=not keep_filler,
                                      jobs=jobs, on_track=on_track)
            if on_track is not None and not formats.is_flux(path):
                on_track()
        all_cands += cands
        sources.append(info)

    if not all_cands:
        raise DiskStackError(
            'No sectors could be read from any input. Either these are not '
            f'{fmt_name} disks or every track failed to decode.\n'
            'Try --format, or --pll lowpass=1.5 for a weak capture.')

    expected = candidates.expected_sectors(fmt)
    sizes = {key: candidates.sector_size(fmt, *key) for key in expected}
    for info in sources:
        if info.kind in formats.FLAT_KINDS:
            info.expected_size = sum(sizes.values())
    result = stack.stack(all_cands, expected, sizes,
                         [info.path for info in sources], vote=not no_vote)
    apply_fill(result, fill_unresolved)

    formats.write_image(output, fmt, result.sector_data(),
                        result.bad_sectors())

    if not no_report:
        target = report_path or output.parent / 'diskstack-report.json'
        data = report.build(result, sources, fmt_name, detail, output,
                            retry_name)
        try:
            report.write(data, target)
        except OSError as exc:
            raise DiskStackError(f'{target}: {exc.strerror or exc}') from exc

    click.echo(report.render(result, sources, fmt_name, detail, retry_name,
                             tables=not quiet))
    click.echo('')
    written = [f'{output} ({output.stat().st_size} bytes)']
    if not no_report:
        written.append(str(target))
    click.echo('Wrote ' + ' and '.join(written))

    if result.counts()[stack.UNRESOLVED] or result.counts()[stack.MISSING]:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
