Real output from the two runs in `console-session.txt`, taken on the test
fixtures: three flux captures of the same 360K disk, each damaged differently.
Nothing here is hand-written.

`diskstack-report.json` is the report from the first run, the one with six
sectors still unresolved, because that is the state you actually act on. It
carries one entry per sector of the disk, so it is large.

Rebuild the fixtures with `python tests/fixtures/make_fixtures.py`.
