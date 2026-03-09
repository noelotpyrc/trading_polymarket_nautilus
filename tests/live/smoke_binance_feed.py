#!/usr/bin/env python3
"""Smoke test: Binance perpetual futures data feed.

Connects to Binance, subscribes to BTCUSDT-PERP 1-minute bars for RUN_SECS seconds.
No API key required (Binance public data).
May require VPN if Binance international is geo-restricted.

Usage:
    python tests/live/smoke_binance_feed.py
    python tests/live/smoke_binance_feed.py --secs 120
"""
import argparse
import os
import sys
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.config import InstrumentProviderConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.test_kit.strategies.tester_data import DataTester, DataTesterConfig

INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
BAR_TYPE = "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"


def main():
    parser = argparse.ArgumentParser(description="Binance feed smoke test")
    parser.add_argument("--secs", type=int, default=90, help="Seconds to run (default: 90)")
    args = parser.parse_args()

    print(f"=== Binance Feed Smoke Test ({args.secs}s) ===")
    print(f"Instrument : {INSTRUMENT_ID}")
    print(f"Bar type   : {BAR_TYPE}")
    print("API key    : not required (public data)")
    print("VPN        : may be needed if Binance international is geo-restricted\n")

    node_config = TradingNodeConfig(
        data_clients={
            "BINANCE": BinanceDataClientConfig(
                account_type=BinanceAccountType.USDT_FUTURES,
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset([INSTRUMENT_ID]),
                ),
            ),
        },
        exec_clients={},
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)

    tester = DataTester(
        DataTesterConfig(
            instrument_ids=[InstrumentId.from_str(INSTRUMENT_ID)],
            bar_types=[BarType.from_str(BAR_TYPE)],
            subscribe_bars=True,
            subscribe_instrument=True,
        )
    )
    node.trader.add_actor(tester)
    node.build()

    # Stop after args.secs — timer fires node.stop() from background thread
    threading.Timer(args.secs, node.stop).start()

    print(f"Running for {args.secs}s — bar data will appear in the logs above...\n")
    node.run()

    print("\n=== Done. ===")
    print("PASS if 1-minute bars appeared in the log.")
    print("Note: bars arrive on the minute mark, so you may see 0 or 1 bar in a short run.")


if __name__ == "__main__":
    main()
