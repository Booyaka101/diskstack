"""Detection of Greaseweazle's bad-sector filler inside decoded images.

A sector image has nowhere to record "this sector was unreadable", so
Greaseweazle writes ``-=[BAD SECTOR]=-`` repeated across the whole sector
instead.  Reading such an image back gives a sector that looks perfectly
valid, which is the complaint in greaseweazle issues #97 and #565.  diskstack
treats any sector that is exactly that repeated pattern as a failed read, so
another dump can supply the real bytes.
"""

FILLER = b'-=[BAD SECTOR]=-'


def is_filler(data: bytes) -> bool:
    """True if ``data`` is Greaseweazle's bad-sector filler and nothing else."""
    n = len(data)
    if n < len(FILLER) or n % len(FILLER) != 0:
        return False
    return bytes(data) == FILLER * (n // len(FILLER))


def make_filler(size: int) -> bytes:
    """Bad-sector filler for a sector of ``size`` bytes.

    Matches Greaseweazle's own ``FILLER * (size // 16)``, which leaves a short
    tail of zeroes when the sector size is not a multiple of 16.
    """
    dat = FILLER * (size // len(FILLER))
    return dat + bytes(size - len(dat))
