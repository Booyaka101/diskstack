"""Turn each input file into a stream of per-sector read attempts.

One :class:`Candidate` is one attempt at one sector: a single revolution of a
single flux capture, or a single sector of an already-decoded image.  Nothing
here decides which attempt wins -- that is :mod:`diskstack.stack`.

Flux captures are decoded revolution by revolution.  Greaseweazle's own codec
collapses the revolutions as it goes (``codec/ibm/ibm.py``: if two sectors
start within 1000 bitcells of each other the good one replaces the bad one)
and defaults to ``default_revs = 2``, so a five-revolution SCP normally yields
at most two views of each sector.  diskstack keeps every one of them.
"""

from __future__ import annotations

import bisect
import itertools
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Callable, Dict, Iterator, List, Optional, Sequence,
                    Set, Tuple)

from diskstack._vendor.greaseweazle import error as gw_error
from diskstack._vendor.greaseweazle.codec import codec as gw_codec
from diskstack._vendor.greaseweazle.codec.amiga import amigados
from diskstack._vendor.greaseweazle.codec.ibm import ibm
from diskstack._vendor.greaseweazle.track import PLL, PLLRevolution, PLLTrack

from diskstack.errors import DiskStackError
from diskstack.filler import is_filler

IBM_FM = 'ibm.fm'
IBM_MFM = 'ibm.mfm'
AMIGA = 'amiga'

_IBM_MODE = {ibm.Mode.FM: IBM_FM, ibm.Mode.MFM: IBM_MFM}


def verify_sector(codec: str, mark: int, data: bytes, check: bytes) -> bool:
    """True if ``check`` is the correct check value for ``data``.

    ``check`` is the trailing CRC/checksum exactly as it came off the disk, so
    this answers "would a drive have accepted this sector?" for a payload that
    diskstack has reconstructed rather than read.
    """
    if not check:
        return False
    if codec == AMIGA:
        return check == struct.pack('>I', amigados.checksum(data))
    prefix = b'\xa1\xa1\xa1' if codec == IBM_MFM else b''
    return ibm.crc16.new(prefix + bytes([mark]) + data + check).crcValue == 0


@dataclass(frozen=True)
class Candidate:
    """One read attempt at one sector."""

    cyl: int
    head: int
    sec_id: int
    data: bytes
    id_crc_ok: bool
    data_crc_ok: bool
    source: Path
    rev: int
    codec: str = IBM_MFM
    check: bytes = b''
    mark: int = ibm.Mark.DAM

    @property
    def key(self) -> Tuple[int, int, int]:
        return (self.cyl, self.head, self.sec_id)

    def recheck(self, data: bytes) -> bool:
        """True if ``data`` satisfies the check value this attempt read."""
        return verify_sector(self.codec, self.mark, data, self.check)


@dataclass
class SourceInfo:
    """What one input file turned out to be."""

    path: Path
    kind: str
    revolutions: int = 0
    candidates: int = 0
    good: int = 0
    unexpected: List[Tuple[int, int, int]] = field(default_factory=list)
    size: int = 0
    expected_size: int = 0

    @property
    def wrong_size(self) -> bool:
        """True if a sector image is not the size this disk format implies.

        The reader simply walks the format's track list, so a dump of a
        different disk is silently truncated or padded rather than rejected.
        """
        return bool(self.expected_size) and self.size != self.expected_size


def _revolution_index(bounds: Sequence[int], offset: int) -> int:
    return bisect.bisect_right(bounds, offset)


def _absolute_areas(raw: PLLTrack, mode) -> List[object]:
    """Decode a track, keeping bit offsets absolute across all revolutions.

    Upstream rebases each area onto its own revolution and then discards which
    revolution that was, but diskstack needs both the revolution number and the
    raw bits (for the on-disk CRC, which the decoder drops).  Presenting the
    track as a single revolution makes the rebase a no-op.
    """
    saved = raw.revolutions
    raw.revolutions = [PLLRevolution(sum(r.nr_bits for r in saved))]
    try:
        if mode is ibm.Mode.FM:
            return ibm.IBMTrack.fm_decode_raw(raw)
        return ibm.IBMTrack.mfm_decode_raw(raw)
    finally:
        raw.revolutions = saved


