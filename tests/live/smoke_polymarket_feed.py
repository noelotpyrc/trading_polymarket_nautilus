#!/usr/bin/env python3
"""Smoke test: Polymarket quote tick data feed.

Connects to Polymarket, subscribes to quote ticks for the current BTC up/down 15m market.
Requires POLYMARKET_TEST_* credentials (zero-funds test wallet, no real money at risk).

Setup (one-time):
    python live/setup/generate_wallet.py --test
    python live/setup/init_trading.py --test

Usage:
    python tests/live/smoke_polymarket_feed.py
    python tests/live/smoke_polymarket_feed.py --secs 60
"""
import argparse
import json
import os
import sys
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.adapters.polymarket import (
    PolymarketDataClientConfig,
    PolymarketLiveDataClientFactory,
)
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.test_kit.strategies.tester_data import DataTester, DataTesterConfig

GAMMA = "https://gamma-api.polymarket.com"
SLUG_PATTERN = "btc-updown-15m"


def _resolve_current_market() -> str | None:
    """Resolve the current active BTC up/down 15m market from Gamma API."""
    interval_secs = 900
    now = int(time.time())
    window_start = (now // interval_secs) * interval_secs
    slug = f"{SLUG_PATTERN}-{window_start}"
    print(f"Resolving: {slug}")
    try:
        resp = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        markets = resp.json()
    except Exception as exc:
        print(f"Gamma API error: {exc}")
        return None

    if not markets:
        print(f"No market found for slug: {slug}")
        return None

    condition_id = markets[0].get("conditionId", "")
    token_ids = json.loads(markets[0].get("clobTokenIds", "[]"))
    if not condition_id or not token_ids:
        print("Missing conditionId or tokenIds in API response")
        return None

    instrument_id = f"{condition_id}-{token_ids[0]}.POLYMARKET"
    print(f"Instrument : {instrument_id}")
    return instrument_id


def main():
    parser = argparse.ArgumentParser(description="Polymarket feed smoke test")
    parser.add_argument("--secs", type=int, default=30, help="Seconds to run (default: 30)")
    args = parser.parse_args()

    print(f"=== Polymarket Feed Smoke Test ({args.secs}s) ===")
    print("Credentials: POLYMARKET_TEST_* (test wallet, no real funds)\n")

    instrument_id_str = _resolve_current_market()
    if not instrument_id_str:
        sys.exit("Could not resolve current market. Check Gamma API or try again.")

    # Load test credentials (zero-funds wallet)
    try:
        private_key = os.environ["POLYMARKET_TEST_PRIVATE_KEY"]
        api_key = os.environ["POLYMARKET_TEST_API_KEY"]
        api_secret = os.environ["POLYMARKET_TEST_API_SECRET"]
        passphrase = os.environ["POLYMARKET_TEST_API_PASSPHRASE"]
        funder = os.environ["POLYMARKET_TEST_WALLET_ADDRESS"]
    except KeyError as e:
        sys.exit(
            f"Missing env var: {e}\n"
            "Run: python live/setup/generate_wallet.py --test && python live/setup/init_trading.py --test"
        )

    node_config = TradingNodeConfig(
        data_clients={
            "POLYMARKET": PolymarketDataClientConfig(
                private_key=private_key,
                api_key=api_key,
                api_secret=api_secret,
                passphrase=passphrase,
                funder=funder,
                instrument_config=PolymarketInstrumentProviderConfig(
                    load_ids=frozenset([instrument_id_str]),
                ),
            ),
        },
        exec_clients={},
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("POLYMARKET", PolymarketLiveDataClientFactory)

    tester = DataTester(
        DataTesterConfig(
            instrument_ids=[InstrumentId.from_str(instrument_id_str)],
            subscribe_quotes=True,
            can_unsubscribe=True,
        )
    )
    node.trader.add_actor(tester)
    node.build()

    # Stop after args.secs — timer fires node.stop() from background thread
    threading.Timer(args.secs, node.stop).start()

    print(f"\nRunning for {args.secs}s — quote ticks will appear in the logs above...\n")
    node.run()

    print("\n=== Done. ===")
    print("PASS if QuoteTick lines appeared in the log.")


if __name__ == "__main__":
    main()
