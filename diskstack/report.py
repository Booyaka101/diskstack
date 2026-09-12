"""The JSON report, the stdout tables, and the re-read command.

The re-read line is the point of the tool: it names only the tracks that are
still bad, so the next pass over the disk is seconds of drive time instead of
minutes, and the result goes straight back in as another input.
"""

from __future__ import annotations

import datetime
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from diskstack import __version__
from diskstack.candidates import SourceInfo
from diskstack.stack import CLEAN, MISSING, UNRESOLVED, VOTED, StackResult

SCHEMA_VERSION = 1


@dataclass
class Progress:
    """How a merge moved against the report from the run before it."""

    recovered: int
    lost: int
    still_bad: int


def read_previous(path: Path) -> Optional[dict]:
    """The report already sitting at ``path``, if it is one diskstack wrote."""
    try:
        previous = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return previous if isinstance(previous, dict) else None


def compare(previous: dict, result: StackResult) -> Optional[Progress]:
    """What this merge changed since ``previous``, or None if not comparable.

    The two runs have to cover the same sectors. Otherwise the report is of
    another disk and a count of what changed would mean nothing.
    """
    if previous.get('schema') != SCHEMA_VERSION:
        return None
    try:
        was = {(s['cyl'], s['head'], s['sec_id']):
               s['status'] in (CLEAN, VOTED)
               for s in previous['sectors']}
    except (KeyError, TypeError):
        return None
    now = {res.key: res.resolved for res in result.resolutions}
    if set(was) != set(now):
        return None
    return Progress(
        recovered=sum(1 for k, ok in now.items() if ok and not was[k]),
        lost=sum(1 for k, ok in now.items() if not ok and was[k]),
        still_bad=sum(1 for k, ok in now.items() if not ok and not was[k]))


def progress_note(progress: Optional[Progress]) -> List[str]:
    """The one line that says whether the last re-read was worth doing."""
    if progress is None:
        return []
    if progress.recovered or progress.lost:
        parts = [f'{progress.recovered} recovered']
        if progress.lost:
            parts.append(f'{progress.lost} lost')
        return [f'Since the last report: {", ".join(parts)}.']
    if progress.still_bad:
        return [f'No change since the last report: the same '
                f'{progress.still_bad} sectors are still bad. Another pass at '
                f'the same settings may not do better; --pll is the other '
                f'thing to vary.']
    return []


def _ranges(values: Sequence[int]) -> str:
    """Collapse sorted integers into Greaseweazle range syntax: 3,7-9."""
    parts, start, prev = [], None, None
    for value in values:
        if start is None:
            start = prev = value
        elif value == prev + 1:
            prev = value
        else:
            parts.append(str(start) if start == prev else f'{start}-{prev}')
            start = prev = value
    if start is not None:
        parts.append(str(start) if start == prev else f'{start}-{prev}')
    return ','.join(parts)


def reread_trackspecs(result: StackResult) -> List[str]:
    """Greaseweazle ``--tracks=`` specs covering every unresolved sector.

    Heads whose bad cylinders are identical share one spec; otherwise each
    head gets its own, so the user never re-reads a track that is already good.
    """
    per_head: Dict[int, set] = defaultdict(set)
    for res in result.resolutions:
        if not res.resolved:
            per_head[res.head].add(res.cyl)
    grouped: Dict[frozenset, List[int]] = defaultdict(list)
    for head, cyls in per_head.items():
        grouped[frozenset(cyls)].append(head)
    specs = []
    for cyls, heads in sorted(grouped.items(),
                              key=lambda kv: (sorted(kv[1]), sorted(kv[0]))):
        specs.append(f'c={_ranges(sorted(cyls))}:h={_ranges(sorted(heads))}')
    return specs


def reread_commands(result: StackResult, name: str = 'retry.scp') -> List[str]:
    """Full ``gw read`` command lines for the tracks still missing data."""
    specs = reread_trackspecs(result)
    if len(specs) == 1:
        return [f'gw read {name} --tracks={specs[0]}']
    stem, suffix = name.rsplit('.', 1) if '.' in name else (name, 'scp')
    return [f'gw read {stem}{i}.{suffix} --tracks={spec}'
            for i, spec in enumerate(specs, 1)]


