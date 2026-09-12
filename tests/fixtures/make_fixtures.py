"""Build the test fixtures: one real disk image, three damaged flux captures.

The source image is a real PC floppy dump from the fluxfox test corpus.  It is
re-encoded here into genuine SuperCard Pro flux with Greaseweazle's own
encoder, then damaged three different ways, so the tests exercise the real
decode path rather than hand-made sector tables.

Each capture gets its own index phase and its own motor speed, because that is
what makes cross-dump matching a real problem: the same sector sits at a
different flux offset in every file, so only the decoded (cyl, head, sector id)
can line them up.

    python tests/fixtures/make_fixtures.py

Needs the network the first time (about 360 KB) and writes about 55 MB of
flux.  Everything it writes is cached and regenerated only when missing.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bitarray import bitarray  # noqa: E402

from diskstack._vendor.greaseweazle.codec import codec as gw_codec  # noqa: E402
from diskstack._vendor.greaseweazle.codec.ibm import ibm  # noqa: E402
from diskstack._vendor.greaseweazle.image.img import IMG  # noqa: E402
from diskstack._vendor.greaseweazle.image.scp import SCP  # noqa: E402

HERE = Path(__file__).resolve().parent

BASE_URL = ('https://raw.githubusercontent.com/dbalsom/fluxfox/main/'
            'tests/images/transylvania/')
SOURCE_NAME = 'Transylvania.img'
SOURCE_SIZE = 368640
SOURCE_SHA256 = \
    '9986f34fe9bef7bfbedc2f81e87fab1d3a4c8ad7be8bfafdfcb148a8a2f52a65'
LICENSE_NAME = 'LICENSE.txt'

FORMAT = 'ibm.360'
REVS = 3
CAPTURES = ('capture_a.scp', 'capture_b.scp', 'capture_c.scp')

# Sectors damaged in every capture, at a different offset each time, so only
# the byte-wise vote gets them back.  Kept on three adjacent cylinders of one
# head so the re-read command comes out as a single tidy track range.
HARD_TRACKS = ((17, 0), (18, 0), (19, 0))
HARD_SECTORS = tuple((cyl, head, sec)
                     for cyl, head in HARD_TRACKS for sec in (4, 7))
HARD_WINDOW = 24


def solo_damage(index: int) -> List[Tuple[int, int, int]]:
    """Sectors only this capture loses. The other two still have them clean."""
    out = []
    for cyl in range(0, 40, 4):
        for head in (0, 1):
            out.append((cyl, head, 1 + (cyl // 4 + head * 3 + index * 2) % 9))
    return out


def fetch(name: str, dest: Path, *, size: int = 0, sha256: str = '') -> Path:
    if dest.exists() and (not size or dest.stat().st_size == size):
        return dest
    url = BASE_URL + name
    print(f'downloading {url}')
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f'could not download {url}: {exc}\n'
                         f'Fetch it by hand and save it as {dest}.') from None
    if size and len(data) != size:
        raise SystemExit(f'{url}: expected {size} bytes, got {len(data)}')
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f'{url}: contents changed upstream, refusing to use '
                         f'it as a fixture')
    dest.write_bytes(data)
    return dest


def dam_regions(bits: bitarray) -> Dict[int, Tuple[int, int, int]]:
    """Per sector id, the bit range of its sync, its data and its CRC."""
    out: Dict[int, Tuple[int, int, int]] = {}
    pending = None
    for offs in bits.search(ibm.mfm_sync):
        if len(bits) < offs + 4 * 16:
            continue
        mark = ibm.decode(bits[offs + 3 * 16:offs + 4 * 16].tobytes())[0]
        if mark == ibm.Mark.IDAM:
            _, _, sec_id, n = ibm.decode(
                bits[offs + 4 * 16:offs + 8 * 16].tobytes())
            pending = sec_id, ibm.sec_sz(n)
        elif mark == ibm.Mark.DAM and pending is not None:
            sec_id, size = pending
            out[sec_id] = (offs, offs + 4 * 16, offs + (4 + size) * 16)
            pending = None
    return out


def scramble(bits: bitarray, start: int, end: int, rng: random.Random) -> None:
    bits[start:end] = bitarray(
        [rng.randint(0, 1) for _ in range(end - start)], endian='big')


def flip(bits: bitarray, start: int, end: int, count: int,
         rng: random.Random) -> None:
    for pos in rng.sample(range(start, end), count):
        bits[pos] ^= 1


def damage_track(bits: bitarray, cyl: int, head: int, index: int,
                 solo: Set[Tuple[int, int, int]],
                 rng: random.Random) -> int:
    """Apply this capture's damage to one track's bitstream."""
    regions = dam_regions(bits)
    hurt = 0
    for sec_id in sorted(regions):
        sync, start, end = regions[sec_id]
        if (cyl, head, sec_id) in HARD_SECTORS:
            offset = start + (40 + index * 120) * 16
            scramble(bits, offset, offset + HARD_WINDOW * 16, rng)
            hurt += 1
        elif (cyl, head, sec_id) in solo:
            if sec_id % 2:
                # Break the data mark itself: the sector vanishes from this
                # capture rather than arriving with a bad CRC.
                flip(bits, sync, start, 8, rng)
            else:
                offset = start + rng.randrange(0, 400) * 16
                scramble(bits, offset, offset + 32 * 16, rng)
            hurt += 1
    return hurt


def make_capture(image: IMG, fmt, index: int, path: Path) -> int:
    rng = random.Random(0xD15C0 + index)
    solo = set(solo_damage(index))
    speed = 1.0 + (index - 1) * 0.015
    out = SCP(str(path), fmt)
    hurt = 0
    for cyl, head in [(t.cyl, t.head) for t in fmt.tracks]:
        track = image.get_track(cyl, head)
        if track is None:
            continue
        master = track.master_track()
        bits = bitarray(master.bits, endian='big')
        hurt += damage_track(bits, cyl, head, index, solo, rng)
        phase = rng.randrange(len(bits))
        master.bits = bits[phase:] + bits[:phase]
        master.time_per_rev *= speed * (1.0 + rng.uniform(-0.002, 0.002))
        out.emit_track(cyl, head, master.flux(revs=REVS))
    path.write_bytes(out.get_image())
    return hurt


def build(force: bool = False) -> List[Path]:
    source = fetch(SOURCE_NAME, HERE / SOURCE_NAME,
                   size=SOURCE_SIZE, sha256=SOURCE_SHA256)
    fetch(LICENSE_NAME, HERE / LICENSE_NAME)

    fmt = gw_codec.get_diskdef(FORMAT)
    made = []
    for index, name in enumerate(CAPTURES):
        path = HERE / name
        if path.exists() and not force:
            print(f'{name} already there')
            continue
        image = IMG.from_file(str(source), fmt, {})
        hurt = make_capture(image, fmt, index, path)
        print(f'{name}: {path.stat().st_size // 1024} KB, '
              f'{hurt} sectors damaged')
        made.append(path)
    return made


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--force', action='store_true',
                        help='rebuild the captures even if they exist')
    build(force=parser.parse_args().force)


if __name__ == '__main__':
    main()
