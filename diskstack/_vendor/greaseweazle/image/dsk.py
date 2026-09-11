# greaseweazle/image/dsk.py
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

from diskstack._vendor.greaseweazle.image.image import Image, OptDict
from diskstack._vendor.greaseweazle.image.img import IMG
from diskstack._vendor.greaseweazle.image.edsk import EDSK
from diskstack._vendor.greaseweazle.image.apridisk import Apridisk

class DSK(IMG):

    @classmethod
    def from_file(cls, name: str, fmt, opts: OptDict) -> Image:

        with open(name, "rb") as f:
            sig = f.read(24)

        if sig[:8] == b'MV - CPC' or sig[:16] == b'EXTENDED CPC DSK':
            return EDSK.from_file(name, fmt, opts)

        if sig[:24] == b"ACT Apricot disk image\032\004":
            return Apridisk.from_file(name, fmt, opts)

        return IMG.from_file(name, fmt, opts)

# Local variables:
# python-indent: 4
# End:
