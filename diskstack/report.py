"""The JSON report, the stdout tables, and the re-read command.

The re-read line is the point of the tool: it names only the tracks that are
still bad, so the next pass over the disk is seconds of drive time instead of
minutes, and the result goes straight back in as another input.
"""

from __future__ import annotations

import datetime
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Sequence

from diskstack import __version__
from diskstack.candidates import SourceInfo
from diskstack.stack import CLEAN, MISSING, UNRESOLVED, VOTED, StackResult

SCHEMA_VERSION = 1


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
        for value, width, how in zip(values, widths, align):
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
          retry_name: str = 'retry.scp') -> dict:
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
            }
            for info in sources
        ],
        'totals': {
            'sectors': len(result.resolutions),
            'clean': counts[CLEAN],
            'recovered_by_vote': counts[VOTED],
            'unresolved': counts[UNRESOLVED],
            'missing': counts[MISSING],
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
                'sources': [{'path': str(c.source), 'rev': c.rev}
                            for c in res.sources],
            }
            for res in result.resolutions
        ],
        'reread': {
            'tracks': reread_trackspecs(result),
            'commands': reread_commands(result, retry_name),
        },
    }


def write(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


def render(result: StackResult, sources: Sequence[SourceInfo],
           fmt_name: str, fmt_detail: str,
           retry_name: str = 'retry.scp',
           tables: bool = True) -> str:
    """The stdout view: summary, provenance, and what to re-read."""
    out = [f'Format: {fmt_name} ({fmt_detail})', '', summary_line(result)]
    if tables:
        out += ['', 'Sources', source_table(sources, result)]
        rows = _track_rows(result)
        if rows:
            out += ['', 'Tracks needing attention', track_table(result)]
    unexpected = [(info, n) for info in sources
                  if (n := len(info.unexpected))]
    for info, n in unexpected:
        out.append(f'{info.path.name}: ignored {n} sector'
                   f'{"s" if n != 1 else ""} the format does not expect')
    commands = reread_commands(result, retry_name)
    if commands:
        out += ['', 'Re-read just these tracks, then run diskstack again '
                    'with the new capture added:']
        out += ['  ' + c for c in commands]
    else:
        out += ['', 'Every sector confirmed by CRC. Nothing left to re-read.']
    return '\n'.join(out)
