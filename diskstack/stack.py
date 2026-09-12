"""Decide, sector by sector, which read attempt wins.

Attempts are grouped by ``(cylinder, head, sector id)`` read out of the
decoded ID address mark -- never by position in the flux.  Greaseweazle's
in-codec merge pairs sectors up when their start offsets are within 1000
bitcells (``codec/ibm/ibm.py``), which works across revolutions of one capture
and cannot work across two captures, where index alignment and motor speed
both differ.

Three tiers per sector:

1. any attempt whose own CRC or checksum passes wins outright;
2. otherwise the attempts are recombined into candidate payloads -- a byte-wise
   majority, then one vote per input file, then each read's own bytes -- and the
   first payload that satisfies a check value off the disk wins;
3. otherwise the largest cluster of attempts that agree exactly is emitted and
   the sector is reported unresolved.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Sequence, Tuple

from diskstack.candidates import Candidate, verify_sector
from diskstack.filler import make_filler

CLEAN = 'clean'
VOTED = 'recovered_by_vote'
UNRESOLVED = 'unresolved'
MISSING = 'missing'

STATUSES = (CLEAN, VOTED, UNRESOLVED, MISSING)

# How a tier 2 payload was put back together, best evidence first.
METHODS = ('majority', 'per_source_majority', 'cross_check')


@dataclass(frozen=True)
class Contribution:
    """One read attempt that produced the bytes diskstack chose."""

    source: Path
    rev: int


@dataclass
class Resolution:
    """The outcome for one sector of the disk."""

    cyl: int
    head: int
    sec_id: int
    size: int
    data: bytes
    status: str
    attempts: int = 0
    good: int = 0
    agreement: int = 0
    sources: List[Contribution] = field(default_factory=list)
    discarded: int = 0
    unstable: bool = False
    method: str = ''
    verified: bool = False
    contested: bool = False

    @property
    def key(self) -> Tuple[int, int, int]:
        return (self.cyl, self.head, self.sec_id)

    @property
    def resolved(self) -> bool:
        return self.status in (CLEAN, VOTED)


@dataclass
class StackResult:
    """Everything the merge decided, ready for writing out and reporting."""

    resolutions: List[Resolution]
    ranking: List[Path]
    supplied: Dict[Path, int] = field(default_factory=dict)
    sole_source: Dict[Path, int] = field(default_factory=dict)

    def counts(self) -> Dict[str, int]:
        counts = {s: 0 for s in STATUSES}
        for res in self.resolutions:
            counts[res.status] += 1
        return counts

    def by_track(self) -> Dict[Tuple[int, int], List[Resolution]]:
        tracks: Dict[Tuple[int, int], List[Resolution]] = defaultdict(list)
        for res in self.resolutions:
            tracks[res.cyl, res.head].append(res)
        return dict(tracks)

    def sector_data(self) -> Dict[Tuple[int, int], Dict[int, bytes]]:
        out: Dict[Tuple[int, int], Dict[int, bytes]] = defaultdict(dict)
        for res in self.resolutions:
            out[res.cyl, res.head][res.sec_id] = res.data
        return dict(out)

    def bad_sectors(self) -> Dict[Tuple[int, int], List[int]]:
        out: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for res in self.resolutions:
            if not res.resolved:
                out[res.cyl, res.head].append(res.sec_id)
        return dict(out)


def rank_sources(candidates: Iterable[Candidate],
                 order: Sequence[Path]) -> List[Path]:
    """Input files, best first, by how many distinct sectors each read cleanly.

    This is the tie-break for the byte vote: when two byte values are equally
    popular, the one from the dump that read the rest of the disk best wins.
    """
    good: Dict[Path, set] = {p: set() for p in order}
    for cand in candidates:
        if cand.data_crc_ok:
            good.setdefault(cand.source, set()).add(cand.key)
    position = {p: i for i, p in enumerate(order)}
    return sorted(good, key=lambda p: (-len(good[p]), position.get(p, 0)))


def largest_cluster(values: Sequence[bytes]) -> Tuple[bytes, int]:
    """Largest set of byte-identical payloads, ties to the earliest value.

    ``values`` must already be ordered best source first.
    """
    counts = Counter(values)
    best = max(counts.values())
    tied = {value for value, n in counts.items() if n == best}
    if len(tied) == 1:
        return tied.pop(), best
    return next(v for v in values if v in tied), best


def majority_bytes(values: Sequence[bytes]) -> bytes:
    """Byte-wise majority over equal-length payloads.

    Per byte position the most common value wins; a tie goes to the earliest
    payload holding one of the tied values, so ``values`` must already be
    ordered best source first.
    """
    out = bytearray()
    for column in zip(*values):
        if len(set(column)) == 1:
            out.append(column[0])
            continue
        counts = Counter(column)
        best = max(counts.values())
        winners = {value for value, n in counts.items() if n == best}
        if len(winners) == 1:
            out.append(winners.pop())
        else:
            out.append(next(v for v in column if v in winners))
    return bytes(out)


def _check_trials(group: Sequence[Candidate]) -> List[Tuple[int, bytes]]:
    """(mark, check) pairs a voted payload can be tested against.

    The check bytes come off the same damaged track as the payload, so the
    vote covers them too and the voted value is tried alongside the ones that
    were actually read.
    """
    trials: List[Tuple[int, bytes]] = []
    seen = set()
    for cand in group:
        if cand.check and (cand.mark, cand.check) not in seen:
            seen.add((cand.mark, cand.check))
            trials.append((cand.mark, cand.check))
    with_check = [c for c in group if c.check]
    if len(with_check) > 1:
        size = Counter(len(c.check) for c in with_check).most_common(1)[0][0]
        same = [c for c in with_check if len(c.check) == size]
        voted = (Counter(c.mark for c in same).most_common(1)[0][0],
                 majority_bytes([c.check for c in same]))
        if voted not in seen:
            trials.append(voted)
    return trials


def _per_source(group: Sequence[Candidate]) -> List[bytes]:
    """One payload per input file, best source first."""
    by_source: Dict[Path, List[bytes]] = {}
    for cand in group:
        by_source.setdefault(cand.source, []).append(cand.data)
    return [majority_bytes(v) for v in by_source.values()]


def reconstructions(group: Sequence[Candidate]) -> Iterator[Tuple[str, bytes]]:
    """Payloads worth testing against the check values that came off the disk.

    The plain majority is what a vote means and comes first.  The other two
    cover what it cannot express: a capture with more revolutions than the
    rest drowning out the sources that read the sector correctly, and a read
    whose payload is fine but whose own check bytes were the damaged part.
    """
    payloads = [c.data for c in group]
    plain, per_source = METHODS[:2]
    seen = set()
    for method, data in [(plain, majority_bytes(payloads)),
                         (per_source, majority_bytes(_per_source(group)))]:
        if data not in seen:
            seen.add(data)
            yield method, data
    for data in payloads:
        if data not in seen:
            seen.add(data)
            yield METHODS[2], data


def unstable(group: Sequence[Candidate]) -> bool:
    """True if some input read this sector two different ways on its own.

    Two revolutions of one capture disagreeing is the signature of weak bits,
    which are deliberate on protected disks and do not settle down however
    many more times the disk is read.
    """
    per_source: Dict[Path, set] = defaultdict(set)
    for cand in group:
        per_source[cand.source].add(cand.data)
    return any(len(seen) > 1 for seen in per_source.values())


def resolve(key: Tuple[int, int, int], size: int,
            group: Sequence[Candidate], vote: bool = True) -> Resolution:
    """Apply the three tiers to one sector's read attempts."""
    cyl, head, sec_id = key
    usable = [c for c in group if len(c.data) == size]
    res = Resolution(cyl=cyl, head=head, sec_id=sec_id, size=size,
                     data=make_filler(size), status=MISSING,
                     attempts=len(group), discarded=len(group) - len(usable))
    if not usable:
        return res
    res.good = sum(1 for c in usable if c.data_crc_ok)
    res.unstable = unstable(usable)

    clean = [c for c in usable if c.data_crc_ok]
    if clean:
        # A read that satisfied a check value off the disk outranks one that
        # was merely never contradicted, which is all a sector image can say.
        verified = [c for c in clean if c.check]
        data, _ = largest_cluster([c.data for c in verified or clean])
        matched = [c for c in clean if c.data == data]
        res.data, res.status = data, CLEAN
        res.agreement = len(matched)
        res.verified = any(c.check for c in matched)
        res.contested = len(matched) < len(clean)
        res.sources = [Contribution(c.source, c.rev) for c in matched]
        return res

    if vote and len(usable) > 1:
        codec = usable[0].codec
        trials = _check_trials(usable)
        for method, data in reconstructions(usable):
            if any(verify_sector(codec, mark, data, check)
                   for mark, check in trials):
                res.data, res.status, res.method = data, VOTED, method
                res.verified = True
                matched = [c for c in usable if c.data == data]
                res.agreement = len(matched)
                res.sources = [Contribution(c.source, c.rev)
                               for c in matched or usable]
                return res

    data, agree = largest_cluster([c.data for c in usable])
    res.data, res.status, res.agreement = data, UNRESOLVED, agree
    res.sources = [Contribution(c.source, c.rev) for c in usable
                   if c.data == data]
    return res