def _pll_track(flux, clock: float, time_per_rev: float,
               pll: Optional[PLL], revs: Optional[int]) -> PLLTrack:
    flux = flux.flux()
    flux.cue_at_index()
    if revs is not None and len(flux.index_list) > revs:
        flux.set_nr_revs(revs)
    return PLLTrack(time_per_rev=time_per_rev, clock=clock, data=flux, pll=pll)


def _ibm_flux_candidates(raw: PLLTrack, proto: ibm.IBMTrack, cyl: int,
                         head: int, source: Path,
                         expected: Dict[int, Tuple[int, int, int]],
                         info: SourceInfo) -> Iterator[Candidate]:
    codec_name = _IBM_MODE[proto.mode]
    bits, _ = raw.get_all_data()
    bounds = list(itertools.accumulate(r.nr_bits for r in raw.revolutions))
    for area in _absolute_areas(raw, proto.mode):
        if not isinstance(area, ibm.Sector):
            continue
        idam, dam = area.idam, area.dam
        want = expected.get(idam.r)
        # A failed IDAM CRC may be a corrupt r/c/h/n or just corrupt CRC bytes.
        # Keep the sector if the fields still name a sector this track should
        # have -- its data may be perfect -- and report the rest as unexpected.
        if want is None or (idam.c, idam.h, idam.n) != want:
            info.unexpected.append((idam.c, idam.h, idam.r))
            continue
        check = ibm.decode(bits[dam.end - 32:dam.end].tobytes())
        data = bytes(dam.data)
        info.candidates += 1
        if dam.crc == 0:
            info.good += 1
        yield Candidate(
            cyl=cyl, head=head, sec_id=idam.r, data=data,
            id_crc_ok=idam.crc == 0, data_crc_ok=dam.crc == 0,
            source=source, rev=_revolution_index(bounds, area.start),
            codec=codec_name, check=check, mark=dam.mark)


def _amiga_flux_candidates(raw: PLLTrack, proto: amigados.AmigaDOS, cyl: int,
                           head: int, source: Path,
                           info: SourceInfo) -> Iterator[Candidate]:
    """Scan a track for AmigaDOS sectors, keeping the ones that fail.

    Upstream's :meth:`AmigaDOS.decode_flux` stops at the first good copy of
    each sector and drops every sector whose checksum fails, which is exactly
    the material diskstack votes on.
    """
    bits, _ = raw.get_all_data()
    bounds = list(itertools.accumulate(r.nr_bits for r in raw.revolutions))
    tracknr = cyl * 2 + head
    for offs in bits.search(amigados.sync):
        sec = bits[offs:offs + 544 * 16].tobytes()
        if len(sec) != 1088:
            continue
        header = amigados.decode(sec[4:12])
        fmt_byte, sec_tracknr, sec_id, togo = tuple(header)
        if fmt_byte != 0xff or sec_tracknr != tracknr:
            continue
        if not (sec_id < proto.nsec and 0 < togo <= proto.nsec):
            info.unexpected.append((cyl, head, sec_id))
            continue
        label = amigados.decode(sec[12:44])
        hsum, = struct.unpack('>I', amigados.decode(sec[44:52]))
        check = amigados.decode(sec[52:60])
        data = amigados.decode(sec[60:1084])
        data_ok = check == struct.pack('>I', amigados.checksum(data))
        info.candidates += 1
        if data_ok:
            info.good += 1
        yield Candidate(
            cyl=cyl, head=head, sec_id=sec_id, data=data,
            id_crc_ok=hsum == amigados.checksum(header + label),
            data_crc_ok=data_ok, source=source,
            rev=_revolution_index(bounds, offs),
            codec=AMIGA, check=check)


