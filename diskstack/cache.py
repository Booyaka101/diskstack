"""Keep decoded flux between runs, so the loop only decodes what is new.

Adding one capture to a stack of three re-decodes all four, and the PLL
walking the flux is nearly the whole runtime.  An entry is named for a digest
of the input's bytes together with every setting that changes the decode, so a
stale entry is never found rather than being found and then discarded.

The file is a plain record of sector payloads, not a pickle: a cache sitting
in a working directory should not be able to run code when it is read.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import zlib
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from diskstack import __version__
from diskstack.candidates import Candidate, SourceInfo

DIR_NAME = '.diskstack-cache'
SUFFIX = '.dsc'
NOTE_SUFFIX = '.dsn'
MAGIC = b'DSKC'
FORMAT_VERSION = 1
MAX_BYTES = 512 * 1024 * 1024

_HEADER = struct.Struct('<4sHI')
# cyl, head, sector id, revolution, crc flags, codec, address mark, lengths.
_RECORD = struct.Struct('<HBHHBBBHB')
_BLOCK = 1 << 20


def settings(fmt_name: str, pll, revs: Optional[int],
             detect_filler: bool) -> str:
    """Everything besides the input bytes that changes what a decode yields.

    ``str(pll)`` is Greaseweazle's own summary of the three knobs the PLL has,
    so two ways of spelling one setting share an entry.
    """
    return '\n'.join([__version__, str(FORMAT_VERSION), fmt_name,
                      'default' if pll is None else str(pll),
                      'all' if revs is None else str(revs),
                      str(bool(detect_filler))])


def key(paths: Sequence[Path], spec: str) -> str:
    """Name for the entry holding these inputs decoded with these settings.

    The bytes are hashed rather than the size and timestamp.  Re-dumping a
    disk over its own file is the normal move in this loop, and a cache that
    hands back the previous decode of it would be worse than no cache at all.
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(spec.encode('utf-8'))
    for path in paths:
        digest.update(f'\n{path.name}\n'.encode('utf-8'))
        with path.open('rb') as f:
            for block in iter(lambda: f.read(_BLOCK), b''):
                digest.update(block)
    return digest.hexdigest()


def _records(blob: bytes, path: Path,
             codecs: Sequence[str]) -> Iterator[Candidate]:
    pos, end = 0, len(blob)
    while pos < end:
        (cyl, head, sec_id, rev, flags, codec, mark, n_data,
         n_check) = _RECORD.unpack_from(blob, pos)
        pos += _RECORD.size
        data, pos = blob[pos:pos + n_data], pos + n_data
        check, pos = blob[pos:pos + n_check], pos + n_check
        if len(data) != n_data or len(check) != n_check:
            raise ValueError('truncated entry')
        yield Candidate(cyl=cyl, head=head, sec_id=sec_id, data=data,
                        id_crc_ok=bool(flags & 1), data_crc_ok=bool(flags & 2),
                        source=path, rev=rev, codec=codecs[codec],
                        check=check, mark=mark)


def _parse(raw: bytes, path: Path) -> Tuple[List[Candidate], SourceInfo]:
    magic, version, meta_len = _HEADER.unpack_from(raw)
    if magic != MAGIC or version != FORMAT_VERSION:
        raise ValueError('not a diskstack cache entry')
    start = _HEADER.size + meta_len
    meta = json.loads(raw[_HEADER.size:start].decode('utf-8'))
    info = SourceInfo(path=path, kind=meta['kind'],
                      revolutions=meta['revolutions'],
                      candidates=meta['candidates'], good=meta['good'],
                      unexpected=[tuple(u) for u in meta['unexpected']],
                      size=meta['size'], cached=True)
    cands = list(_records(zlib.decompress(raw[start:]), path, meta['codecs']))
    return cands, info


def load(directory: Path, name: str,
         path: Path) -> Optional[Tuple[List[Candidate], SourceInfo]]:
    """The decode stored under ``name``, or None if there is not a usable one.

    ``path`` replaces the one recorded, so a capture that has been renamed or
    moved since it was decoded still hits.
    """
    entry = directory / (name + SUFFIX)
    try:
        raw = entry.read_bytes()
    except OSError:
        return None
    try:
        cands, info = _parse(raw, path)
    except (ValueError, KeyError, IndexError, struct.error, zlib.error,
            UnicodeDecodeError):
        return None
    try:
        os.utime(entry)  # Read counts as use: _prune drops what nothing reads.
    except OSError:
        pass
    return cands, info


def _put(directory: Path, filename: str, blob: bytes) -> bool:
    """Write one cache file, or say it could not be written.

    A cache that will not write is not an error. The merge is the product; the
    cache only makes the next one quick.
    """
    tmp = directory / f'{filename}.{os.getpid()}.tmp'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(blob)
        os.replace(tmp, directory / filename)
    except OSError:
        # Cleaning up can fail for the same reason the write did: on Linux a
        # cache directory that is really a file makes unlink raise ENOTDIR.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def note(directory: Path, name: str, value: dict) -> None:
    """Remember a small fact about a set of inputs, such as their format."""
    _put(directory, name + NOTE_SUFFIX,
         json.dumps(value).encode('utf-8'))


def recall(directory: Path, name: str) -> Optional[dict]:
    """The fact stored under ``name``, or None if there is not a usable one."""
    try:
        value = json.loads((directory / (name + NOTE_SUFFIX)).read_text('utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def store(directory: Path, name: str, cands: Sequence[Candidate],
          info: SourceInfo) -> None:
    """Record a decode under ``name``."""
    codecs = sorted({c.codec for c in cands})
    index = {codec: i for i, codec in enumerate(codecs)}
    body = bytearray()
    for c in cands:
        body += _RECORD.pack(c.cyl, c.head, c.sec_id, c.rev,
                             c.id_crc_ok | (c.data_crc_ok << 1),
                             index[c.codec], c.mark, len(c.data),
                             len(c.check))
        body += c.data + c.check
    meta = json.dumps({'path': info.path.name, 'kind': info.kind,
                       'revolutions': info.revolutions,
                       'candidates': info.candidates, 'good': info.good,
                       'unexpected': info.unexpected, 'size': info.size,
                       'codecs': codecs}).encode('utf-8')
    blob = (_HEADER.pack(MAGIC, FORMAT_VERSION, len(meta)) + meta
            + zlib.compress(bytes(body), 1))
    if _put(directory, name + SUFFIX, blob):
        prune(directory)


def prune(directory: Path, max_bytes: int = MAX_BYTES) -> None:
    """Drop the least recently used entries once the cache passes its cap.

    Nothing here is precious: an entry that goes can be rebuilt by decoding
    the flux again, which is what happens for a new capture anyway.
    """
    entries, total = [], 0
    for entry in (list(directory.glob('*' + SUFFIX))
                  + list(directory.glob('*' + NOTE_SUFFIX))):
        try:
            st = entry.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, entry))
        total += st.st_size
    for _, size, entry in sorted(entries, key=lambda e: e[0]):
        if total <= max_bytes:
            return
        try:
            entry.unlink()
        except OSError:
            continue
        total -= size


__all__ = ['DIR_NAME', 'MAX_BYTES', 'key', 'load', 'note', 'prune', 'recall',
           'settings', 'store']
