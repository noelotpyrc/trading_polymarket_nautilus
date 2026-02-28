#!/usr/bin/env python3
"""
Clean raw price history JSON into analysis-ready format.

Filters to markets with exactly 15 in-window data points,
assigns minute indices, and flattens to a list of records.
"""

import csv
import json
import sys
from datetime import datetime, timezone


def clean_price_history(input_path: str, output_path: str) -> None:
    with open(input_path) as f:
        data = json.load(f)

    records = []
    dropped = []

    for slug, v in data.items():
        ph = v.get("price_history", [])
        start = v["market_timestamp"]
        end = start + 900  # 15 minutes

        in_window = [p for p in ph if start <= p["t"] < end]

        if len(in_window) != 15:
            dropped.append((slug, len(in_window)))
            continue

        for p in in_window:
            minute = (p["t"] - start) // 60
            minute_ts = start + minute * 60
            records.append({
                "market_timestamp": start,
                "datetime": datetime.fromtimestamp(minute_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "minute": minute,
                "t": p["t"],
                "snapshot_datetime": datetime.fromtimestamp(p["t"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "p": p["p"],
            })

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["market_timestamp", "datetime", "minute", "t", "snapshot_datetime", "p"])
        writer.writeheader()
        writer.writerows(records)

    print(f"Kept: {len(records) // 15} markets ({len(records)} rows)")
    print(f"Dropped: {len(dropped)} markets")
    if dropped:
        print("Dropped details:")
        for slug, count in dropped:
            print(f"  {slug}: {count} in-window points")


if __name__ == "__main__":
    input_path = sys.argv[1] if len(sys.argv) > 1 else "data/btc_15m_price_history.json"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "data/btc_15m_clean.csv"
    clean_price_history(input_path, output_path)
