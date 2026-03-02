#!/usr/bin/env python3
"""
Live TradingNode for BTC up/down markets.

Usage:
    python live/node.py --market-slug btc-updown-15m-XXXXXXXXXX
"""
import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv
from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.adapters.polymarket import (
    PolymarketLiveDataClientFactory,
    PolymarketLiveExecClientFactory,
)
from nautilus_trader.live.node import TradingNode

load_dotenv()

# Project root → enables `from live.* import ...`
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from live.config import build_node_config
from live.strategies.btc_updown import BtcUpDownConfig, BtcUpDownStrategy

GAMMA = "https://gamma-api.polymarket.com"


def resolve_pm_instrument_id(market_slug: str) -> str:
    """Return YES token instrument ID ('TOKEN_ID.POLYMARKET') for a market slug."""
    resp = requests.get(f"{GAMMA}/markets", params={"slug": market_slug})
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        sys.exit(f"No market found for slug: {market_slug}")
    market = markets[0]
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    if not token_ids:
        sys.exit(f"No token IDs for market: {market_slug}")
    return f"{token_ids[0]}.POLYMARKET"


def main():
    parser = argparse.ArgumentParser(description="Live BTC up/down trading node")
    parser.add_argument("--market-slug", required=True, help="Polymarket market slug")
    args = parser.parse_args()

    pm_instrument_id = resolve_pm_instrument_id(args.market_slug)
    print(f"PM instrument: {pm_instrument_id}")

    node_config = build_node_config(pm_instrument_ids=[pm_instrument_id])
    node = TradingNode(config=node_config)

    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_data_client_factory("POLYMARKET", PolymarketLiveDataClientFactory)
    node.add_exec_client_factory("POLYMARKET", PolymarketLiveExecClientFactory)

    strategy = BtcUpDownStrategy(BtcUpDownConfig(pm_instrument_id=pm_instrument_id))
    node.trader.add_strategy(strategy)

    node.build()
    node.run()


if __name__ == "__main__":
    main()
