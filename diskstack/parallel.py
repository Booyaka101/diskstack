"""Decode a flux capture one track per worker process.

Nearly all of a run is the PLL walking flux, and tracks are independent, so
this is the one place worth parallelising.  Results are collected in track
order, so a parallel run produces byte-identical candidates to a serial one.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from diskstack._vendor.greaseweazle.codec import codec as gw_codec
from diskstack._vendor.greaseweazle.track import PLL
from diskstack.candidates import (Candidate, SourceInfo, track_candidates,
                                  track_list)

MAX_JOBS = 8

_worker: dict = {}


def default_jobs() -> int:
    """Worker count to use when the user did not ask for one."""
    return max(1, min(os.cpu_count() or 1, MAX_JOBS))


def _init(path: Path, fmt: gw_codec.DiskDef, pll: Optional[PLL],
          revs: Optional[int]) -> None:
    from diskstack import formats

    image, _ = formats.open_image(path, fmt)
    _worker.update(path=path, fmt=fmt, image=image, pll=pll, revs=revs)


def _decode(track: Tuple[int, int]) -> Tuple[List[Candidate], SourceInfo]:
    cyl, head = track
    info = SourceInfo(path=_worker['path'], kind='scp')
    cands = track_candidates(_worker['image'], _worker['fmt'], cyl, head,
                             _worker['path'], info, _worker['pll'],
                             _worker['revs'])
    return cands, info


def merge_info(into: SourceInfo, part: SourceInfo) -> None:
    """Fold one track's tally into the tally for the whole file."""
    into.revolutions = max(into.revolutions, part.revolutions)
    into.candidates += part.candidates
    into.good += part.good
    into.unexpected += part.unexpected


def flux_candidates(path: Path, fmt: gw_codec.DiskDef, info: SourceInfo, *,
                    pll: Optional[PLL] = None, revs: Optional[int] = None,
                    jobs: int = 2,
                    on_track: Optional[Callable[[], None]] = None
                    ) -> List[Candidate]:
    """Same result as :func:`diskstack.candidates.flux_candidates`, in
    parallel."""
    tracks = track_list(fmt)
    out: List[Candidate] = []
    with ProcessPoolExecutor(max_workers=min(jobs, len(tracks) or 1),
                             initializer=_init,
                             initargs=(path, fmt, pll, revs)) as pool:
        for cands, part in pool.map(_decode, tracks, chunksize=2):
            merge_info(info, part)
            out += cands
            if on_track is not None:
                on_track()
    return out
