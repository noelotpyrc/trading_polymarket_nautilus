#!/usr/bin/env python3
"""Redeem a single resolved live Polymarket position by market slug."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import requests

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(PROJECT_ROOT))

from live.env import add_env_file_arg, bootstrap_env_file, load_project_env
from live.market_metadata import ResolvedWindowMetadata, WindowMetadataRegistry
from live.node import _parse_interval_secs, _window_metadata_from_market
from live.redemption import DEFAULT_POLYGON_RPC_URL, ProdRedemptionExecutor
from live.resolution import fetch_market_resolution
from live.wallet_truth import ProdWalletTruthProvider, make_polymarket_balance_client

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()

GAMMA = "https://gamma-api.polymarket.com"


def main(argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_arg_parser()
    args = parser.parse_args(argv)

    metadata = resolve_market_metadata_by_slug(args.market_slug, outcome_side=args.outcome_side)
    registry = WindowMetadataRegistry([metadata])
    balance_client, funder = make_polymarket_balance_client(sandbox=False)
    provider = ProdWalletTruthProvider(
        wallet_address=funder,
        balance_client=balance_client,
        registry=registry,
    )
    snapshot = provider.snapshot()
    positions = tuple(position for position in snapshot.positions if position.condition_id == metadata.condition_id)

    print(f"Market slug  : {metadata.slug}")
    print(f"Condition    : {metadata.condition_id}")
    print(f"Window end   : {_fmt_utc(metadata.window_end_ns)}")
    print(f"Wallet       : {snapshot.wallet_address}")
    print(f"Collateral   : {snapshot.collateral_balance:.6f} USDC.e")
    print(f"Positions    : {len(positions)}")
    for position in positions:
        print(
            f"  - {position.outcome_side.upper()} {position.outcome_label or '?'} "
            f"token={position.token_id} size={position.size:.6f} "
            f"redeemable={position.redeemable} mergeable={position.mergeable}"
        )

    if not positions:
        raise SystemExit("No wallet position found for that market.")

    resolution = fetch_market_resolution(metadata.condition_id, metadata.selected_token_id)
    print(
        "Resolution   : "
        f"resolved={resolution.resolved} winning_outcome={resolution.winning_outcome} "
        f"winning_token={resolution.winning_token_id}"
    )
    if not resolution.resolved:
        raise SystemExit("Market is not resolved yet; cannot redeem.")

    if not args.execute and not args.yes:
        confirm = input("\nRun one-off redemption in dry-run mode? [y/N]: ")
        if confirm.lower() != "y":
            raise SystemExit("Operator cancelled dry-run redemption.")
    if args.execute and not args.yes:
        confirm = input("\nExecute live redemption transaction now? [y/N]: ")
        if confirm.lower() != "y":
            raise SystemExit("Operator cancelled live redemption.")

    executor = ProdRedemptionExecutor(
        private_key=os.environ["PRIVATE_KEY"],
        wallet_address=os.getenv("POLYMARKET_FUNDER") or os.environ["WALLET_ADDRESS"],
        rpc_url=args.rpc_url,
        dry_run=not args.execute,
    )
    results = executor.settle(positions=positions, resolution=resolution)

    print("\nRedemption results:")
    for result in results:
        settlement = "n/a" if result.settlement_price is None else f"{result.settlement_price:.2f}"
        tx = "" if result.transaction_hash is None else f" tx={result.transaction_hash}"
        print(
            f"  - {result.instrument_id} size={result.position_size:.6f} "
            f"status={result.status} settled={settlement}{tx}"
        )


def _make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Redeem a single resolved market position by slug")
    add_env_file_arg(parser)
    parser.add_argument("--market-slug", required=True, help="Exact Polymarket market slug")
    parser.add_argument(
        "--outcome-side",
        choices=("yes", "no"),
        default="yes",
        help="Selected outcome side for metadata lookup (default: yes)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the redemption transaction instead of a dry-run summary",
    )
    parser.add_argument(
        "--rpc-url",
        default=DEFAULT_POLYGON_RPC_URL,
        help=f"Polygon RPC URL for live redemptions (default: {DEFAULT_POLYGON_RPC_URL})",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    return parser


def resolve_market_metadata_by_slug(market_slug: str, *, outcome_side: str) -> ResolvedWindowMetadata:
    response = requests.get(f"{GAMMA}/markets", params={"slug": market_slug}, timeout=10)
    response.raise_for_status()
    markets = response.json()
    if not markets:
        raise SystemExit(f"No market found for slug: {market_slug}")
    market = markets[0]
    window_end_ns = _window_end_ns_for_market_slug(market_slug, market)
    metadata = _window_metadata_from_market(
        slug=market_slug,
        market=market,
        outcome_side=outcome_side,
        window_end_ns=window_end_ns,
    )
    if metadata is None:
        raise SystemExit(f"Could not resolve metadata for market slug: {market_slug}")
    return metadata


def _window_end_ns_for_market_slug(market_slug: str, market: dict) -> int:
    try:
        interval_secs = _parse_interval_secs(market_slug)
        window_start = int(market_slug.rsplit("-", 1)[1])
        return (window_start + interval_secs) * 1_000_000_000
    except Exception:
        end_raw = (
            market.get("closedTime")
            or market.get("umaEndDate")
            or market.get("endDate")
            or market.get("end_date_iso")
        )
        if not end_raw:
            raise SystemExit(f"Could not determine window end for market slug: {market_slug}")
        return _parse_timestamp_ns(str(end_raw))


def _parse_timestamp_ns(value: str) -> int:
    normalized = value.strip().replace("Z", "+00:00").replace(" ", "T")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def _fmt_utc(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


if __name__ == "__main__":
    main()
