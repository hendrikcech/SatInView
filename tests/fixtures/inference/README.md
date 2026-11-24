The obstruction and expected CSVs contain 2026-01-11 00:33:12 through 00:38:11
UTC from `sats_2026-01-11/` from the ["Dissecting the Starlink"
dataset](https://mediatum.ub.tum.de/1856124). The TLE is the complete matching
00:33 snapshot, so all original satellite candidates remain available. The test
uses the observer coordinates supplied explicitly by the test.

The historical output includes 20 rows without observations. The current
processor omits those rows; the snapshot compares all 280 observed rows.
Computed distances (km) and matched angles (degrees) use a 0.01 absolute
tolerance for small historical differences. Identities and observed geometry are
compared exactly. Review any changes to this baseline rather than regenerating
it automatically.