def _expected_ibm(track: ibm.IBMTrack) -> Dict[int, Tuple[int, int, int]]:
    return {s.idam.r: (s.idam.c, s.idam.h, s.idam.n) for s in track.sectors}


def track_candidates(image, fmt: gw_codec.DiskDef, cyl: int, head: int,
                     source: Path, info: SourceInfo,
                     pll: Optional[PLL] = None,
                     revs: Optional[int] = None) -> List[Candidate]:
    """Every read attempt on one track of a flux capture.

    This is where a run spends nearly all of its time: the PLL walks the flux
    of every revolution.  :mod:`diskstack.parallel` calls it one track per
    worker process.
    """
    flux = image.get_track(cyl, head)
    if flux is None:
        return []
    proto = fmt.mk_track(cyl, head)
    if proto is None:
        return []
    raw = _pll_track(flux, proto.clock, proto.time_per_rev, pll, revs)
    info.revolutions = max(info.revolutions, len(raw.revolutions))
    if isinstance(proto, amigados.AmigaDOS):
        return list(_amiga_flux_candidates(raw, proto, cyl, head,
                                           source, info))
    inner = proto.raw if isinstance(proto, ibm.IBMTrack_Fixed) else proto
    return list(_ibm_flux_candidates(raw, inner, cyl, head, source,
                                     _expected_ibm(proto), info))


def flux_candidates(image, fmt: gw_codec.DiskDef, source: Path,
                    info: SourceInfo, pll: Optional[PLL] = None,
                    revs: Optional[int] = None,
                    on_track: Optional[Callable[[], None]] = None
                    ) -> Iterator[Candidate]:
    """Every sector read attempt in a flux image, across every revolution."""
    for cyl, head in track_list(fmt):
        if on_track is not None:
            on_track()
        yield from track_candidates(image, fmt, cyl, head, source, info,
                                    pll, revs)


def _img_layout(track) -> List[Tuple[int, int]]:
    """(sector id, byte length) in the order a raw sector image stores them."""
    if isinstance(track, amigados.AmigaDOS):
        return [(i, 512) for i in range(track.nsec)]
    bps = getattr(track, 'img_bps', None)
    return [(s.idam.r, bps or len(s.dam.data))
            for s in sorted(track.sectors, key=lambda s: s.idam.r)]


def backed_by_file(image, fmt: gw_codec.DiskDef,
                   size: int) -> Set[Tuple[int, int, int]]:
    """Sectors of a raw image the file actually holds the bytes for.

    A short image is zero-filled to the format's length by the reader, with
    every fabricated sector marked CRC-clean, so without this a truncated dump
    would win the merge outright with 512 bytes of nothing.
    """
    order = image.track_list() if hasattr(image, 'track_list') else None
    out, pos = set(), 0
    for cyl, head in order or track_list(fmt):
        track = image.get_track(cyl, head)
        if track is None:
            continue
        for sec_id, length in _img_layout(track):
            if pos + length <= size:
                out.add((cyl, head, sec_id))
            pos += length
    return out


def _image_track_candidates(track, cyl: int, head: int, source: Path,
                            detect_filler: bool, info: SourceInfo,
                            backed: Optional[Set[Tuple[int, int, int]]] = None
                            ) -> Iterator[Candidate]:
    def present(sec_id: int) -> bool:
        return backed is None or (cyl, head, sec_id) in backed

    if isinstance(track, amigados.AmigaDOS):
        for sec_id, sec in enumerate(track.sector):
            if not present(sec_id):
                continue
            data = bytes(512) if sec is None else bytes(sec[1])
            ok = sec is not None and not (detect_filler and is_filler(data))
            info.candidates += 1
            info.good += ok
            yield Candidate(cyl=cyl, head=head, sec_id=sec_id, data=data,
                            id_crc_ok=True, data_crc_ok=ok, source=source,
                            rev=0, codec=AMIGA)
        return
    if isinstance(track, ibm.IBMTrack_Fixed):
        mode = track.mode
    else:  # ibm.scan wrapper, or a bare IBMTrack
        mode = getattr(track, 'mode', None) or track.track.mode
    codec_name = _IBM_MODE[mode]
    for sec in track.sectors:
        if not present(sec.idam.r):
            continue
        data = bytes(sec.dam.data)
        ok = sec.dam.crc == 0 and not (detect_filler and is_filler(data))
        info.candidates += 1
        info.good += ok
        yield Candidate(cyl=cyl, head=head, sec_id=sec.idam.r, data=data,
                        id_crc_ok=sec.idam.crc == 0, data_crc_ok=ok,
                        source=source, rev=0, codec=codec_name,
                        mark=sec.dam.mark)


