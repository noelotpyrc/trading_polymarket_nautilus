#!/usr/bin/env python3
"""
Fetch price history for all markets in a Polymarket events JSON file.

The script parses market slugs to extract timestamps and calculates 
appropriate time ranges. For example:
- btc-updown-15m-1770594300 -> market starts at Unix timestamp 1770594300
- The market is open for trading before the official start (buffer_before)
- The market ends 15 mins after start + buffer_after

Usage:
    python fetch_market_price_history.py \
        --input data/bitcoin_up_or_down_15m.json \
        --output data/bitcoin_15m_price_history.json \
        --buffer-before 24 --buffer-after 1 \
        --fidelity 1
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from nautilus_trader.adapters.polymarket import PolymarketDataLoader


def parse_slug_timestamp(slug: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Parse market slug to extract timestamp and duration.
    
    Examples:
        btc-updown-15m-1770594300 -> (1770594300, 15)
        btc-up-or-down-15m-1757724300 -> (1757724300, 15)
        btc-up-or-down-1h-1757724300 -> (1757724300, 60)
    
    Returns:
        (unix_timestamp, duration_minutes) or (None, None) if parsing fails
    """
    # Pattern: look for duration (15m, 1h, 4h, etc.) followed by timestamp
    pattern = r"(\d+)(m|h)-(\d{10})$"
    match = re.search(pattern, slug)
    
    if match:
        duration_value = int(match.group(1))
        duration_unit = match.group(2)
        timestamp = int(match.group(3))
        
        # Convert to minutes
        if duration_unit == "h":
            duration_minutes = duration_value * 60
        else:
            duration_minutes = duration_value
        
        return timestamp, duration_minutes
    
    return None, None


def calculate_time_range(
    timestamp: int,
    duration_minutes: int,
    buffer_hours_before: float = 24,
    buffer_hours_after: float = 1,
) -> Tuple[int, int]:
    """
    Calculate the time range for fetching price history.
    
    Args:
        timestamp: Unix timestamp when market officially starts
        duration_minutes: Duration of the market in minutes
        buffer_hours_before: Hours before start to begin fetching
        buffer_hours_after: Hours after end to stop fetching
    
    Returns:
        (start_time_ms, end_time_ms)
    """
    # Start time: timestamp - buffer_before
    start_time_s = timestamp - int(buffer_hours_before * 3600)
    
    # End time: timestamp + duration + buffer_after
    end_time_s = timestamp + (duration_minutes * 60) + int(buffer_hours_after * 3600)
    
    return start_time_s * 1000, end_time_s * 1000


async def fetch_price_history_for_market(
    market_slug: str,
    start_time_ms: int,
    end_time_ms: int,
    fidelity: int = 1,
) -> Optional[List[Dict]]:
    """Fetch price history for a single market by slug."""
    try:
        loader = await PolymarketDataLoader.from_market_slug(market_slug)
        price_history = await loader.fetch_price_history(
            token_id=loader.token_id,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            fidelity=fidelity,
        )
        # Convert to serializable format
        if price_history:
            return [
                {"t": p.get("t"), "p": p.get("p")} 
                for p in price_history
            ] if isinstance(price_history, list) else price_history
        return price_history
    except Exception as e:
        print(f"  Error fetching {market_slug}: {e}")
        return None


