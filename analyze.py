# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas>=2.0",
# ]
# ///
"""Quantify Muni reliability for routes 1, 33, 38 from collected data.

Reads data/muni.db (built by collect.py) plus the GTFS schedule in
data/gtfs_sfmta and reports:

1. Schedule adherence — actual vs scheduled time at each stop, using
   SFMTA's own on-time definition (no more than 1 min early / 4 min late),
   plus terminal (first-stop) departure punctuality specifically.
2. Prediction accuracy — how far off the real-time predictions (what the
   NextMuni signs show) were from when the bus actually arrived, bucketed
   by prediction horizon.
3. Schedule vs real-time divergence — how often the real-time estimate
   disagreed with the printed schedule by more than 2 / 5 minutes.

"Actual" arrival times are derived by prediction convergence: the last
prediction observed for a (trip, stop) immediately before that stop drops
out of the feed. Muni predictions converge to the true arrival as the bus
approaches, so with a 150s poll interval this is accurate to ~±1 min.

Usage:  uv run analyze.py [--gtfs data/gtfs_sfmta] [--db data/muni.db]
"""

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

HERE = Path(__file__).parent
TZ = ZoneInfo("America/Los_Angeles")
POLL_INTERVAL = 150  # must match collect.py
ONTIME_EARLY = -60   # SFMTA definition: <=1 min early
ONTIME_LATE = 240    # <=4 min late


def load_predictions(db_path: Path) -> pd.DataFrame:
    db = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        """SELECT poll_ts, trip_id, route_id, direction_id, start_date,
                  stop_id, stop_sequence,
                  COALESCE(arrival_time, departure_time) AS pred_time,
                  departure_time AS pred_departure
           FROM predictions
           WHERE COALESCE(arrival_time, departure_time) IS NOT NULL""",
        db,
    )
    db.close()
    df["trip_key"] = df["trip_id"] + "_" + df["start_date"].fillna("")
    return df


def derive_actuals(preds: pd.DataFrame) -> pd.DataFrame:
    """Actual passage time per (trip_key, stop_id) via prediction convergence."""
    last_trip_poll = preds.groupby("trip_key")["poll_ts"].max().rename("trip_last_poll")

    idx = preds.groupby(["trip_key", "stop_id"])["poll_ts"].idxmax()
    last = preds.loc[idx].merge(last_trip_poll, on="trip_key")

    # When did we next see the trip after this stop's final appearance?
    trip_polls = preds[["trip_key", "poll_ts"]].drop_duplicates().sort_values("poll_ts")
    tp = trip_polls.rename(columns={"poll_ts": "next_trip_poll"})
    last = last.sort_values("poll_ts")
    tp = tp.sort_values("next_trip_poll")
    last = pd.merge_asof(
        last, tp, left_on="poll_ts", right_on="next_trip_poll",
        by="trip_key", direction="forward", allow_exact_matches=False,
    )

    # Stop dropped while trip continued: passage happened between poll_ts and
    # next_trip_poll. Accept converged prediction as actual.
    cont = last.next_trip_poll.notna()
    ok_cont = cont & (last.pred_time <= last.next_trip_poll + 60) \
                   & (last.pred_time >= last.poll_ts - 300)
    # Trip itself ended after this poll: accept only imminent predictions.
    ended = ~cont
    ok_end = ended & ((last.pred_time - last.poll_ts).abs() <= POLL_INTERVAL)

    actuals = last[ok_cont | ok_end].copy()
    actuals = actuals.rename(columns={"pred_time": "actual_time",
                                      "poll_ts": "final_obs_poll"})
    return actuals[["trip_key", "trip_id", "route_id", "direction_id", "start_date",
                    "stop_id", "stop_sequence", "actual_time", "final_obs_poll"]]


def load_schedule(gtfs_dir: Path, trip_ids: set[str]) -> pd.DataFrame:
    st = pd.read_csv(gtfs_dir / "stop_times.txt",
                     usecols=["trip_id", "stop_id", "stop_sequence",
                              "arrival_time", "departure_time"],
                     dtype={"trip_id": str, "stop_id": str})
    st = st[st.trip_id.isin(trip_ids)]

    def to_unix(hms: str, service_date: str) -> float:
        h, m, s = map(int, hms.split(":"))
        base = datetime.strptime(service_date, "%Y%m%d").replace(tzinfo=TZ)
        return (base + timedelta(hours=h, minutes=m, seconds=s)).timestamp()

    st = st.rename(columns={"arrival_time": "sched_arrival_hms",
                            "departure_time": "sched_departure_hms"})
    st.attrs["to_unix"] = to_unix
    return st