def image_candidates(image, fmt: gw_codec.DiskDef, source: Path,
                     info: SourceInfo, detect_filler: bool = True,
                     raw_size: Optional[int] = None) -> Iterator[Candidate]:
    """Every sector of an already-decoded image, filler counted as unread.

    Decoded images carry no CRC, so a sector is trusted unless the format can
    say otherwise (IMD's error flag) or it holds Greaseweazle's bad-sector
    filler.  With no check value, these attempts can never confirm a voted
    reconstruction -- only supply bytes to vote on.

    ``raw_size`` is the file's size for the flat formats, where the tail of a
    short file is padding rather than data.
    """
    info.revolutions = 1
    backed = (None if raw_size is None
              else backed_by_file(image, fmt, raw_size))
    for cyl, head in track_list(fmt):
        track = image.get_track(cyl, head)
        if track is None:
            continue
        yield from _image_track_candidates(track, cyl, head, source,
                                           detect_filler, info, backed)


def track_list(fmt: gw_codec.DiskDef) -> List[Tuple[int, int]]:
    """Physical (cylinder, head) pairs a disk format covers, in order."""
    return [(t.cyl, t.head) for t in fmt.tracks]


def expected_sectors(fmt: gw_codec.DiskDef) -> List[Tuple[int, int, int]]:
    """Every (cyl, head, sec_id) the format says the disk should hold."""
    out = []
    for cyl, head in track_list(fmt):
        track = fmt.mk_track(cyl, head)
        if track is None:
            continue
        if isinstance(track, amigados.AmigaDOS):
            out += [(cyl, head, i) for i in range(track.nsec)]
        else:
            out += [(cyl, head, s.idam.r) for s in track.sectors]
    return out


def sector_size(fmt: gw_codec.DiskDef, cyl: int, head: int,
                sec_id: int) -> int:
    """Payload size of one sector according to the format."""
    track = fmt.mk_track(cyl, head)
    if isinstance(track, amigados.AmigaDOS):
        return 512
    for sec in track.sectors:
        if sec.idam.r == sec_id:
            return len(sec.dam.data)
    return 512


def load(path: Path, fmt: gw_codec.DiskDef, *, pll: Optional[PLL] = None,
         revs: Optional[int] = None, detect_filler: bool = True,
         jobs: int = 1,
         on_track: Optional[Callable[[], None]] = None
         ) -> Tuple[List[Candidate], SourceInfo]:
    """Read one input file and return all of its sector read attempts."""
    from diskstack import formats, parallel  # circular at module scope

    image, kind = formats.open_image(path, fmt)
    info = SourceInfo(path=path, kind=kind,
                      size=formats.input_size(path, kind))
    try:
        if kind not in formats.FLUX_KINDS:
            flat = info.size if kind in formats.FLAT_KINDS else None
            cands = list(image_candidates(image, fmt, path, info,
                                          detect_filler=detect_filler,
                                          raw_size=flat))
        elif jobs > 1:
            cands = parallel.flux_candidates(path, fmt, info, pll=pll,
                                             revs=revs, jobs=jobs,
                                             on_track=on_track)
        else:
            cands = list(flux_candidates(image, fmt, path, info,
                                         pll=pll, revs=revs,
                                         on_track=on_track))
    except gw_error.Fatal as exc:
        raise DiskStackError(f'{path}: {exc}') from exc
    return cands, info
