# Muni reliability study — routes 1, 33, 38

Quantifying two intuitions:

1. **Real-time estimates (NextMuni signs / apps) don't match reality** — prediction accuracy.
2. **Buses frequently don't leave at their scheduled time** — schedule adherence.

## What's already known (existing public data)

- SFMTA's own systemwide on-time performance metric (departures within
  1 min early / 4 min late of schedule) hovered at **~55–58%** through
  February 2024, when SFMTA stopped publishing it ("report offline, new
  reports in development"). Series saved in `data/sfmta_systemwide_otp.csv`
  (source: DataSF Scorecard Measures, dataset `kc49-udxn`).
- No current public **route-level** on-time data exists for the 1/33/38,
  and no public dataset measures prediction accuracy. Hence this project:
  we collect our own.

## Setup (one-time)

Get a free 511.org API token (instant, emailed): https://511.org/open-data/token

Then either `export TRANSIT_511_TOKEN=...` or write the token to a file
named `.token` in this directory.

## Path A (preferred): 511 historic archive

511 archives observed real-time arrivals monthly since 2022-03
(`stop_observations.txt` inside the `-so` historic feed). No collection
needed for schedule adherence:

```sh
uv run fetch_historic.py 2026-06 2026-05 2026-04
```

downloads each month's schedule + observed arrivals for Muni into
`data/historic/`. Analysis script for this format gets written once we
can see the actual schema.

This covers "do buses leave on time" completely. It does NOT contain
prediction snapshots, so grading the accuracy of the NextMuni signs
requires recording predictions as they happen (below).

## Path B: recording predictions (for accuracy grading)

Running now, no laptop needed: this repo (github.com/jonahweissman/muni-tracker,
public) has a GitHub Actions cron (`.github/workflows/snapshot.yml`) that runs
`snapshot_predictions.py` every ~5 minutes and commits each GTFS-RT
trip-updates snapshot to `snapshots/YYYY-MM-DD/HHMMSSZ.csv.gz`. Token lives
in the `TRANSIT_511_TOKEN` Actions secret. Snapshots covered only routes
1/33/38/38R until 2026-08-13; since then they cover **all Muni routes**
(~35k predictions, ~250 KB each — ~70 MB/day, so the repo grows ~2 GB/month
while collection runs; move storage out of git if this runs long-term).

Grading: once the covering month's `-so` archive is published, join snapshots
to observed arrivals on trip + stop_sequence + service date (note: archive
trip_ids look like `SF:12053393_M11:20260630`; live feed uses the bare numeric
prefix, and archive route_ids carry an `SF:` prefix). Error = predicted −
observed, bucketed by horizon (predicted − poll_ts).

## Path B-alt: local live collector (unused)

```sh
uv run collect.py
```

Polls 511's GTFS-realtime TripUpdates + VehiclePositions for agency SF
every 150 s (respects 511's 60 req/hr limit), keeps only routes 1, 33,
38, 38R, and appends to `data/muni.db` (SQLite). Leave it running for
**2–3 days minimum** (a week is better) — run it under `caffeinate -is`
or in tmux so the laptop doesn't sleep.

## Analyze

```sh
uv run analyze.py
```

Reports, per route:

1. **Schedule adherence** at every observed stop (SFMTA's -1/+4 min
   on-time definition, median & p90 delay).
2. **Terminal departures** — did trips leave their first stop on time
   (the "buses don't leave when scheduled" claim).
3. **Prediction accuracy** — error of the real-time predictions vs the
   actual arrival, bucketed by how far out the prediction was (0–3, 3–6,
   6–10, 10–15, 15–30 min).
4. **Schedule vs real-time divergence** — how often the live estimate
   disagreed with the printed schedule by >2 / >5 min.

## Method notes

- "Actual" arrival times are derived by **prediction convergence**: the
  last prediction observed for a (trip, stop) just before that stop drops
  out of the feed. Predictions converge to truth as the bus arrives, so
  this is good to roughly ±1 min at a 150 s poll interval. The converged
  snapshot itself is excluded from the accuracy stats to avoid bias.
- The schedule comes from SFMTA's GTFS (`data/gtfs_sfmta`, downloaded
  2026-08-04 from https://muni-gtfs.apps.sfmta.com/data/muni_gtfs-current.zip).
  If SFMTA ships a schedule change mid-collection, re-download it —
  `analyze.py` prints the trip_id join rate and warns if it's poor.
- The 511 feed is fed by the same prediction engine (Swiftly) that powers
  the NextMuni signs at stops, so "prediction accuracy" here is a fair
  proxy for the signs.
