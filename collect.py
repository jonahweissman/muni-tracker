# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "gtfs-realtime-bindings>=1.0.0",
#   "requests>=2.31",
# ]
# ///
"""Poll 511.org GTFS-realtime feeds for SF Muni routes 1, 33, 38.

Records every prediction snapshot (TripUpdates) and vehicle position to
SQLite so we can later compare scheduled vs predicted vs actual times.

Usage:
    TRANSIT_511_TOKEN=xxxx uv run collect.py
or put the token in a file named .token in this directory.

511 rate limit is 60 requests/hour per token; we make 2 requests per
cycle at a 150s interval (48/hour).
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

ROUTES = {"1", "33", "38", "38R"}  # 38R included: riders often treat 38/38R interchangeably
AGENCY = "SF"
INTERVAL_S = 150
BASE = "http://api.511.org/transit"
HERE = Path(__file__).parent
DB_PATH = HERE / "data" / "muni.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    poll_ts INTEGER NOT NULL,          -- unix time of our poll
    feed_ts INTEGER,                   -- feed header timestamp
    trip_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    direction_id INTEGER,
    start_date TEXT,                   -- service date YYYYMMDD
    vehicle_id TEXT,
    stop_id TEXT NOT NULL,
    stop_sequence INTEGER,
    arrival_time INTEGER,              -- predicted, unix time
    departure_time INTEGER,            -- predicted, unix time
    schedule_relationship TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_trip_stop ON predictions(trip_id, stop_id, poll_ts);
CREATE TABLE IF NOT EXISTS vehicle_positions (
    poll_ts INTEGER NOT NULL,
    trip_id TEXT,
    route_id TEXT,
    vehicle_id TEXT,
    lat REAL, lon REAL,
    stop_id TEXT,
    current_stop_sequence INTEGER,
    current_status TEXT,
    vehicle_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_vp_trip ON vehicle_positions(trip_id, poll_ts);
CREATE TABLE IF NOT EXISTS poll_log (
    poll_ts INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    http_status INTEGER,
    n_entities INTEGER,
    error TEXT
);
"""


def get_token() -> str:
    tok = os.environ.get("TRANSIT_511_TOKEN", "").strip()
    if not tok:
        f = HERE / ".token"
        if f.exists():
            tok = f.read_text().strip()
    if not tok:
        sys.exit(
            "No 511 API token found. Get a free one at https://511.org/open-data/token\n"
            "then either `export TRANSIT_511_TOKEN=...` or write it to a file named .token"
        )
    return tok


def fetch_feed(endpoint: str, token: str) -> tuple[gtfs_realtime_pb2.FeedMessage | None, int, str]:
    url = f"{BASE}/{endpoint}?api_key={token}&agency={AGENCY}"
    try:
        r = requests.get(url, timeout=30)
    except requests.RequestException as e:
        return None, 0, str(e)
    if r.status_code != 200:
        return None, r.status_code, r.text[:200]
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(r.content)
    except Exception as e:
        return None, r.status_code, f"parse error: {e}"
    return feed, r.status_code, ""


def poll_once(db: sqlite3.Connection, token: str) -> str:
    now = int(time.time())
    status_parts = []

    feed, http, err = fetch_feed("tripupdates", token)
    n = 0
    if feed:
        feed_ts = feed.header.timestamp or None
        rows = []
        for ent in feed.entity:
            if not ent.HasField("trip_update"):
                continue
            tu = ent.trip_update
            route = tu.trip.route_id
            if route not in ROUTES:
                continue
            n += 1
            vid = tu.vehicle.id if tu.HasField("vehicle") else None
            for stu in tu.stop_time_update:
                rows.append((
                    now, feed_ts, tu.trip.trip_id, route,
                    tu.trip.direction_id if tu.trip.HasField("direction_id") else None,
                    tu.trip.start_date or None, vid,
                    stu.stop_id,
                    stu.stop_sequence if stu.HasField("stop_sequence") else None,
                    stu.arrival.time if stu.HasField("arrival") and stu.arrival.time else None,
                    stu.departure.time if stu.HasField("departure") and stu.departure.time else None,
                    gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(
                        stu.schedule_relationship),
                ))
        db.executemany("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT INTO poll_log VALUES (?,?,?,?,?)",
               (now, "tripupdates", http, n, err or None))
    status_parts.append(f"tripupdates: {n} trips" if not err else f"tripupdates ERR {http} {err}")

    feed, http, err = fetch_feed("vehiclepositions", token)
    n = 0
    if feed:
        rows = []
        for ent in feed.entity:
            if not ent.HasField("vehicle"):
                continue
            v = ent.vehicle
            route = v.trip.route_id
            if route not in ROUTES:
                continue
            n += 1
            rows.append((
                now, v.trip.trip_id or None, route,
                v.vehicle.id or None,
                v.position.latitude if v.HasField("position") else None,
                v.position.longitude if v.HasField("position") else None,
                v.stop_id or None,
                v.current_stop_sequence if v.HasField("current_stop_sequence") else None,
                gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name(v.current_status),
                v.timestamp or None,
            ))
        db.executemany("INSERT INTO vehicle_positions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT INTO poll_log VALUES (?,?,?,?,?)",
               (now, "vehiclepositions", http, n, err or None))
    status_parts.append(f"positions: {n} vehicles" if not err else f"positions ERR {http} {err}")

    db.commit()
    return "; ".join(status_parts)


def main() -> None:
    token = get_token()
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    print(f"Collecting routes {sorted(ROUTES)} every {INTERVAL_S}s into {DB_PATH}")
    print("Stop with Ctrl-C. Aim for at least 2-3 days of data.")
    while True:
        started = time.time()
        try:
            status = poll_once(db, token)
        except sqlite3.Error as e:
            status = f"db error: {e}"
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {status}", flush=True)
        time.sleep(max(0, INTERVAL_S - (time.time() - started)))


if __name__ == "__main__":
    main()
