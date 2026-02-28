"""
Test script to fetch closed and archived markets from Polymarket using NautilusTrader.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from nautilus_trader.adapters.polymarket import PolymarketDataLoader


async def fetch_markets_batch(
    closed: bool = True,
    archived: bool = True,
    active: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Fetch a batch of markets using PolymarketDataLoader."""
    return await PolymarketDataLoader.fetch_markets(
        active=active,
        closed=closed,
        archived=archived,
        limit=limit,
        offset=offset,
    )


async def count_all_closed_markets(max_markets: int = 500) -> tuple[int, list[dict]]:
    """Paginate through closed markets and count them."""
    all_markets = []
    offset = 0
    limit = 100
    
    print("Fetching closed/archived markets using NautilusTrader...")
    
    while True:
        batch = await fetch_markets_batch(
            closed=True,
            archived=True,
            active=False,
            limit=limit,
            offset=offset,
        )
        
        if not batch:
            break
            
        all_markets.extend(batch)
        print(f"  Fetched {len(all_markets)} markets so far...")
        offset += limit
        
        # Safety limit for testing
        if len(all_markets) >= max_markets:
            print(f"  (Stopping at {max_markets} for testing purposes)")
            break
    
    return len(all_markets), all_markets


async def main():
    print("=" * 60)
    print("Polymarket Closed Markets Test (NautilusTrader)")
    print("=" * 60)
    
    # First, fetch a small sample to understand the data structure
    print("\n1. Fetching sample of 5 closed markets...")
    sample = await fetch_markets_batch(closed=True, archived=True, limit=5)
    
    if sample:
        print(f"   Retrieved {len(sample)} markets")
        print("\n   Sample market fields:")
        first = sample[0]
        key_fields = ["id", "question", "closed", "archived", "active", 
                      "closedTime", "endDate", "volume", "category"]
        for field in key_fields:
            value = first.get(field, "N/A")
            if isinstance(value, str) and len(value) > 60:
                value = value[:60] + "..."
            print(f"   - {field}: {value}")
    
    # Count how many closed markets exist
    print("\n2. Counting closed/archived markets (up to 500)...")
    count, markets = await count_all_closed_markets(max_markets=500)
    print(f"\n   Total fetched: {count} markets")
    
    # Analyze the data
    if markets:
        print("\n3. Analyzing market statuses:")
        closed_count = sum(1 for m in markets if m.get("closed"))
        archived_count = sum(1 for m in markets if m.get("archived"))
        active_count = sum(1 for m in markets if m.get("active"))
        
        print(f"   - closed=true: {closed_count}")
        print(f"   - archived=true: {archived_count}")
        print(f"   - active=true: {active_count}")
        
        # Categories breakdown
        print("\n4. Category breakdown:")
        categories = {}
        for m in markets:
            cat = m.get("category", "Unknown")
            categories[cat] = categories.get(cat, 0) + 1
        
        for cat, cnt in sorted(categories.items(), key=lambda x: -x[1])[:10]:
            print(f"   - {cat}: {cnt}")
        
        # Date range
        print("\n5. Date range of closed markets:")
        close_times = [m.get("closedTime") for m in markets if m.get("closedTime")]
        if close_times:
            close_times.sort()
            print(f"   - Earliest: {close_times[0]}")
            print(f"   - Latest: {close_times[-1]}")
        
        # Save to JSON
        print("\n6. Saving markets to JSON...")
        output_file = Path(__file__).parent / "closed_markets.json"
        with open(output_file, "w") as f:
            json.dump(markets, f, indent=2, default=str)
        print(f"   Saved {len(markets)} markets to: {output_file}")
    
    print("\n" + "=" * 60)
    print("Test complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