def fmt_pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def report(actuals: pd.DataFrame, preds: pd.DataFrame, gtfs_dir: Path) -> None:
    sched = load_schedule(gtfs_dir, set(actuals.trip_id))
    to_unix = sched.attrs["to_unix"]

    joined = actuals.merge(sched, on=["trip_id", "stop_id"], how="inner",
                           suffixes=("", "_sched"))
    join_rate = len(joined) / max(len(actuals), 1)
    print(f"\n=== Data overview ===")
    days = preds.poll_ts.agg(["min", "max"])
    print(f"Observation window: {datetime.fromtimestamp(days['min'], TZ):%Y-%m-%d %H:%M} "
          f"to {datetime.fromtimestamp(days['max'], TZ):%Y-%m-%d %H:%M}")
    print(f"Derived actual stop passages: {len(actuals):,} "
          f"({actuals.trip_key.nunique():,} trips)")
    print(f"GTFS trip_id join rate: {fmt_pct(join_rate)}")
    if join_rate < 0.5:
        print("WARNING: poor trip_id match between 511 realtime and the SFMTA GTFS.\n"
              "The schedule feed may be a different vintage than the realtime feed.\n"
              "Re-download data/gtfs_sfmta from "
              "https://muni-gtfs.apps.sfmta.com/data/muni_gtfs-current.zip and rerun.")
        if join_rate == 0:
            return

    joined["sched_time"] = [
        to_unix(h, d) for h, d in zip(joined.sched_arrival_hms, joined.start_date)
    ]
    joined["delay_s"] = joined.actual_time - joined.sched_time
    joined = joined[joined.delay_s.between(-1800, 3600)]  # drop unmatched garbage

    print("\n=== 1. Schedule adherence (all observed stops) ===")
    print("SFMTA on-time definition: no more than 1 min early / 4 min late\n")
    for route, g in joined.groupby("route_id"):
        ontime = g.delay_s.between(ONTIME_EARLY, ONTIME_LATE).mean()
        late = (g.delay_s > ONTIME_LATE).mean()
        early = (g.delay_s < ONTIME_EARLY).mean()
        med = g.delay_s.median() / 60
        p90 = g.delay_s.quantile(0.9) / 60
        print(f"  Route {route:>3}: on-time {fmt_pct(ontime)}, late {fmt_pct(late)}, "
              f"early {fmt_pct(early)} | median delay {med:+.1f} min, p90 {p90:+.1f} min "
              f"(n={len(g):,})")

    print("\n=== 2. Terminal departures (first stop of each trip) ===")
    seq = joined.dropna(subset=["stop_sequence"])
    first = seq.loc[seq.groupby("trip_key").stop_sequence.idxmin()]
    first = first[first.stop_sequence <= 2]
    for route, g in first.groupby("route_id"):
        ontime = g.delay_s.between(ONTIME_EARLY, ONTIME_LATE).mean()
        late = (g.delay_s > ONTIME_LATE).mean()
        med = g.delay_s.median() / 60
        print(f"  Route {route:>3}: departed on-time {fmt_pct(ontime)}, "
              f">4 min late {fmt_pct(late)} | median {med:+.1f} min (n={len(g):,})")

    print("\n=== 3. Real-time prediction accuracy (what the signs show) ===")
    print("Error = predicted arrival - actual arrival, by prediction horizon\n")
    snap = preds.merge(
        actuals[["trip_key", "stop_id", "actual_time", "final_obs_poll"]],
        on=["trip_key", "stop_id"], how="inner")
    snap = snap[snap.poll_ts < snap.final_obs_poll]  # exclude the converged snapshot
    snap["horizon_min"] = (snap.pred_time - snap.poll_ts) / 60
    snap["err_min"] = (snap.pred_time - snap.actual_time) / 60
    snap = snap[snap.horizon_min.between(0, 30)]
    bins = [0, 3, 6, 10, 15, 30]
    snap["bucket"] = pd.cut(snap.horizon_min, bins,
                            labels=["0-3", "3-6", "6-10", "10-15", "15-30"])
    for route, g in snap.groupby("route_id"):
        print(f"  Route {route}:")
        for b, gb in g.groupby("bucket", observed=True):
            mae = gb.err_min.abs().mean()
            within1 = (gb.err_min.abs() <= 1).mean()
            within2 = (gb.err_min.abs() <= 2).mean()
            bias = gb.err_min.mean()
            print(f"    {b:>5} min out: MAE {mae:.1f} min, within ±1 min {fmt_pct(within1)}, "
                  f"±2 min {fmt_pct(within2)}, bias {bias:+.1f} (n={len(gb):,})")

    print("\n=== 4. Schedule vs real-time divergence ===")
    print("How often the live estimate disagreed with the printed schedule:\n")
    snap2 = snap.merge(sched, on=["trip_id", "stop_id"], how="inner",
                       suffixes=("", "_s2"))
    snap2["sched_time"] = [
        to_unix(h, d) for h, d in zip(snap2.sched_arrival_hms, snap2.start_date)
    ]
    snap2["diverge_min"] = (snap2.pred_time - snap2.sched_time).abs() / 60
    for route, g in snap2.groupby("route_id"):
        gt2 = (g.diverge_min > 2).mean()
        gt5 = (g.diverge_min > 5).mean()
        print(f"  Route {route:>3}: live estimate off schedule by >2 min in {fmt_pct(gt2)} "
              f"of snapshots, >5 min in {fmt_pct(gt5)} (n={len(g):,})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=HERE / "data" / "muni.db")
    ap.add_argument("--gtfs", type=Path, default=HERE / "data" / "gtfs_sfmta")
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"{args.db} not found - run collect.py first")
    preds = load_predictions(args.db)
    if preds.empty:
        raise SystemExit("No predictions collected yet - let collect.py run longer")
    actuals = derive_actuals(preds)
    report(actuals, preds, args.gtfs)


if __name__ == "__main__":
    main()
