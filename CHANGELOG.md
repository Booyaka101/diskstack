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
