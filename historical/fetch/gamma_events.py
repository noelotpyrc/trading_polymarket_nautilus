#!/usr/bin/env python3
"""
Fetch Polymarket Gamma events within a time window, with pagination and date slicing.

Usage examples:
    python polymarket/fetch_gamma_events.py \
        --start-date 2024-01-01 --end-date 2024-01-31 \
        --closed true --volume-min 10000 --order volume --descending \
        --output-dir ./data/polymarket --format csv

The script queries: GET https://gamma-api.polymarket.com/events
and writes events to CSV/JSON/Parquet.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

try:
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover
    pd = None  # type: ignore


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


def parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lower = value.lower()
    if lower in {"1", "true", "t", "yes", "y"}:
        return True
    if lower in {"0", "false", "f", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")


def request_json(
    path: str,
    params: Dict[str, object],
    max_retries: int = 3,
    timeout_seconds: int = 30,
    sleep_between_retries_seconds: float = 1.0,
) -> List[Dict]:
    url = f"{GAMMA_BASE_URL}{path}"
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=timeout_seconds)
            response.raise_for_status()
            return response.json()  # type: ignore[return-value]
        except requests.exceptions.RequestException as exc:  # pragma: no cover
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(sleep_between_retries_seconds * (2**attempt))
                continue
            raise
    if last_error:
        raise last_error
    return []


def fetch_events_paged(
    base_params: Dict[str, object],
    page_limit: int = 500,
    max_pages: int = 200,
    per_page_delay_seconds: float = 0.0,
) -> List[Dict]:
    all_events: List[Dict] = []
    offset = 0
    for _ in range(max_pages):
        page_params = dict(base_params)
        page_params["limit"] = page_limit
        page_params["offset"] = offset
        batch = request_json("/events", page_params)
        if not batch:
            break
        all_events.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit
        if per_page_delay_seconds > 0:
            time.sleep(per_page_delay_seconds)
    return all_events


def date_chunks(
    start_iso: str, end_iso: str, chunk_days: int
) -> List[Tuple[str, str]]:
    start = datetime.strptime(start_iso, "%Y-%m-%d")
    end = datetime.strptime(end_iso, "%Y-%m-%d")
    if end < start:
        raise ValueError("end date before start date")
    chunks: List[Tuple[str, str]] = []
    cur = start
    delta = timedelta(days=chunk_days)
    while cur <= end:
        nxt = min(cur + delta - timedelta(days=1), end)
        chunks.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt + timedelta(days=1)
    return chunks


def fetch_events_in_window(
    start_date_min: str,
    end_date_max: str,
    base_params: Dict[str, object],
    chunk_days: int = 14,
    page_limit: int = 500,
    per_page_delay_seconds: float = 0.0,
    slice_by: str = "start",  # "start" or "end"
) -> List[Dict]:
    unique_events: List[Dict] = []
    seen_ids: set = set()
    for chunk_start, chunk_end in date_chunks(start_date_min, end_date_max, chunk_days):
        params = dict(base_params)
        if slice_by == "start":
            params["start_date_min"] = chunk_start
            params["start_date_max"] = chunk_end
        else:
            params["end_date_min"] = chunk_start
            params["end_date_max"] = chunk_end
        batch = fetch_events_paged(
            params,
            page_limit=page_limit,
            per_page_delay_seconds=per_page_delay_seconds,
        )
        for ev in batch:
            ev_id = ev.get("id")
            if ev_id in seen_ids:
                continue
            seen_ids.add(ev_id)
            unique_events.append(ev)
    return unique_events


def write_output(
    events: List[Dict],
    output_dir: Path,
    filename_stem: str,
    out_format: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path: Path
    if out_format == "json":
        path = output_dir / f"{filename_stem}.json"
        with path.open("w") as f:
            json.dump(events, f, indent=2)
        return path
    if out_format == "csv":
        if pd is None:  # pragma: no cover
            raise RuntimeError("pandas is required for CSV output")
        df = pd.DataFrame(events)
        path = output_dir / f"{filename_stem}.csv"
        df.to_csv(path, index=False)
        return path
    if out_format == "parquet":
        if pd is None:  # pragma: no cover
            raise RuntimeError("pandas is required for Parquet output")
        df = pd.DataFrame(events)
        path = output_dir / f"{filename_stem}.parquet"
        df.to_parquet(path, index=False)
        return path
    raise ValueError(f"Unsupported format: {out_format}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch Polymarket Gamma events for a time window")
    p.add_argument("--start-date", required=True, help="ISO date YYYY-MM-DD")
    p.add_argument("--end-date", required=True, help="ISO date YYYY-MM-DD")

    p.add_argument("--slice-by", choices=["start", "end"], default="start",
                   help="Slice window by start_date or end_date filters")
    p.add_argument("--chunk-days", type=int, default=14, help="Days per slice")
    p.add_argument("--page-limit", type=int, default=500, help="Page size per request")
    p.add_argument("--per-page-delay", type=float, default=0.0, help="Sleep seconds between pages")

    p.add_argument("--active", type=parse_bool, help="Filter by active status")
    p.add_argument("--closed", type=parse_bool, help="Filter by closed status")
    p.add_argument("--archived", type=parse_bool, help="Filter by archived status")
    p.add_argument("--volume-min", type=float, default=None)
    p.add_argument("--volume-max", type=float, default=None)
    p.add_argument("--liquidity-min", type=float, default=None)
    p.add_argument("--liquidity-max", type=float, default=None)
    p.add_argument("--tag", type=str, default=None)
    p.add_argument("--tag-id", type=int, default=None)
    p.add_argument("--tag-slug", type=str, default=None)
    p.add_argument("--related-tags", action="store_true", help="Include events with related tags (requires --tag-id)")

    p.add_argument("--order", type=str, default=None, help="Key to sort by (e.g., volume)")
    sort_group = p.add_mutually_exclusive_group()
    sort_group.add_argument("--ascending", action="store_true")
    sort_group.add_argument("--descending", action="store_true")

    p.add_argument("--output-dir", type=Path, default=Path("/Volumes/Extreme SSD/trading_data/polymarket/events"))
    p.add_argument("--filename", type=str, default=None, help="Output filename stem (without extension)")
    p.add_argument("--format", choices=["csv", "json", "parquet"], default="csv")
    return p


def build_base_params(args: argparse.Namespace) -> Dict[str, object]:
    params: Dict[str, object] = {}
    if args.active is not None:
        params["active"] = args.active
    if args.closed is not None:
        params["closed"] = args.closed
    if args.archived is not None:
        params["archived"] = args.archived
    if args.volume_min is not None:
        params["volume_min"] = args.volume_min
    if args.volume_max is not None:
        params["volume_max"] = args.volume_max
    if args.liquidity_min is not None:
        params["liquidity_min"] = args.liquidity_min
    if args.liquidity_max is not None:
        params["liquidity_max"] = args.liquidity_max
    if args.tag is not None:
        params["tag"] = args.tag
    if args.tag_id is not None:
        params["tag_id"] = args.tag_id
    if args.tag_slug is not None:
        params["tag_slug"] = args.tag_slug
    if args.related_tags:
        params["related_tags"] = True
    if args.order is not None:
        params["order"] = args.order
        params["ascending"] = True
        if args.descending:
            params["ascending"] = False
        if args.ascending:
            params["ascending"] = True
    return params


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    base_params = build_base_params(args)

    events = fetch_events_in_window(
        start_date_min=args.start_date,
        end_date_max=args.end_date,
        base_params=base_params,
        chunk_days=args.chunk_days,
        page_limit=args.page_limit,
        per_page_delay_seconds=args.per_page_delay,
        slice_by=args.slice_by,
    )

    unique_count = len(events)
    print(f"Fetched unique events: {unique_count}")

    start_safe = args.start_date.replace("-", "")
    end_safe = args.end_date.replace("-", "")
    filename_stem = args.filename or f"gamma_events_{args.slice_by}_{start_safe}_{end_safe}"
    out_path = write_output(events, args.output_dir, filename_stem, args.format)
    print(f"Wrote {unique_count} events to: {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


