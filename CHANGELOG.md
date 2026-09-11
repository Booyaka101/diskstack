# Changelog

## 1.0.0

First release.

- Merges two or more dumps of one floppy disk into a single sector image.
- Reads SuperCard Pro flux (`.scp`) and sector images (`.img`, `.ima`, `.st`,
  `.adf`, `.imd`), in any mix.
- Decodes every revolution of every flux capture, not just the first two.
- Groups read attempts by the decoded `(cylinder, head, sector id)`, so
  captures with different index phases and motor speeds still line up.
- Three tiers per sector: a passing CRC wins, else a byte-wise majority vote
  that has to satisfy a check value off the disk, else unresolved.
- Treats Greaseweazle's `-=[BAD SECTOR]=-` filler as a failed read.
- Prints a `gw read --tracks=` command naming only the tracks still unresolved.
- Writes `diskstack-report.json` with per-sector provenance, schema 1.
- IBM FM/MFM and AmigaDOS MFM formats, auto-detected.