def stack(candidates: Iterable[Candidate],
          expected: Sequence[Tuple[int, int, int]],
          sizes: Dict[Tuple[int, int, int], int],
          order: Sequence[Path],
          vote: bool = True) -> StackResult:
    """Merge every read attempt into one resolution per expected sector."""
    candidates = list(candidates)
    ranking = rank_sources(candidates, order)
    rank = {path: i for i, path in enumerate(ranking)}

    groups: Dict[Tuple[int, int, int], List[Candidate]] = defaultdict(list)
    for cand in candidates:
        groups[cand.key].append(cand)
    for group in groups.values():
        group.sort(key=lambda c: (rank.get(c.source, len(rank)), c.rev))

    resolutions = [resolve(key, sizes[key], groups.get(key, ()), vote=vote)
                   for key in expected]

    supplied = {path: 0 for path in order}
    sole = {path: 0 for path in order}
    for res in resolutions:
        if not res.resolved:
            continue
        paths = {c.source for c in res.sources}
        for path in paths:
            supplied[path] = supplied.get(path, 0) + 1
        if len(paths) == 1:
            path = next(iter(paths))
            sole[path] = sole.get(path, 0) + 1

    return StackResult(resolutions=resolutions, ranking=ranking,
                       supplied=supplied, sole_source=sole)
