# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "duckdb>=1.0",
#   "pandas>=2.0",
# ]
# ///
"""Route-level Muni reliability from 511 historic stop_observations.

Usage: uv run analyze_historic.py [data/historic/SF-2026-06-so ...]
Defaults to every directory under data/historic. Prints per-route stats
and writes summary CSVs to data/out/ for charting.
"""

import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
ROUTES = ("SF:1", "SF:33", "SF:38", "SF:38R")
OUT = HERE / "data" / "out"

SECS = "(CAST(split_part({c}, ':', 1) AS INT)*3600 + CAST(split_part({c}, ':', 2) AS INT)*60 + CAST(split_part({c}, ':', 3) AS INT))"


def main() -> None:
    dirs = [Path(a) for a in sys.argv[1:]] or sorted((HERE / "data" / "historic").iterdir())
    files = [str(d / "stop_observations.txt") for d in dirs if (d / "stop_observations.txt").exists()]
    if not files:
        sys.exit("no stop_observations.txt found; run fetch_historic.py first")
    print(f"Analyzing: {', '.join(files)}\n")

    db = duckdb.connect()
    OUT.mkdir(exist_ok=True)
    obs_secs = SECS.format(c="observed_departure_time")
    sch_secs = SECS.format(c="scheduled_departure_time")

    db.execute(f"""
        CREATE VIEW obs AS
        SELECT route_id, direction_id, service_date, trip_id, stop_sequence,
               {obs_secs} - {sch_secs} AS delay_s,
               {sch_secs} AS sched_s
        FROM read_csv_auto({files}, header=true, all_varchar=false,
                           types={{'observed_departure_time':'VARCHAR',
                                   'scheduled_departure_time':'VARCHAR',
                                   'service_date':'VARCHAR'}})
        WHERE route_id IN {ROUTES}
          AND schedule_relationship = 0
          AND observed_departure_time IS NOT NULL AND observed_departure_time <> ''
          AND scheduled_departure_time IS NOT NULL AND scheduled_departure_time <> ''
    """)

    metric_sql = """
        count(*) AS n,
        round(100.0*avg((delay_s BETWEEN -60 AND 240)::INT), 1) AS ontime_pct,
        round(100.0*avg((delay_s > 240)::INT), 1) AS late_pct,
        round(100.0*avg((delay_s < -60)::INT), 1) AS early_pct,
        round(median(delay_s)/60.0, 1) AS med_delay_min,
        round(quantile_cont(delay_s, 0.9)/60.0, 1) AS p90_delay_min
    """

    print("=== All stops, SFMTA on-time definition (<=1 min early, <=4 min late) ===")
    q = db.execute(f"""
        SELECT route_id, {metric_sql} FROM obs
        WHERE abs(delay_s) < 3600
        GROUP BY route_id ORDER BY route_id
    """).df()
    print(q.to_string(index=False))
    q.to_csv(OUT / "adherence_all_stops.csv", index=False)

    print("\n=== Terminal departures (first stop of trip) ===")
    q = db.execute(f"""
        SELECT route_id, {metric_sql} FROM obs
        WHERE stop_sequence = 1 AND abs(delay_s) < 3600
        GROUP BY route_id ORDER BY route_id
    """).df()
    print(q.to_string(index=False))
    q.to_csv(OUT / "terminal_departures.csv", index=False)

    print("\n=== On-time % by hour of day (all stops) ===")
    q = db.execute(f"""
        SELECT (sched_s/3600)::INT % 24 AS hour,
               route_id,
               round(100.0*avg((delay_s BETWEEN -60 AND 240)::INT), 1) AS ontime_pct,
               count(*) AS n
        FROM obs WHERE abs(delay_s) < 3600
        GROUP BY 1, 2 ORDER BY 2, 1
    """).df()
    print(q.pivot(index="hour", columns="route_id", values="ontime_pct").to_string())
    q.to_csv(OUT / "ontime_by_hour.csv", index=False)

    print("\n=== Delay distribution (all stops, minutes late) ===")
    q = db.execute("""
        SELECT route_id,
               round(100.0*avg((delay_s < -60)::INT),1)          AS "early>1m",
               round(100.0*avg((delay_s BETWEEN -60 AND 240)::INT),1) AS "ontime",
               round(100.0*avg((delay_s > 240 AND delay_s <= 600)::INT),1)  AS "late4-10m",
               round(100.0*avg((delay_s > 600 AND delay_s <= 1200)::INT),1) AS "late10-20m",
               round(100.0*avg((delay_s > 1200)::INT),1)         AS "late>20m"
        FROM obs WHERE abs(delay_s) < 3600
        GROUP BY route_id ORDER BY route_id
    """).df()
    print(q.to_string(index=False))
    q.to_csv(OUT / "delay_distribution.csv", index=False)

    # Scheduled trips never observed at all ("ghost" candidates)
    print("\n=== Scheduled trips with zero observations ===")
    trips_files = [str(Path(f).parent / "trips.txt") for f in files]
    cal_files = [str(Path(f).parent / "calendar_dates.txt") for f in files]
    q = db.execute(f"""
        WITH sched AS (
            SELECT t.route_id, t.trip_id, c.date AS service_date
            FROM read_csv_auto({trips_files}, header=true) t
            JOIN read_csv_auto({cal_files}, header=true, types={{'date':'VARCHAR'}}) c
              ON t.service_id = c.service_id AND c.exception_type = 1
            WHERE t.route_id IN {ROUTES}
        ),
        seen AS (SELECT DISTINCT trip_id, service_date FROM obs)
        SELECT sched.route_id,
               count(*) AS scheduled_trips,
               round(100.0*avg((seen.trip_id IS NULL)::INT), 1) AS never_observed_pct
        FROM sched LEFT JOIN seen
          ON sched.trip_id = seen.trip_id AND sched.service_date = seen.service_date
        GROUP BY 1 ORDER BY 1
    """).df()
    print(q.to_string(index=False))
    q.to_csv(OUT / "unobserved_trips.csv", index=False)


if __name__ == "__main__":
    main()
