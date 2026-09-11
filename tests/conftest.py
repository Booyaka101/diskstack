"""Shared fixtures. Decoding flux is the slow part, so it happens once."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FIXTURES))

from diskstack import candidates, formats, parallel, stack  # noqa: E402


@dataclass
class Decoded:
    """Every read attempt in the three captures, decoded once."""

    paths: List[Path]
    fmt_name: str
    fmt: object
    detail: str
    candidates: List[candidates.Candidate]
    sources: List[candidates.SourceInfo]
    source_image: Path

    def stacked(self, *names: str, vote: bool = True) -> stack.StackResult:
        """Stack some subset of the captures, by file name."""
        chosen = [p for p in self.paths if not names or p.name in names]
        cands = [c for c in self.candidates if c.source in chosen]
        expected = candidates.expected_sectors(self.fmt)
        sizes = {key: candidates.sector_size(self.fmt, *key) for key in expected}
        return stack.stack(cands, expected, sizes, chosen, vote=vote)

    def info_for(self, *names: str) -> List[candidates.SourceInfo]:
        return [i for i in self.sources if not names or i.path.name in names]


@pytest.fixture(scope='session')
def fixture_dir() -> Path:
    import make_fixtures

    try:
        make_fixtures.build()
    except SystemExit as exc:
        pytest.skip(f'test fixtures unavailable: {exc}')
    return FIXTURES


@pytest.fixture(scope='session')
def decoded(fixture_dir: Path) -> Decoded:
    import make_fixtures

    paths = [fixture_dir / name for name in make_fixtures.CAPTURES]
    fmt_name, detail = formats.detect_format(paths)
    fmt = formats.get_format(fmt_name)
    cands: List[candidates.Candidate] = []
    infos = []
    for path in paths:
        got, info = candidates.load(path, fmt, jobs=parallel.default_jobs())
        cands += got
        infos.append(info)
    return Decoded(paths=paths, fmt_name=fmt_name, fmt=fmt, detail=detail,
                   candidates=cands, sources=infos,
                   source_image=fixture_dir / make_fixtures.SOURCE_NAME)
