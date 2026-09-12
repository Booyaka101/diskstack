"""Disk formats, image files, and working out which is which.

diskstack needs one disk format for the whole run: it is the list of sectors
the disk is supposed to have, and without it a merge cannot say what is
missing.  Auto-detection reads the geometry out of whichever input can state
it most directly -- an IMG boot sector, an IMD track header, or failing that
a scan of the first few cylinders of a flux capture.
"""

from __future__ import annotations

import functools
import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from diskstack._vendor.greaseweazle import error as gw_error
from diskstack._vendor.greaseweazle.codec import codec as gw_codec
from diskstack._vendor.greaseweazle.codec.amiga import amigados
from diskstack._vendor.greaseweazle.codec.ibm import ibm
from diskstack._vendor.greaseweazle.image.adf import ADF
from diskstack._vendor.greaseweazle.image.hfe import HFE
from diskstack._vendor.greaseweazle.image.imd import IMD
from diskstack._vendor.greaseweazle.image.img import IMG
from diskstack._vendor.greaseweazle.image.kryoflux import KryoFlux
from diskstack._vendor.greaseweazle.image.scp import SCP
from diskstack._vendor.greaseweazle.track import PLL

from diskstack.errors import DiskStackError

# v1 is IBM FM/MFM and AmigaDOS MFM only.  ibm.scan is excluded on purpose:
# it makes each track its own format, so it cannot state what the disk should
# contain, which is the one thing a merge needs.
SUPPORTED_PREFIXES = ('ibm.', 'amiga.')
EXCLUDED_FORMATS = frozenset({'ibm.scan'})
SCAN_CYLS = 5
PROBE_CYLS = 84

READERS = {'.scp': (SCP, 'scp'), '.img': (IMG, 'img'), '.ima': (IMG, 'img'),
           '.adf': (ADF, 'adf'), '.imd': (IMD, 'imd'), '.st': (IMG, 'img'),
           '.raw': (KryoFlux, 'kryoflux'), '.hfe': (HFE, 'hfe')}
WRITERS = {'.img': IMG, '.ima': IMG, '.adf': ADF, '.imd': IMD, '.st': IMG}

# Inputs that have to be decoded, and inputs that are a flat run of sector
# payloads with no per-sector framing at all.
FLUX_KINDS = frozenset({'scp', 'kryoflux', 'hfe'})
FLAT_KINDS = frozenset({'img', 'adf'})

# Every standard geometry this tool supports has a distinct raw image size.
SIZE_FORMATS = {
    163840: 'ibm.160', 184320: 'ibm.180', 327680: 'ibm.320',
    368640: 'ibm.360', 737280: 'ibm.720', 819200: 'ibm.800',
    901120: 'amiga.amigados', 1228800: 'ibm.1200', 1474560: 'ibm.1440',
    1720320: 'ibm.1680', 1802240: 'amiga.amigados_hd', 2949120: 'ibm.2880',
}


def supported_formats() -> List[str]:
    """Names of the disk formats diskstack can merge."""
    all_fmts = gw_codec.get_all_formats('', gw_codec.DiskDef_File(name=None))
    return sorted(f for f in all_fmts
                  if f.startswith(SUPPORTED_PREFIXES)
                  and f not in EXCLUDED_FORMATS)


def get_format(name: str) -> gw_codec.DiskDef:
    """Look up a disk format by name, or explain why it is not usable."""
    if name in EXCLUDED_FORMATS or not name.startswith(SUPPORTED_PREFIXES):
        raise DiskStackError(
            f"Unsupported format '{name}'. diskstack v1 handles IBM FM/MFM "
            f"and AmigaDOS MFM only.\nSupported: "
            f"{', '.join(supported_formats())}")
    try:
        fmt = gw_codec.get_diskdef(name)
    except gw_error.Fatal as exc:
        raise DiskStackError(f"Bad format '{name}': {exc}") from exc
    if fmt is None:
        raise DiskStackError(
            f"Unknown format '{name}'.\nSupported: "
            f"{', '.join(supported_formats())}")
    return fmt


class Geometry:
    """Enough shape to pick a disk format: cylinders, heads, sectors, size."""

    def __init__(self, cyls: int, heads: int, nsec: int, secsz: int,
                 amiga: bool = False) -> None:
        self.cyls, self.heads = cyls, heads
        self.nsec, self.secsz, self.amiga = nsec, secsz, amiga

    def __str__(self) -> str:
        kind = 'AmigaDOS' if self.amiga else 'IBM'
        return (f'{kind} {self.cyls} cyls, {self.heads} heads, '
                f'{self.nsec} sectors of {self.secsz} bytes')


@functools.lru_cache(maxsize=None)
def _format_geometry(name: str) -> Optional[Geometry]:
    fmt = gw_codec.get_diskdef(name)
    track = fmt.mk_track(0, 0)
    if track is None:
        return None
    if isinstance(track, amigados.AmigaDOS):
        return Geometry(fmt.cyls, fmt.heads, track.nsec, 512, amiga=True)
    sizes = {len(s.dam.data) for s in track.sectors}
    if len(sizes) != 1:
        return None
    return Geometry(fmt.cyls, fmt.heads, len(track.sectors), sizes.pop())


def match_geometry(geom: Geometry) -> Optional[str]:
    """Name of the supported format matching ``geom`` exactly, if any."""
    for name in supported_formats():
        other = _format_geometry(name)
        if other is None:
            continue
        if (other.cyls, other.heads, other.nsec, other.secsz, other.amiga) == \
           (geom.cyls, geom.heads, geom.nsec, geom.secsz, geom.amiga):
            return name
    return None


def _bpb_geometry(dat: bytes) -> Optional[Geometry]:
    """Geometry from a DOS BIOS Parameter Block, if the boot sector has one."""
    if len(dat) < 512:
        return None
    bps, = struct.unpack('<H', dat[11:13])
    total, = struct.unpack('<H', dat[19:21])
    spt, heads = struct.unpack('<HH', dat[24:28])
    if bps not in (128, 256, 512, 1024) or spt == 0 or heads not in (1, 2):
        return None
    if total == 0 or total * bps != len(dat) or total % (spt * heads):
        return None
    return Geometry(total // (spt * heads), heads, spt, bps)


def _flux_extent(image) -> Optional[Tuple[int, int]]:
    """(cylinders, heads) a flux container holds, found by probing for tracks.

    Flux readers do not agree on how to list their tracks, and the KryoFlux
    one cannot list them at all: it goes looking for a file per track.
    """
    top = next((cyl for cyl in reversed(range(PROBE_CYLS))
                if image.get_track(cyl, 0) is not None
                or image.get_track(cyl, 1) is not None), None)
    if top is None:
        return None
    return top + 1, 2 if image.get_track(0, 1) is not None else 1


def _scan_flux_geometry(path: Path, pll: Optional[PLL]) -> Optional[Geometry]:
    """Decode the first few cylinders of a flux capture to see what is there.

    A track with a damaged sector reports one sector fewer than the disk
    really has, which would pick a smaller format, so several tracks are
    scanned and the fullest one wins.
    """
    reader, _ = READERS[path.suffix.lower()]
    image = reader.from_file(str(path), None, {})
    extent = _flux_extent(image)
    if extent is None:
        return None
    cyls, heads = extent

    scan_fmt = gw_codec.get_diskdef('ibm.scan')
    best = None
    for cyl in range(min(SCAN_CYLS, cyls)):
        for head in range(heads):
            flux = image.get_track(cyl, head)
            if flux is None:
                continue
            track = scan_fmt.mk_track(cyl, head)
            track.decode_flux(flux)
            inner = track.track
            sizes = {len(s.dam.data) for s in inner.sectors}
            if inner.sectors and len(sizes) == 1:
                geom = Geometry(cyls, heads, len(inner.sectors), sizes.pop())
                if best is None or geom.nsec > best.nsec:
                    best = geom
            elif best is None:
                # No IBM sectors: try AmigaDOS, which uses a different sync.
                for cls in (amigados.AmigaDOS_DD, amigados.AmigaDOS_HD):
                    proto = cls(cyl, head)
                    proto.decode_flux(flux, pll)
                    if proto.nr_missing() < proto.nsec:
                        return Geometry(cyls, heads, proto.nsec, 512,
                                        amiga=True)
    return best


def _imd_geometry(path: Path) -> Optional[Geometry]:
    image = IMD.from_file(str(path), None, {})
    if not image.to_track:
        return None
    cyls = max(c for c, _ in image.to_track) + 1
    heads = max(h for _, h in image.to_track) + 1
    track = image.to_track[min(image.to_track)]
    sizes = {len(s.dam.data) for s in track.sectors}
    if len(sizes) != 1:
        return None
    return Geometry(cyls, heads, len(track.sectors), sizes.pop())


def detect_format(paths: Sequence[Path],
                  pll: Optional[PLL] = None) -> Tuple[str, str]:
    """Guess the disk format from the inputs.

    Returns ``(format_name, how_it_was_detected)``.  Sector images go first:
    their size and boot sector say the geometry outright, where a flux capture
    has to be decoded to find out.
    """
    ordered = sorted(paths, key=is_flux)
    tried = []
    for path in ordered:
        kind = READERS.get(path.suffix.lower(), (None, None))[1]
        try:
            if kind in FLAT_KINDS:
                size = path.stat().st_size
                # An ADF is pure sector data, with no boot sector to read.
                geom = (None if kind == 'adf'
                        else _bpb_geometry(path.read_bytes()))
                if geom is not None and (name := match_geometry(geom)):
                    return name, f'{path.name} boot sector ({geom})'
                if size in SIZE_FORMATS:
                    return SIZE_FORMATS[size], f'{path.name} size ({size} bytes)'
                tried.append(f'{path.name}: {size} bytes matches no format')
                continue
            if kind == 'imd':
                geom = _imd_geometry(path)
            elif kind in FLUX_KINDS:
                geom = _scan_flux_geometry(path, pll)
            else:
                continue
        except (gw_error.Fatal, OSError, struct.error) as exc:
            tried.append(f'{path.name}: {exc}')
            continue
        if geom is None:
            tried.append(f'{path.name}: no sectors decoded on its first cylinders')
            continue
        name = match_geometry(geom)
        if name is not None:
            return name, f'{path.name} ({geom})'
        tried.append(f'{path.name}: {geom} matches no supported format')

    detail = '\n  '.join(tried) if tried else 'no usable inputs'
    raise DiskStackError(
        'Could not work out the disk format automatically:\n  ' + detail
        + '\nPass --format explicitly, e.g. --format ibm.720.\nSupported: '
        + ', '.join(supported_formats()))


def is_flux(path: Path) -> bool:
    """True if this input has to be decoded rather than simply read."""
    return READERS.get(path.suffix.lower(), (None, None))[1] in FLUX_KINDS


def input_size(path: Path, kind: str) -> int:
    """Bytes an input occupies. A KryoFlux input names one file of a set."""
    if kind != 'kryoflux':
        return path.stat().st_size
    base = Path(KryoFlux(str(path), None).basename)
    return sum(f.stat().st_size for f in base.parent.glob(base.name + '*.raw'))


def open_image(path: Path, fmt: gw_codec.DiskDef):
    """Open one input file, returning ``(image, kind)``."""
    if not path.exists():
        raise DiskStackError(f'{path}: no such file')
    if path.is_dir():
        raise DiskStackError(f'{path}: is a directory, not a disk image')
    entry = READERS.get(path.suffix.lower())
    if entry is None:
        raise DiskStackError(
            f'{path}: unrecognised extension. diskstack reads '
            + ', '.join(sorted(READERS)))
    cls, kind = entry
    try:
        return cls.from_file(str(path), fmt, {}), kind
    except gw_error.Fatal as exc:
        raise DiskStackError(f'{path}: {exc}') from exc
    except (struct.error, IndexError) as exc:
        raise DiskStackError(f'{path}: truncated or corrupt {kind} file '
                             f'({exc})') from exc
    except OSError as exc:
        raise DiskStackError(f'{path}: {exc.strerror or exc}') from exc


def fill_track(track, data: Dict[int, bytes], bad: Iterable[int]):
    """Put merged sector payloads into a freshly-made track object."""
    bad = set(bad)
    if isinstance(track, amigados.AmigaDOS):
        track.map = list(range(track.nsec))
        for sec_id in range(track.nsec):
            payload = data.get(sec_id)
            if payload is not None:
                track.sector[sec_id] = bytes(16), payload
        return track
    for sec in track.sectors:
        payload = data.get(sec.idam.r)
        if payload is None:
            continue
        sec.dam.data = payload
        # IMD carries a per-sector error flag and writes it from dam.crc, so
        # an unresolved sector survives a round trip through .imd.
        bad_crc = 0xffff if sec.idam.r in bad else 0
        sec.crc = sec.idam.crc = sec.dam.crc = bad_crc
    return track


def check_writable(path: Path) -> None:
    """Reject an output path now rather than after decoding every input."""
    if path.suffix.lower() not in WRITERS:
        raise DiskStackError(
            f'{path}: cannot write that format. diskstack writes '
            + ', '.join(sorted(WRITERS)))
    if path.parent and not path.parent.exists():
        raise DiskStackError(f'{path.parent}: output directory does not exist')


def write_image(path: Path, fmt: gw_codec.DiskDef,
                data: Dict[Tuple[int, int], Dict[int, bytes]],
                bad: Dict[Tuple[int, int], Iterable[int]]) -> None:
    """Write the merged sectors out as a sector image."""
    check_writable(path)
    cls = WRITERS[path.suffix.lower()]
    try:
        with cls.to_file(str(path), fmt, False, {}) as out:
            for cyl, head in [(t.cyl, t.head) for t in fmt.tracks]:
                track = fmt.mk_track(cyl, head)
                if track is None:
                    continue
                fill_track(track, data.get((cyl, head), {}),
                           bad.get((cyl, head), ()))
                out.emit_track(cyl, head, track)
    except gw_error.Fatal as exc:
        raise DiskStackError(f'{path}: {exc}') from exc
    except OSError as exc:
        raise DiskStackError(f'{path}: {exc.strerror or exc}') from exc


__all__ = ['Geometry', 'check_writable', 'detect_format', 'fill_track',
           'get_format', 'ibm', 'input_size', 'is_flux', 'match_geometry',
           'open_image', 'supported_formats', 'write_image']
