# diskstack

Merge several dumps of one floppy disk into the best possible image, and get
told exactly what is still missing.

## The loop

Read the disk twice, stack the dumps, and diskstack hands you back a
Greaseweazle command that re-reads only the tracks that are still bad:

```
$ diskstack disk_a.scp disk_b.scp -o merged.img

720 sectors: 714 clean (from 2 files), 6 unresolved

Tracks needing attention
  cyl  head  clean  voted  unresolved  missing  sector ids still bad
   17     0      7      0           2        0  4,7
   18     0      7      0           2        0  4,7
   19     0      7      0           2        0  4,7

Re-read just these tracks, then run diskstack again with the new capture added:
  gw read retry.scp --tracks=c=17-19:h=0
```

Three tracks instead of eighty, so the drive spends seconds on the media
rather than minutes. Feed the new capture back in:

```
$ gw read disk_c.scp --tracks=c=17-19:h=0
$ diskstack disk_a.scp disk_b.scp disk_c.scp -o merged.img

720 sectors: 714 clean (from 3 files), 6 recovered by vote

Every sector confirmed by CRC. Nothing left to re-read.
```

That run produced a file byte-identical to the original disk image. Repeat
until the unresolved count reaches zero or stops moving. The full session is in
[examples/console-session.txt](examples/console-session.txt) and the report it
wrote is in [examples/diskstack-report.json](examples/diskstack-report.json).

## Install

```
pip install diskstack
```

Python 3.11 or newer. No hardware, no network, no configuration.

## Use

```
diskstack disk_a.scp disk_b.scp disk_c.img -o merged.img
```

Inputs are two or more dumps of the same disk, in any mix of:

| what | extensions |
| --- | --- |
| flux | `.scp` (SuperCard Pro), `.raw` (KryoFlux streams), `.hfe` (HxC) |
| sector images | `.img`, `.ima`, `.st`, `.adf`, `.imd` |

For KryoFlux, name any one of the per-track stream files and the rest of the
set is picked up from the directory.

Output is a sector image: `.img`, `.ima`, `.st`, `.adf` or `.imd`. Flux goes in
but never comes out. Along with it you get `diskstack-report.json` next to the
image and the table above on stdout.

Formats understood are IBM FM and MFM (PC 160K through 2.88M, Atari ST) and
AmigaDOS MFM. `diskstack --list-formats` prints the list. Detection is
automatic; `--format ibm.720` overrides it.

The exit code is 0 when every sector is confirmed and 2 when something is still
unresolved, so a retry loop can be scripted.

## How it decides

Every sector of every revolution of every input is a separate read attempt.
Attempts are grouped by the `(cylinder, head, sector id)` in their ID address
mark, never by where they sit in the flux, because two captures of the same
disk do not line up: the index phase differs and so does the motor speed.

Per sector, in order:

1. **clean** - an attempt whose own CRC or checksum passes. Taken as is.
2. **recovered by vote** - no attempt passed, so the attempts get recombined
   into candidate payloads and the first one that satisfies a check value off
   the disk wins. Three routes are tried, best evidence first:
   `majority` votes byte by byte across every attempt, `per_source_majority`
   collapses each input file to one payload first so a capture with more
   revolutions cannot outvote the files that read the sector correctly, and
   `cross_check` tries each individual read as it stands, which is what
   recovers a sector whose payload was fine and whose own CRC bytes were the
   damaged part. The CRC bytes came off the same damaged track, so they get
   voted on too and the voted check value is tried alongside the ones read.
   Ties go to the dump that read the rest of the disk best. The report says
   which route it was.
3. **unresolved** - the vote still failed. The largest cluster of attempts that
   agree exactly is written out, the sector is listed in the table, and its
   track goes into the re-read command.

A sector that is good in any input is never worse in the output. There is a
test for that.

Only a handful of reconstructions are tried, deliberately. A CRC16 accepts the
wrong payload once in 65536 tries, so every extra candidate thrown at the check
value buys recovery at the price of a small chance of confidently writing out
garbage. Three routes that each mean something beats a brute-force sweep over
the contested bytes.

The check value has to come off the disk, which means a stack of only sector
images can never reach step 2. `.img` and `.adf` files carry data and nothing
to verify it with, so identical bytes in two images prove only that both dumps
read the same thing. Mixing in one flux capture is what gives the vote
something to check against.

A sector the merge could not confirm is marked `unstable` if one capture read
it two different ways across its own revolutions. That is what weak bits look
like, and they are usually deliberate, so more passes over the disk will not
settle them. The stdout note says so next to the re-read command.

Sectors Greaseweazle filled with `-=[BAD SECTOR]=-` are treated as failed
reads, not as data, so a `.img` from an earlier bad session still contributes
its good sectors. `--keep-filler` turns that off.

