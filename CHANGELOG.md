# Changelog

## 1.0.0

First release.

- Merges two or more dumps of one floppy disk into a single sector image.
- Reads flux (`.scp`, KryoFlux `.raw` streams, `.hfe`) and sector images
  (`.img`, `.ima`, `.st`, `.adf`, `.imd`), in any mix.
- Decodes every revolution of every flux capture, not just the first two.
- Groups read attempts by the decoded `(cylinder, head, sector id)`, so
  captures with different index phases and motor speeds still line up.
- Three tiers per sector: a passing CRC wins, else the attempts are rebuilt
  into candidate payloads that have to satisfy a check value off the disk,
  else unresolved.
- Three reconstruction routes, reported per sector as `method`: a byte-wise
  majority, a majority of one payload per input file so a capture with more
  revolutions cannot outvote the rest, and each read on its own, which
  recovers a sector whose CRC bytes rather than payload were damaged.
- Treats Greaseweazle's `-=[BAD SECTOR]=-` filler as a failed read.
- Prints a `gw read --tracks=` command naming only the tracks still unresolved.
- Writes `diskstack-report.json` with per-sector provenance, schema 1.
- IBM FM/MFM and AmigaDOS MFM formats, auto-detected.
- Decodes flux one track per core, `-j/--jobs` to change it, falling back to a
  single process if the worker pool cannot start.
- Flags a sector as unstable when one capture read it two different ways across
  its own revolutions, and says so beside the re-read command, because weak
  bits do not settle down however many more passes you make.
- Refuses to write the merged image or the report over one of the inputs.
- Warns when a sector image is not the size the chosen format implies, since
  the extra sectors get ignored and a missing tail counts as unread.
- Takes only the sectors a truncated `.img` or `.adf` actually holds. The
  readers pad a short image to the format length and mark what they invented
  CRC-clean, which would otherwise win the merge with zeroes.
- Rejects an output path it cannot write before reading any input rather than
  after decoding all of them.
- Calls a sector verified only when some read of it satisfied a check value off
  the disk, and ends a merge of sector images by saying nothing confirmed them
  instead of claiming a CRC did.
- Flags a sector contested when two dumps both read it cleanly and disagreed,
  keeps the best-ranked dump's copy, and names those sectors on stdout, since
  nothing in a sector image can arbitrate between them.
- Applies a repeated `--pll` to every flux container. KryoFlux streams and HFE
  images previously took the first setting and ignored the rest.
- Refuses a report path equal to the merged image.
- Counts a KryoFlux input as the whole stream set rather than the single file
  named on the command line.
- Compares each run against the report it replaces and says how many sectors
  the new capture recovered, or that nothing moved, which is the loop's stop
  condition. Also in the report as `since_last_report`.
- Stops calling a sector unstable when a repeated `--pll` read one revolution
  two ways. Weak bits are revolutions disagreeing with each other, not two
  decodes of the same revolution.
- Rejects an input it cannot open, including a KryoFlux name that does not fit
  the per-track pattern, before decoding any of the others.
- Measures a KryoFlux set by the track files the reader will open, so a second
  capture sitting in the same directory is not counted in.
