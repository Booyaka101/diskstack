# Extracted from greaseweazle/tools/util.py by tools/vendor.py.
# The rest of that module drives the USB hardware and needs pyserial.
from __future__ import annotations
from typing import List, Optional
import re
import itertools as it
from diskstack._vendor.greaseweazle import error


def columnify(strings, columns=80, sep=2):
    max_len = max(len(s) for s in strings) + sep
    per_row = max(1, columns // max_len)
    return '\n'.join(map(lambda row: (f'{{:{max_len}}}'*per_row).format(*row),
                         it.zip_longest(*[iter(strings)]*per_row,
                                        fillvalue='')))


def range_str(l):
    if len(l) == 0:
        return '<none>'
    p, str = None, ''
    for i in l:
        if p is not None and i == p[1]+1:
            p = p[0], i
            continue
        if p is not None:
            str += ('%d,' % p[0]) if p[0] == p[1] else ('%d-%d,' % p)
        p = (i,i)
    if p is not None:
        str += ('%d' % p[0]) if p[0] == p[1] else ('%d-%d' % p)
    return str

class TrackSet:

    class TrackIter:
        """Iterate over a TrackSet in physical <cyl,head> order."""
        def __init__(self, ts):
            l = []
            for c in ts.cyls:
                for h in ts.heads:
                    pc, ph = ts.ch_to_pch(c, h)
                    l.append((pc, ph, c, h))
            l.sort()
            self.l = iter(l)
        def __iter__(self):
            return self
        def __next__(self):
            (self.physical_cyl, self.physical_head,
             self.cyl, self.head) = next(self.l)
            return self
    
    def __init__(self, trackspec):
        self.cyls = list()
        self.heads = list()
        self.h_off = [0]*2
        self.step = 1
        self.hswap = False
        self.trackspec = ''
        self.update_from_trackspec(trackspec)

    def ch_to_pch(self, c, h):
        pc = c//-self.step if self.step < 0 else c*self.step
        pc += self.h_off[h]
        ph = 1-h if self.hswap else h
        return pc, ph

    def update_from_trackspec(self, trackspec):
        """Update a TrackSet based on a trackspec."""
        self.trackspec += trackspec
        for x in trackspec.split(':'):
            if x == 'hswap':
                self.hswap = True
                continue
            k,v = x.split('=')
            if k == 'c':
                cyls = set()
                for crange in v.split(','):
                    m = re.match(r'(\d+)(-(\d+)(/(\d+))?)?$', crange)
                    if m is None: raise ValueError()
                    if m.group(3) is None:
                        s,e,step = int(m.group(1)), int(m.group(1)), 1
                    else:
                        s,e,step = int(m.group(1)), int(m.group(3)), 1
                        if m.group(5) is not None:
                            step = int(m.group(5))
                    for c in range(s, e+1, step):
                        cyls.add(c)
                self.cyls = sorted(cyls)
            elif k == 'h':
                heads = [False]*2
                for hrange in v.split(','):
                    m = re.match(r'([01])(-([01]))?$', hrange)
                    if m is None: raise ValueError()
                    if m.group(3) is None:
                        s,e = int(m.group(1)), int(m.group(1))
                    else:
                        s,e = int(m.group(1)), int(m.group(3))
                    for h in range(s, e+1):
                        heads[h] = True
                self.heads = []
                for h in range(len(heads)):
                    if heads[h]: self.heads.append(h)
            elif re.match(r'h[01].off$', k):
                h = int(re.match(r'h([01]).off$', k).group(1))
                m = re.match(r'([+-]\d+)$', v)
                if m is None: raise ValueError()
                self.h_off[h] = int(m.group(1))
            elif k == 'step':
                m = re.match(r'1/(\d+)$', v)
                self.step = -int(m.group(1)) if m is not None else int(v)
            else:
                raise ValueError()
        
    def __str__(self):
        s = 'c=%s' % range_str(self.cyls)
        s += ':h=%s' % range_str(self.heads)
        for i in range(len(self.h_off)):
            x = self.h_off[i]
            if x != 0:
                s += ':h%d.off=%s%d' % (i, '+' if x >= 0 else '', x)
        if self.step != 1:
            s += ':step=' + (('1/%d' % -self.step) if self.step < 0
                             else ('%d' % self.step))
        if self.hswap: s += ':hswap'
        return s

    def __iter__(self):
        return self.TrackIter(self)

    def __contains__(self, key):
        c, h = key
        return c in self.cyls and h in self.heads