def summary_line(result: StackResult) -> str:
    """The headline: how much of the disk came out readable, and from where."""
    counts = result.counts()
    total = len(result.resolutions)
    files = len({path for path, n in result.supplied.items() if n})
    parts = [f'{counts[CLEAN]} clean '
             f'(from {files} file{"s" if files != 1 else ""})']
    if counts[VOTED]:
        parts.append(f'{counts[VOTED]} recovered by vote')
    if counts[UNRESOLVED]:
        parts.append(f'{counts[UNRESOLVED]} unresolved')
    if counts[MISSING]:
        parts.append(f'{counts[MISSING]} missing')
    return f'{total} sectors: ' + ', '.join(parts)


def render_table(headers: Sequence[str],
                 rows: Sequence[Sequence[object]],
                 align: str = '') -> str:
    """Fixed-width table; ``align`` is one of 'l'/'r' per column."""
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h)
              for i, h in enumerate(headers)]
    align = (align + 'l' * len(headers))[:len(headers)]

    def line(values):
        out = []
        for value, width, how in zip(values, widths, align, strict=True):
            out.append(value.rjust(width) if how == 'r' else value.ljust(width))
        return '  ' + '  '.join(out).rstrip()

    return '\n'.join([line(headers)] + [line(r) for r in cells])


def source_table(sources: Sequence[SourceInfo], result: StackResult) -> str:
    """Per-input-file provenance: what each dump brought to the merge."""
    rows = []
    for info in sources:
        rows.append([
            info.path.name, info.kind, info.revolutions, info.candidates,
            info.good, result.supplied.get(info.path, 0),
            result.sole_source.get(info.path, 0),
        ])
    return render_table(
        ['file', 'type', 'revs', 'attempts', 'good', 'supplied', 'sole'],
        rows, align='llrrrrr')


def _track_rows(result: StackResult) -> List[List[object]]:
    rows = []
    for (cyl, head), sectors in sorted(result.by_track().items()):
        counts = {CLEAN: 0, VOTED: 0, UNRESOLVED: 0, MISSING: 0}
        for res in sectors:
            counts[res.status] += 1
        if not counts[UNRESOLVED] and not counts[MISSING]:
            continue
        bad = sorted(r.sec_id for r in sectors if not r.resolved)
        rows.append([cyl, head, counts[CLEAN], counts[VOTED],
                     counts[UNRESOLVED], counts[MISSING],
                     ','.join(str(s) for s in bad)])
    return rows


def track_table(result: StackResult) -> str:
    """Tracks that still hold a sector diskstack could not confirm."""
    return render_table(
        ['cyl', 'head', 'clean', 'voted', 'unresolved', 'missing',
         'sector ids still bad'],
        _track_rows(result), align='rrrrrrl')


def build(result: StackResult, sources: Sequence[SourceInfo],
          fmt_name: str, fmt_detail: str, output: Path,
          retry_name: str = 'retry.scp',
          progress: Optional[Progress] = None) -> dict:
    """The machine-readable report: every sector, with its provenance."""
    counts = result.counts()
    return {
        'schema': SCHEMA_VERSION,
        'tool': 'diskstack',
        'version': __version__,
        'generated': datetime.datetime.now(datetime.timezone.utc)
                             .replace(microsecond=0).isoformat(),
        'format': fmt_name,
        'format_detail': fmt_detail,
        'output': str(output),
        'inputs': [
            {
                'path': str(info.path),
                'type': info.kind,
                'revolutions': info.revolutions,
                'attempts': info.candidates,
                'good': info.good,
                'supplied': result.supplied.get(info.path, 0),
                'sole_source': result.sole_source.get(info.path, 0),
                'unexpected_sectors': len(info.unexpected),
                'bytes': info.size,
                'expected_bytes': info.expected_size or None,
                'cached': info.cached,
            }
            for info in sources
        ],
        'totals': {
            'sectors': len(result.resolutions),
            'clean': counts[CLEAN],
            'recovered_by_vote': counts[VOTED],
            'unresolved': counts[UNRESOLVED],
            'missing': counts[MISSING],
            'verified': sum(1 for r in result.resolutions if r.verified),
            'contested': sum(1 for r in result.resolutions if r.contested),
        },
        'sectors': [
            {
                'cyl': res.cyl,
                'head': res.head,
                'sec_id': res.sec_id,
                'size': res.size,
                'status': res.status,
                'attempts': res.attempts,
                'good': res.good,
                'agreement': res.agreement,
                'discarded': res.discarded,
                'unstable': res.unstable,
                'contested': res.contested,
                'verified': res.verified,
                'method': res.method or None,
                'sources': [{'path': str(c.source), 'rev': c.rev}
                            for c in res.sources],
            }
            for res in result.resolutions
        ],
        'reread': {
            'tracks': reread_trackspecs(result),
            'commands': reread_commands(result, retry_name),
        },
        'since_last_report': asdict(progress) if progress else None,
    }


def write(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


def warnings(sources: Sequence[SourceInfo], fmt_name: str) -> List[str]:
    """What the user should know about the inputs before trusting the merge."""
    out = []
    for info in sources:
        if n := len(info.unexpected):
            out.append(f'{info.path.name}: ignored {n} sector'
                       f'{"s" if n != 1 else ""} the format does not expect')
        if info.wrong_size:
            tail = ('the extra sectors were ignored'
                    if info.size > info.expected_size
                    else 'the missing tail counts as unread')
            out.append(f'{info.path.name}: {info.size} bytes where {fmt_name} '
                       f'is {info.expected_size}, so {tail}. Pass --format if '
                       f'that is the wrong disk format.')
    return out


def cache_note(sources: Sequence[SourceInfo]) -> List[str]:
    """Say when a run reused a stored decode rather than reading the flux."""
    reused = sum(1 for info in sources if info.cached)
    if not reused:
        return []
    return [f'{reused} of {len(sources)} inputs came out of the cache instead '
            f'of being decoded again. --no-cache turns that off.']


def contested_note(result: StackResult) -> List[str]:
    """Sectors where the dumps disagreed and every reading looked good.

    Nothing arbitrates this: the merge keeps the best-ranked dump's copy. It is
    what deliberate weak bits look like, and what two dumps of two different
    disks look like.
    """
    bad = [r for r in result.resolutions if r.contested]
    if not bad:
        return []
    where = ', '.join(f'c{r.cyl}:h{r.head}:s{r.sec_id}' for r in bad[:4])
    if len(bad) > 4:
        where += f', and {len(bad) - 4} more'
    return [f'{len(bad)} sector{"s" if len(bad) != 1 else ""} came out good '
            f'in more than one dump but with different bytes ({where}). '
            f"diskstack kept the best-ranked dump's copy and flagged them "
            f'contested in the report.']


def render(result: StackResult, sources: Sequence[SourceInfo],
           fmt_name: str, fmt_detail: str,
           retry_name: str = 'retry.scp',
           tables: bool = True,
           progress: Optional[Progress] = None) -> str:
    """The stdout view: summary, provenance, and what to re-read."""
    out = ([f'Format: {fmt_name} ({fmt_detail})', '', summary_line(result)]
           + progress_note(progress))
    if tables:
        out += ['', 'Sources', source_table(sources, result)]
        rows = _track_rows(result)
        if rows:
            out += ['', 'Tracks needing attention', track_table(result)]
    notes = (warnings(sources, fmt_name) + cache_note(sources)
             + contested_note(result))
    if notes:
        out += [''] + notes
    commands = reread_commands(result, retry_name)
    if commands:
        out += ['', 'Re-read just these tracks, then run diskstack again '
                    'with the new capture added:']
        out += ['  ' + c for c in commands]
        shaky = sum(1 for r in result.resolutions
                    if not r.resolved and r.unstable)
        if shaky:
            out.append(f'{shaky} of those sectors read differently on every '
                       f'pass of the same capture, which is what weak bits '
                       f'look like. More reads may not settle them.')
    else:
        unchecked = sum(1 for r in result.resolutions if not r.verified)
        if unchecked:
            out += ['', f'Nothing left to re-read, but {unchecked} of these '
                        f'sectors came from inputs that carry no check value, '
                        f'so nothing confirmed them.']
        else:
            out += ['', 'Every sector confirmed by CRC. Nothing left to '
                        're-read.']
    return '\n'.join(out)
