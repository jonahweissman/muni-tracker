# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests>=2.31",
# ]
# ///
"""Download 511.org monthly historic GTFS feeds with stop observations.

The '-so' variant of the historic feed includes stop_observations.txt:
observed real-time arrival times at all stops for all trips, alongside
that month's static schedule. Available since 2022-03.

Usage:
    uv run fetch_historic.py 2026-06 [2026-05 ...]     # operator SF (Muni)
    uv run fetch_historic.py --operator RG 2026-06     # whole region

Token: env TRANSIT_511_TOKEN or a .token file in this directory.
Output: data/historic/<operator>-<month>-so/ (unzipped)
"""

import argparse
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import requests

HERE = Path(__file__).parent
OUT = HERE / "data" / "historic"


def get_token() -> str:
    tok = os.environ.get("TRANSIT_511_TOKEN", "").strip()
    if not tok and (HERE / ".token").exists():
        tok = (HERE / ".token").read_text().strip()
    if not tok:
        sys.exit("No 511 token. Get one at https://511.org/open-data/token, "
                 "then export TRANSIT_511_TOKEN=... or write it to .token")
    return tok


def fetch_month(month: str, operator: str, token: str) -> None:
    dest = OUT / f"{operator}-{month}-so"
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} already exists, skipping")
        return
    url = (f"https://api.511.org/transit/datafeeds?api_key={token}"
           f"&operator_id={operator}&historic={month}-so")
    print(f"Fetching {operator} {month}-so ...", flush=True)
    r = requests.get(url, timeout=600)
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text[:300]}")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = z.namelist()
        z.extractall(dest)
        # some feeds nest a zip inside the zip; unpack one level if so
        for n in names:
            if n.endswith(".zip"):
                with zipfile.ZipFile(dest / n) as inner:
                    inner.extractall(dest / Path(n).stem)
    print(f"  -> {dest}: {', '.join(sorted(p.name for p in dest.iterdir()))}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("months", nargs="+", help="e.g. 2026-06")
    ap.add_argument("--operator", default="SF", help="511 operator id (SF=Muni, RG=region)")
    args = ap.parse_args()
    token = get_token()
    for i, m in enumerate(args.months):
        if i:
            time.sleep(65)  # stay under the 60 req/hr limit politely
        fetch_month(m, args.operator, token)


if __name__ == "__main__":
    main()
