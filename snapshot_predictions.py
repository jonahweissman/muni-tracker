# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "gtfs-realtime-bindings>=1.0.0",
#   "requests>=2.31",
# ]
# ///
"""One-shot snapshot of 511 GTFS-RT trip updates (predictions) for Muni
routes 1/33/38/38R. Designed to run on a schedule (GitHub Actions cron).

Writes snapshots/YYYY-MM-DD/HHMMSSZ.csv.gz (UTC). Ground truth arrives
later via the 511 monthly '-so' archive; join on trip_id + stop_sequence
+ service_date, so cadence and jitter don't affect accuracy — only
sample count.
"""

import csv
import gzip
import io
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

ROUTES = {"SF:1", "SF:33", "SF:38", "SF:38R", "1", "33", "38", "38R"}
HERE = Path(__file__).parent


def main() -> None:
    token = os.environ.get("TRANSIT_511_TOKEN", "").strip()
    if not token and (HERE / ".token").exists():
        token = (HERE / ".token").read_text().strip()
    if not token:
        sys.exit("TRANSIT_511_TOKEN not set")

    r = requests.get(
        f"https://api.511.org/transit/tripupdates?api_key={token}&agency=SF",
        timeout=60,
    )
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)

    now = int(time.time())
    rows = []
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        if tu.trip.route_id not in ROUTES:
            continue
        vid = tu.vehicle.id if tu.HasField("vehicle") else ""
        for stu in tu.stop_time_update:
            rows.append([
                now, feed.header.timestamp or "",
                tu.trip.trip_id, tu.trip.start_date or "",
                tu.trip.route_id,
                tu.trip.direction_id if tu.trip.HasField("direction_id") else "",
                vid, stu.stop_id,
                stu.stop_sequence if stu.HasField("stop_sequence") else "",
                stu.arrival.time if stu.HasField("arrival") and stu.arrival.time else "",
                stu.departure.time if stu.HasField("departure") and stu.departure.time else "",
                stu.schedule_relationship,
            ])

    utc = datetime.fromtimestamp(now, timezone.utc)
    out_dir = HERE / "snapshots" / utc.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (utc.strftime("%H%M%S") + "Z.csv.gz")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["poll_ts", "feed_ts", "trip_id", "start_date", "route_id",
                "direction_id", "vehicle_id", "stop_id", "stop_sequence",
                "pred_arrival_ts", "pred_departure_ts", "schedule_relationship"])
    w.writerows(rows)
    with gzip.open(out, "wt", newline="") as f:
        f.write(buf.getvalue())
    print(f"{out}: {len(rows)} prediction rows")

    # Vehicle positions: independent GPS record of where each bus actually
    # is, so stop passages can be bracketed without trusting predictions.
    try:
        r = requests.get(
            f"https://api.511.org/transit/vehiclepositions?api_key={token}&agency=SF",
            timeout=60,
        )
        r.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(r.content)
        vrows = []
        for ent in feed.entity:
            if not ent.HasField("vehicle"):
                continue
            v = ent.vehicle
            if v.trip.route_id not in ROUTES:
                continue
            vrows.append([
                now, v.trip.trip_id, v.trip.start_date or "", v.trip.route_id,
                v.vehicle.id or "",
                round(v.position.latitude, 6) if v.HasField("position") else "",
                round(v.position.longitude, 6) if v.HasField("position") else "",
                v.stop_id or "",
                v.current_stop_sequence if v.HasField("current_stop_sequence") else "",
                v.current_status, v.timestamp or "",
            ])
        vout = out_dir / (utc.strftime("%H%M%S") + "Z-vp.csv.gz")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["poll_ts", "trip_id", "start_date", "route_id", "vehicle_id",
                    "lat", "lon", "stop_id", "current_stop_sequence",
                    "current_status", "vehicle_ts"])
        w.writerows(vrows)
        with gzip.open(vout, "wt", newline="") as f:
            f.write(buf.getvalue())
        print(f"{vout}: {len(vrows)} vehicle positions")
    except Exception as e:  # predictions are the primary record; never fail on VP
        print(f"vehicle positions skipped: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
