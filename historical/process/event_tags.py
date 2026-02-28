#!/usr/bin/env python3
"""
Utility for analyzing and filtering Polymarket events by tags.

Usage:
    # List all unique tags with counts
    python event_tags_util.py list-tags --input data/gamma_events_start_20240101_20260208.json
    
    # Filter events by tag(s)
    python event_tags_util.py filter --input data/gamma_events_start_20240101_20260208.json \
        --tags nfl,sports --output data/nfl_events.json
    
    # Search tags by keyword
    python event_tags_util.py search-tags --input data/gamma_events_start_20240101_20260208.json \
        --keyword crypto
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Set


def load_events(path: Path) -> List[Dict]:
    """Load events from JSON file."""
    print(f"Loading events from {path}...")
    with open(path, "r") as f:
        events = json.load(f)
    print(f"Loaded {len(events):,} events")
    return events


def extract_all_tags(events: List[Dict]) -> Dict[str, Dict]:
    """Extract all unique tags with their metadata and counts."""
    tag_counts: Counter = Counter()
    tag_metadata: Dict[str, Dict] = {}
    
    for event in events:
        tags = event.get("tags", [])
        if not tags:
            continue
        for tag in tags:
            if isinstance(tag, dict):
                slug = tag.get("slug", "")
                if slug:
                    tag_counts[slug] += 1
                    if slug not in tag_metadata:
                        tag_metadata[slug] = {
                            "id": tag.get("id"),
                            "label": tag.get("label"),
                            "slug": slug,
                        }
    
    # Add counts to metadata
    for slug, meta in tag_metadata.items():
        meta["event_count"] = tag_counts[slug]
    
    return tag_metadata


def list_tags_command(args: argparse.Namespace) -> int:
    """List all unique tags with their counts."""
    events = load_events(args.input)
    tags = extract_all_tags(events)
    
    # Sort by count (descending) or by label (alphabetical)
    if args.sort == "count":
        sorted_tags = sorted(tags.values(), key=lambda t: -t["event_count"])
    else:
        sorted_tags = sorted(tags.values(), key=lambda t: t["label"].lower())
    
    # Apply limit
    if args.limit:
        sorted_tags = sorted_tags[:args.limit]
    
    print(f"\n{'='*60}")
    print(f"Found {len(tags):,} unique tags")
    print(f"{'='*60}\n")
    
    # Output format
    if args.format == "table":
        print(f"{'Count':>8}  {'Tag Label':<40}  {'Slug':<30}")
        print("-" * 82)
        for tag in sorted_tags:
            print(f"{tag['event_count']:>8,}  {tag['label']:<40}  {tag['slug']:<30}")
    elif args.format == "json":
        print(json.dumps(sorted_tags, indent=2))
    else:  # csv
        print("count,label,slug,id")
        for tag in sorted_tags:
            print(f"{tag['event_count']},{tag['label']},{tag['slug']},{tag['id']}")
    
    return 0


def search_tags_command(args: argparse.Namespace) -> int:
    """Search tags by keyword."""
    events = load_events(args.input)
    tags = extract_all_tags(events)
    
    keyword = args.keyword.lower()
    matches = [
        tag for tag in tags.values()
        if keyword in tag["label"].lower() or keyword in tag["slug"].lower()
    ]
    
    # Sort by count
    matches = sorted(matches, key=lambda t: -t["event_count"])
    
    print(f"\n{'='*60}")
    print(f"Found {len(matches)} tags matching '{args.keyword}'")
    print(f"{'='*60}\n")
    
    print(f"{'Count':>8}  {'Tag Label':<40}  {'Slug':<30}")
    print("-" * 82)
    for tag in matches:
        print(f"{tag['event_count']:>8,}  {tag['label']:<40}  {tag['slug']:<30}")
    
    return 0


def filter_events_command(args: argparse.Namespace) -> int:
    """Filter events by tag(s)."""
    events = load_events(args.input)
    
    # Parse target tags
    target_tags: Set[str] = set(t.strip().lower() for t in args.tags.split(","))
    print(f"Filtering for tags: {target_tags}")
    
    # Filter events
    filtered = []
    for event in events:
        event_tags = event.get("tags", [])
        if not event_tags:
            continue
        event_tag_slugs = {
            tag.get("slug", "").lower() 
            for tag in event_tags 
            if isinstance(tag, dict)
        }
        
        # Check matching mode
        if args.match == "any":
            if target_tags & event_tag_slugs:
                filtered.append(event)
        else:  # all
            if target_tags <= event_tag_slugs:
                filtered.append(event)
    
    print(f"\nFiltered {len(filtered):,} events (from {len(events):,} total)")
    
    # Optionally extract markets
    if args.extract_markets:
        all_markets = []
        for event in filtered:
            markets = event.get("markets", [])
            for market in markets:
                # Add event metadata to market
                market["_event_id"] = event.get("id")
                market["_event_title"] = event.get("title")
                market["_event_tags"] = [t.get("slug") for t in event.get("tags", []) if isinstance(t, dict)]
                all_markets.append(market)
        print(f"Extracted {len(all_markets):,} markets from filtered events")
        output_data = all_markets
    else:
        output_data = filtered
    
    # Write output
    output_path = args.output or Path(f"filtered_events_{args.tags.replace(',', '_')}.json")
    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)
    print(f"Wrote output to: {output_path}")
    
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Utility for analyzing and filtering Polymarket events by tags"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # list-tags command
    list_p = subparsers.add_parser("list-tags", help="List all unique tags with counts")
    list_p.add_argument("--input", "-i", type=Path, required=True, help="Input JSON file")
    list_p.add_argument("--sort", choices=["count", "alpha"], default="count", 
                        help="Sort by count or alphabetically")
    list_p.add_argument("--limit", "-n", type=int, default=None, help="Limit number of results")
    list_p.add_argument("--format", choices=["table", "json", "csv"], default="table")
    list_p.set_defaults(func=list_tags_command)
    
    # search-tags command
    search_p = subparsers.add_parser("search-tags", help="Search tags by keyword")
    search_p.add_argument("--input", "-i", type=Path, required=True, help="Input JSON file")
    search_p.add_argument("--keyword", "-k", required=True, help="Keyword to search for")
    search_p.set_defaults(func=search_tags_command)
    
    # filter command
    filter_p = subparsers.add_parser("filter", help="Filter events by tag(s)")
    filter_p.add_argument("--input", "-i", type=Path, required=True, help="Input JSON file")
    filter_p.add_argument("--tags", "-t", required=True, 
                          help="Comma-separated tag slugs to filter by")
    filter_p.add_argument("--match", choices=["any", "all"], default="any",
                          help="Match any or all tags")
    filter_p.add_argument("--output", "-o", type=Path, help="Output JSON file")
    filter_p.add_argument("--extract-markets", action="store_true",
                          help="Extract individual markets instead of events")
    filter_p.set_defaults(func=filter_events_command)
    
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