A truncated `.img` or `.adf` contributes only the sectors the file actually
holds bytes for. The readers pad a short image out to the format's length and
mark every sector they invented as CRC-clean, so without that check a dump that
stopped halfway would win the merge with 512 bytes of nothing per sector.

## Options

```
  -o, --output PATH               Merged sector image to write (.img, .ima,
                                  .st, .adf, .imd).  [required]
  -r, --report PATH               JSON report path  [default: diskstack-
                                  report.json beside the output]
  --no-report                     Do not write the JSON report.
  -f, --format TEXT               Disk format, e.g. ibm.720 or amiga.amigados.
                                  Detected from the inputs when not given.
  --list-formats                  Print the disk formats diskstack understands
                                  and exit.
  --pll SPEC                      Flux PLL settings, e.g.
                                  period=5:phase=60:lowpass=1.5. Repeat to
                                  decode each capture several ways and let the
                                  results compete.
  --revs N                        Use only the first N revolutions of each
                                  flux capture [default: all of them]  [x>=1]
  --keep-filler                   Trust sectors written as '-=[BAD SECTOR]=-'
                                  filler instead of treating them as failed
                                  reads.
  --fill-unresolved [best|filler|zero]
                                  What to write for a sector no input
                                  confirmed: its best guess, bad-sector
                                  filler, or zeroes.  [default: best]
  --no-vote                       Skip the byte-wise majority vote; keep only
                                  sectors whose own CRC passes.
  -j, --jobs N                    Worker processes for decoding flux [default:
                                  one per core, up to 8]  [x>=0]
  --retry-name NAME               Filename used in the printed gw read
                                  command.  [default: retry.scp]
  -q, --quiet                     Print the summary and the re-read command,
                                  no tables.
  -V, --version                   Show the version and exit.
  -h, --help                      Show this message and exit.
```

Decoding flux is the slow part, so it runs one track per core by default. A
three-capture 40-cylinder merge takes about six seconds on eight cores against
half a minute on one. `--jobs 1` forces the single-process path, which is also
what happens automatically if the worker pool cannot start.

`--pll` is worth knowing about. Repeating it decodes each flux capture several
ways, and since two PLL settings disagree about different marginal bitcells,
they become independent attempts that the vote can use:

```
diskstack a.scp b.scp -o merged.img --pll period=5:phase=60 --pll lowpass=1.5
```

## The report

`diskstack-report.json` carries one entry per sector of the disk: its status,
how many attempts it got, how many agreed, and which file and revolution each
contributing read came from. Plus per-input totals and the re-read trackspecs.
`schema` is versioned and starts at 1.

```json
{
  "cyl": 17, "head": 0, "sec_id": 4, "size": 512,
  "status": "unresolved", "attempts": 6, "good": 0, "agreement": 3,
  "discarded": 0, "unstable": false, "method": null,
  "sources": [{"path": "capture_a.scp", "rev": 0}]
}
```

`method` names the route that rebuilt a recovered sector and is null for any
other status. `discarded` counts attempts thrown away because they decoded to
the wrong length for the sector.

## What it does not do

v1 is deliberately narrow. No GCR, so no Apple II and no Commodore. No writing
flux back out. No hardware access, so nothing here talks to a Greaseweazle
or a KryoFlux. No copy-protection preservation: this produces sector images,
which is the wrong container for weak bits and long tracks. No network calls.

## Validation, honestly

The three-capture test builds genuine SuperCard Pro flux from a real PC floppy
image, damages each capture a different way, gives each its own index phase and
its own motor speed within 1.5 percent, and asserts the merged output is
byte-identical to the original. The AmigaDOS codec gets the same treatment one
track at a time, since the fixture disk is a PC one. That is a real decode of
real flux through Greaseweazle's own decoders, but the damage is synthetic. It
has not yet been run against dumps of a genuinely deteriorated disk.

If you have a disk that needs several passes, I would like to see the report.
Open an issue with the `diskstack-report.json` and what the disk is.

## Tests

```
pip install -e ".[test]"
python tests/fixtures/make_fixtures.py     # downloads 360 KB, writes 55 MB
python -m pytest tests -q
```

The fixture builder needs the network once. Everything else runs offline.

## Built on Greaseweazle

The flux and sector decoders are [Greaseweazle's](
https://github.com/keirf/greaseweazle), vendored under
`diskstack/_vendor/greaseweazle` because Greaseweazle is not published on PyPI.
It is public domain under the Unlicense. `tools/vendor.py` regenerates the tree
from a pinned commit and applies every local change; `NOTICE` lists them.

Runtime dependencies are `click`, `bitarray` and `crcmod`. The last two are
what the vendored decoders need.

## License

MIT. See `LICENSE`, and `NOTICE` for the vendored code.
