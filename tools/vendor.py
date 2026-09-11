#!/usr/bin/env python3
"""Regenerate diskstack/_vendor/greaseweazle from upstream.

Greaseweazle is not published on PyPI, so its decoders are vendored rather
than depended on. Run this to re-vendor at a new upstream commit; the SHA
lives in UPSTREAM_SHA below and is copied into NOTICE.
"""

from __future__ import annotations

import io
import pathlib
import re
import shutil
import sys
import tarfile
import urllib.request

UPSTREAM_REPO = 'keirf/greaseweazle'
UPSTREAM_SHA = '26690f89967d519e0106ab9566019a026b920bb4'
UPSTREAM_DESC = 'master, 2026-03-18 (post-v1.23)'

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEST = ROOT / 'diskstack' / '_vendor' / 'greaseweazle'

# Everything the SCP reader plus the IBM and AmigaDOS codecs pull in. The GCR
# codecs come along because codec/codec.py imports them at module scope; they
# are never reachable from diskstack's own format list.
SKIP_DIRS = {'src/greaseweazle/tools'}
SKIP_FILES = {
    'src/greaseweazle/usb.py',       # hardware, needs pyserial
    'src/greaseweazle/cli.py',       # upstream's own entry point
}
KEEP_EXT = {'.py', '.cfg'}

# tools/util.py imports pyserial and the USB stack for the parts diskstack
# never touches. Only TrackSet (diskdef geometry) and columnify (error text)
# are needed, so they are extracted into a standalone module.
UTIL_KEEP = ('class TrackSet', 'def columnify', 'def range_str')


def fetch_tarball() -> tarfile.TarFile:
    url = f'https://codeload.github.com/{UPSTREAM_REPO}/tar.gz/{UPSTREAM_SHA}'
    print(f'fetching {url}')
    with urllib.request.urlopen(url) as r:
        blob = r.read()
    return tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz')


def rewrite(text: str) -> str:
    """Repoint upstream's absolute imports at the vendored package."""
    text = re.sub(r'\bgreaseweazle\.data\b',
                  'diskstack._vendor.greaseweazle.data', text)
    text = re.sub(r'(?<![\w.])greaseweazle(?=[\s.])',
                  'diskstack._vendor.greaseweazle', text)
    return text


def extract_util(text: str) -> str:
    """Pull TrackSet and columnify out of tools/util.py."""
    lines = text.splitlines(keepends=True)
    out = [
        '# Extracted from greaseweazle/tools/util.py by tools/vendor.py.\n',
        '# The rest of that module drives the USB hardware and needs pyserial.\n',
        'from __future__ import annotations\n',
        'from typing import List, Optional\n',
        'import re\n',
        'import itertools as it\n',
        'from diskstack._vendor.greaseweazle import error\n',
        '\n\n',
    ]
    keep = False
    for line in lines:
        if line.startswith(UTIL_KEEP):
            keep = True
        elif line and not line[0].isspace() and not line.startswith(')'):
            keep = False
        if keep:
            out.append(line)
    body = ''.join(out)
    for name in UTIL_KEEP:
        assert name in body, f'vendor: {name} not found in tools/util.py'
    return body


# (path, marker that must be present, replacement) applied after import
# rewriting. Every entry is listed in NOTICE.
PATCHES = [
    (
        'codec/ibm/ibm.py',
        '            else:\n                print("Unknown mark %02x" % mark)\n',
        '            else:\n'
        '                pass  # damaged tracks produce these constantly\n',
    ),
    (
        'optimised/__init__.py',
        "        print('*** WARNING: Optimised data routines not found: '\n"
        "              'Run scripts/setup.sh')\n",
        '',
    ),
    (
        'optimised/__init__.py',
        "    print('*** WARNING: Optimised data routines disabled (GW_OPT=%s)'\n"
        "          % gw_opt)\n",
        '    pass\n',
    ),
    (
        # Upstream's IMD reader marks every sector good, discarding both the
        # per-sector data-error flag and the data-unavailable code. diskstack
        # needs them: an IMD with error sectors is the whole reason to merge.
        'image/imd.py',
        '                if rec == 0:\n'
        '                    continue # Data unavailable\n',
        '                if rec == 0:\n'
        '                    s.crc = s.dam.crc = 0xffff\n'
        '                    continue # Data unavailable\n',
    ),
    (
        'image/imd.py',
        '                if rec&2:\n'
        '                    s.dam.mark = ibm.Mark.DDAM\n',
        '                if rec&2:\n'
        '                    s.dam.mark = ibm.Mark.DDAM\n'
        '                if rec&4:\n'
        '                    s.crc = s.dam.crc = 0xffff\n',
    ),
    (
        'codec/codec.py',
        "            with importlib.resources.open_text("
        "'diskstack._vendor.greaseweazle.data',\n"
        "                                               self.name) as f:\n",
        "            res = importlib.resources.files(\n"
        "                'diskstack._vendor.greaseweazle.data')\n"
        "            with res.joinpath(self.name).open('r') as f:\n",
    ),
]


def main() -> int:
    tar = fetch_tarball()
    prefix = f'greaseweazle-{UPSTREAM_SHA}/'

    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    util_src = None
    written = []
    for member in tar.getmembers():
        if not member.isfile() or not member.name.startswith(prefix):
            continue
        rel = member.name[len(prefix):]
        if rel == 'COPYING':
            data = tar.extractfile(member).read()
            (DEST / 'COPYING').write_bytes(data)
            written.append('COPYING')
            continue
        if rel == 'src/greaseweazle/tools/util.py':
            util_src = tar.extractfile(member).read().decode()
            continue
        if not rel.startswith('src/greaseweazle/'):
            continue
        if rel in SKIP_FILES or any(rel.startswith(d + '/') for d in SKIP_DIRS):
            continue
        if pathlib.Path(rel).suffix not in KEEP_EXT:
            continue
        sub = rel[len('src/greaseweazle/'):]
        text = tar.extractfile(member).read().decode()
        out = DEST / sub
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rewrite(text), newline='\n')
        written.append(sub)

    assert util_src is not None, 'vendor: tools/util.py missing upstream'
    (DEST / 'tools').mkdir()
    (DEST / 'tools' / '__init__.py').write_text('', newline='\n')
    (DEST / 'tools' / 'util.py').write_text(extract_util(util_src),
                                            newline='\n')
    written += ['tools/__init__.py', 'tools/util.py']

    # Upstream generates this at build time to carry the version string. The
    # codec import is diskstack's: codec/codec.py imports every codec at module
    # scope and those codecs import back into codec.ibm.ibm, so importing e.g.
    # image.imd first lands on a partially-initialised module. Upstream never
    # hits it because its CLI always reaches codec.codec first.
    (DEST / '__init__.py').write_text(
        f"__version__ = 'vendored-{UPSTREAM_SHA[:12]}'\n"
        'from diskstack._vendor.greaseweazle.codec import codec  # noqa: F401\n',
        newline='\n')
    written.append('__init__.py')

    for rel, marker, replacement in PATCHES:
        path = DEST / rel
        text = path.read_text()
        assert marker in text, f'vendor: patch target missing in {rel}'
        path.write_text(text.replace(marker, replacement), newline='\n')

    print(f'vendored {len(written)} files into {DEST}')
    print(f'upstream {UPSTREAM_REPO}@{UPSTREAM_SHA} ({UPSTREAM_DESC})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
