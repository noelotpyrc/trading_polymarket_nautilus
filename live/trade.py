#!/usr/bin/env python3
"""
Place a market order (BUY or SELL) on Polymarket.

Usage:
    # BUY: amount is USDC to spend
    python live/trade.py --event bitcoin-above-on-march-1 --side BUY --amount 5

    # SELL: amount is shares to sell
    python live/trade.py --event bitcoin-above-on-march-1 --side SELL --amount 6.58

    # Show your recent trades
    python live/trade.py --trades
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    PartialCreateOrderOptions,
    OrderType,
    TradeParams,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from live.env import add_env_file_arg, bootstrap_env_file, load_project_env

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()

HOST = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CHAIN_ID = 137


def make_client() -> ClobClient:
    client = ClobClient(
        host=HOST,
        key=os.environ["PRIVATE_KEY"],
        chain_id=CHAIN_ID,
        signature_type=0,
    )
    client.set_api_creds(ApiCreds(
        api_key=os.environ["POLYMARKET_API_KEY"],
        api_secret=os.environ["POLYMARKET_API_SECRET"],
        api_passphrase=os.getenv("POLYMARKET_PASSPHRASE") or os.environ["POLYMARKET_API_PASSPHRASE"],
    ))
    return client


def lookup_market(event_slug: str) -> list[dict]:
    """Return list of sub-markets for a Polymarket event slug."""
    resp = requests.get(f"{GAMMA}/events", params={"slug": event_slug})
    resp.raise_for_status()
    events = resp.json()
    if not events:
        print(f"No event found for slug: {event_slug}")
        sys.exit(1)
    return events[0]["markets"]


def pick_market(markets: list[dict]) -> dict:
    """Print sub-markets and prompt user to pick one."""
    print("\nSub-markets:")
    for i, m in enumerate(markets):
        prices = json.loads(m.get("outcomePrices", "[]"))
        token_ids = json.loads(m.get("clobTokenIds", "[]"))
        yes_price = prices[0] if prices else "?"
        print(f"  [{i}] {m['slug']}")
        print(f"      YES price: {yes_price}  |  YES token: {token_ids[0] if token_ids else '?'}")
    idx = int(input("\nPick market index: "))
    return markets[idx]


def get_tick_size(client: ClobClient, condition_id: str) -> str:
    """Fetch tick size from CLOB for the market."""
    resp = requests.get(f"{HOST}/markets/{condition_id}")
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("minimum_tick_size", "0.01"))


def show_trades(client: ClobClient):
    """Print recent trade history."""
    trades = client.get_trades(TradeParams())
    if not trades:
        print("No trade history found.")
        return
    print(f"\nRecent trades ({len(trades)} total):\n")
    for t in trades[-20:]:  # show last 20
        print(
            f"  {t.get('match_time', '')[:19]}  "
            f"{t.get('side', ''):4}  "
            f"{t.get('outcome', ''):3}  "
            f"price={t.get('price', ''):6}  "
            f"size={t.get('size', ''):8}  "
            f"status={t.get('status', '')}"
        )


def sync_conditional(client: ClobClient, token_id: str):
    """Sync Polymarket's API record of outcome token balance (required before SELL)."""
    client.update_balance_allowance(BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id=token_id,
    ))
    bal = client.get_balance_allowance(BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id=token_id,
    ))
    balance = int(bal.get("balance", 0)) / 1e6
    print(f"  Conditional balance: {balance:.4f} shares")


def place_order(client: ClobClient, token_id: str, side: str, amount: float, tick_size: str):
    """Create and post a market FOK order."""
    if side == "SELL":
        print("\nSyncing conditional token balance with Polymarket API...")
        sync_conditional(client, token_id)

    print(f"\nPlacing {side} order: amount={amount} tick_size={tick_size} token={token_id[:12]}...")

    order = client.create_market_order(
        MarketOrderArgs(token_id=token_id, amount=amount, side=side),
        options=PartialCreateOrderOptions(tick_size=tick_size),
    )
    resp = client.post_order(order, OrderType.FOK)

    print(f"Response: {resp}")
    if resp.get("success"):
        print(f"\n✓ Order filled — status: {resp.get('status')}")
    else:
        print(f"\n✗ Order failed — {resp}")


def main(argv: list[str] | None = None):
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = argparse.ArgumentParser(description="Place a Polymarket market order")
    add_env_file_arg(parser)
    parser.add_argument("--event", help="Event slug (e.g. bitcoin-above-on-march-1)")
    parser.add_argument("--side", choices=["BUY", "SELL"])
    parser.add_argument("--amount", type=float, help="USDC for BUY, shares for SELL")
    parser.add_argument("--trades", action="store_true", help="Show recent trade history")
    args = parser.parse_args(argv)

    client = make_client()

    if args.trades:
        show_trades(client)
        return

    if not args.event or not args.side or args.amount is None:
        parser.error("--event, --side, and --amount are required for placing orders")

    markets = lookup_market(args.event)
    market = pick_market(markets)
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    yes_token_id = token_ids[0]
    tick_size = get_tick_size(client, market["conditionId"])

    print(f"\nMarket : {market['slug']}")
    print(f"Side   : {args.side}")
    print(f"Amount : {args.amount} {'USDC' if args.side == 'BUY' else 'shares'}")
    confirm = input("\nConfirm order? [y/N]: ")
    if confirm.lower() != "y":
        print("Cancelled.")
        return

    place_order(client, yes_token_id, args.side, args.amount, tick_size)


if __name__ == "__main__":
    main()