async def fetch_all_market_price_histories(
    events: List[Dict],
    buffer_hours_before: float = 24,
    buffer_hours_after: float = 1,
    fidelity: int = 1,
    max_markets: Optional[int] = None,
    delay_between_requests: float = 0.1,
) -> Dict[str, Any]:
    """Fetch price history for all markets in events list."""
    
    print(f"Buffer before: {buffer_hours_before} hours")
    print(f"Buffer after: {buffer_hours_after} hours")
    print(f"Fidelity: {fidelity} minute(s)")
    
    # Extract all market slugs from events
    market_slugs = []
    for event in events:
        markets = event.get("markets", [])
        for market in markets:
            slug = market.get("slug")
            if slug:
                market_slugs.append(slug)
    
    # Apply limit if specified
    if max_markets:
        market_slugs = market_slugs[:max_markets]
    
    print(f"Found {len(market_slugs)} markets to fetch")
    
    # Fetch price histories
    results: Dict[str, Any] = {}
    success_count = 0
    error_count = 0
    skipped_count = 0
    
    for i, slug in enumerate(market_slugs):
        if (i + 1) % 10 == 0 or i == 0:
            print(f"Processing market {i + 1}/{len(market_slugs)}: {slug}")
        
        # Parse slug to get timestamp and duration
        timestamp, duration_minutes = parse_slug_timestamp(slug)
        
        if timestamp is None:
            print(f"  Warning: Could not parse timestamp from slug: {slug}")
            results[slug] = {"error": "Could not parse timestamp from slug"}
            skipped_count += 1
            continue
        
        # Calculate time range for this market
        start_time_ms, end_time_ms = calculate_time_range(
            timestamp=timestamp,
            duration_minutes=duration_minutes,
            buffer_hours_before=buffer_hours_before,
            buffer_hours_after=buffer_hours_after,
        )
        
        # Log the time range for first few markets
        if i < 3:
            start_dt = pd.Timestamp(start_time_ms, unit="ms", tz="UTC")
            end_dt = pd.Timestamp(end_time_ms, unit="ms", tz="UTC")
            market_start = pd.Timestamp(timestamp, unit="s", tz="UTC")
            print(f"  Market start: {market_start}, Duration: {duration_minutes}m")
            print(f"  Fetch range: {start_dt} to {end_dt}")
        
        price_history = await fetch_price_history_for_market(
            market_slug=slug,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            fidelity=fidelity,
        )
        
        if price_history is not None:
            results[slug] = {
                "market_timestamp": timestamp,
                "duration_minutes": duration_minutes,
                "fetch_start_ms": start_time_ms,
                "fetch_end_ms": end_time_ms,
                "price_history": price_history,
            }
            success_count += 1
        else:
            results[slug] = {"error": "Failed to fetch"}
            error_count += 1
        
        # Small delay to avoid rate limiting
        if delay_between_requests > 0:
            await asyncio.sleep(delay_between_requests)
    
    print(f"\nCompleted: {success_count} success, {error_count} errors, {skipped_count} skipped")
    return results


async def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch price history for all markets in an events JSON file"
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Input events JSON file")
    parser.add_argument("--output", "-o", type=Path, required=True,
                        help="Output JSON file for price histories")
    parser.add_argument("--buffer-before", type=float, default=1,
                        help="Hours before market start to fetch (default: 1)")
    parser.add_argument("--buffer-after-min", type=float, default=10,
                        help="Minutes after market end to fetch (default: 10)")
    parser.add_argument("--fidelity", type=int, default=1,
                        help="Price resolution in minutes (default: 1)")
    parser.add_argument("--max-markets", type=int, default=None,
                        help="Max number of markets to fetch (for testing)")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="Delay between requests in seconds")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Only include markets on or after this date (YYYY-MM-DD)")
    
    args = parser.parse_args(argv)
    
    # Load events
    print(f"Loading events from {args.input}...")
    with open(args.input, "r") as f:
        events = json.load(f)
    print(f"Loaded {len(events)} events")
    
    # Filter by start date if specified
    if args.start_date:
        from datetime import datetime, timezone
        start_dt = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ts = int(start_dt.timestamp())
        print(f"Filtering for markets on or after {args.start_date}...")
        
        filtered_events = []
        for event in events:
            filtered_markets = []
            for market in event.get("markets", []):
                slug = market.get("slug", "")
                # Parse timestamp from slug
                parts = slug.split("-")
                if parts and parts[-1].isdigit():
                    ts = int(parts[-1])
                    if ts >= start_ts:
                        filtered_markets.append(market)
            if filtered_markets:
                event_copy = dict(event)
                event_copy["markets"] = filtered_markets
                filtered_events.append(event_copy)
        
        events = filtered_events
        print(f"After filtering: {len(events)} events")
    
    # Convert buffer_after from minutes to hours for the function
    buffer_after_hours = args.buffer_after_min / 60.0
    
    # Fetch price histories
    results = await fetch_all_market_price_histories(
        events=events,
        buffer_hours_before=args.buffer_before,
        buffer_hours_after=buffer_after_hours,
        fidelity=args.fidelity,
        max_markets=args.max_markets,
        delay_between_requests=args.delay,
    )
    
    # Write output
    print(f"\nWriting results to {args.output}...")
    with open(args.output, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Done! Saved {len(results)} market price histories")
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
