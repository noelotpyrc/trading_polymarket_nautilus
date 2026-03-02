#!/usr/bin/env python3
"""
E2E Nautilus backtest: Binance 1m signal + Polymarket YES token price.

Usage:
    python historical/backtest/run.py \
      --binance-csv "/Volumes/Extreme SSD/trading_data/cex/ohlvc/binance_btcusdt_perp_1m/BTCUSDT-1m-merged.csv" \
      --market-slug btc-updown-15m-1770594300 \
      --start 2025-09-01 --end 2025-10-01
"""
import argparse
import asyncio
import os
import sys
from decimal import Decimal

import pandas as pd

# Project root → enables `from historical.* import ...` and `from live.* import ...`
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.adapters.polymarket import PolymarketDataLoader
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.model.currencies import BTC, USDC, USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.persistence.wranglers import BarDataWrangler

from historical.fetch.market_price_history import calculate_time_range, parse_slug_timestamp
from live.strategies.btc_updown import BtcUpDownConfig, BtcUpDownStrategy


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def _fetch_pm(slug: str, ts: int, duration_min: int):
    """Fetch Polymarket instrument + 1-min price history from CLOB."""
    start_ms, end_ms = calculate_time_range(
        ts, duration_min, buffer_hours_before=1, buffer_hours_after=0.25
    )
    loader = await PolymarketDataLoader.from_market_slug(slug)
    price_history = await loader.fetch_price_history(
        token_id=loader.token_id,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        fidelity=1,
    )
    return loader.instrument, price_history


# ---------------------------------------------------------------------------
# Instrument construction
# ---------------------------------------------------------------------------

def _build_btcusdt() -> CurrencyPair:
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol("BTCUSDT"), Venue("BINANCE")),
        raw_symbol=Symbol("BTCUSDT"),
        base_currency=BTC,
        quote_currency=USDT,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        size_precision=6,
        size_increment=Quantity.from_str("0.000001"),
        lot_size=None,
        max_quantity=None,
        min_quantity=None,
        max_notional=None,
        min_notional=None,
        max_price=None,
        min_price=None,
        margin_init=Decimal("0.0"),
        margin_maint=Decimal("0.0"),
        maker_fee=Decimal("0.0"),
        taker_fee=Decimal("0.0"),
        ts_event=0,
        ts_init=0,
    )


# ---------------------------------------------------------------------------
# Data wrangling
# ---------------------------------------------------------------------------

def _load_binance_bars(
    csv_path: str, start: pd.Timestamp, end: pd.Timestamp, instrument: CurrencyPair
) -> list:
    print(f"Loading Binance CSV ({csv_path})...")
    df = pd.read_csv(
        csv_path,
        usecols=["datetime_utc", "open", "high", "low", "close", "volume"],
    )
    df["datetime_utc"] = pd.to_datetime(df["datetime_utc"], utc=True)
    df = df.rename(columns={"datetime_utc": "timestamp"}).set_index("timestamp")
    df = df.loc[start:end]
    print(f"  {len(df)} bars in [{start.date()}, {end.date()}]")
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    return BarDataWrangler(bar_type=bar_type, instrument=instrument).process(df)


def _pm_history_to_bars(
    price_history: list, instrument, start: pd.Timestamp, end: pd.Timestamp
) -> list:
    if not price_history:
        return []
    records = [
        {"timestamp": pd.Timestamp(item["t"], unit="s", tz="UTC"), "close": float(item["p"])}
        for item in price_history
        if item.get("t") and item.get("p")
    ]
    df = pd.DataFrame(records).set_index("timestamp").sort_index()
    df["open"] = df["close"]
    df["high"] = df["close"]
    df["low"] = df["close"]
    df["volume"] = 0.0
    df = df.loc[start:end][["open", "high", "low", "close", "volume"]]
    print(f"  {len(df)} PM bars in [{start.date()}, {end.date()}]")
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    return BarDataWrangler(bar_type=bar_type, instrument=instrument).process(df)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="E2E Nautilus backtest")
    parser.add_argument("--binance-csv", required=True, help="Path to merged Binance 1m CSV")
    parser.add_argument("--market-slug", required=True, help="Polymarket market slug, e.g. btc-updown-15m-1770594300")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=500.0, help="Starting USDC capital")
    args = parser.parse_args()

    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")

    # --- Parse slug ---
    ts, duration_min = parse_slug_timestamp(args.market_slug)
    if ts is None:
        sys.exit(f"Cannot parse slug: {args.market_slug}")

    # --- Fetch PM data ---
    print(f"Fetching PM data for {args.market_slug}...")
    pm_instrument, price_history = asyncio.run(_fetch_pm(args.market_slug, ts, duration_min))
    print(f"  instrument : {pm_instrument.id}")
    print(f"  raw points : {len(price_history) if price_history else 0}")

    # --- Build data ---
    btcusdt = _build_btcusdt()
    btc_bars = _load_binance_bars(args.binance_csv, start, end, btcusdt)
    pm_bars = _pm_history_to_bars(price_history, pm_instrument, start, end)

    if not pm_bars:
        sys.exit("No PM price data in the given date range.")

    print(f"\nData ready: {len(btc_bars)} BTC bars | {len(pm_bars)} PM bars")

    # --- Engine ---
    engine = BacktestEngine(config=BacktestEngineConfig(trader_id="BACKTESTER-001"))

    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(0, USDT)],
    )
    engine.add_venue(
        venue=Venue("POLYMARKET"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=[Money(args.capital, USDC)],
        base_currency=USDC,
        bar_execution=True,
    )
    engine.add_instrument(btcusdt)
    engine.add_instrument(pm_instrument)
    engine.add_data(btc_bars)
    engine.add_data(pm_bars)

    strategy = BtcUpDownStrategy(
        BtcUpDownConfig(pm_instrument_id=str(pm_instrument.id))
    )
    engine.add_strategy(strategy)

    # --- Run ---
    print("\nRunning backtest...")
    engine.run()

    # --- Results ---
    # Note: "Cannot calculate exchange rate" warnings are benign — the engine
    # cannot map binary option token P&L to USDC without quote data. Fills and
    # signal logic are still correct.
    print("\n--- Results ---")
    orders_closed = engine.cache.orders_closed()
    print(f"  Signal fires : {strategy._trade_count} fills ({len(orders_closed)} orders closed)")
    account = engine.cache.account_for_venue(Venue("POLYMARKET"))
    if account:
        bal = account.balance_total(USDC)
        if bal is not None:
            print(f"  USDC balance : {bal}")
    if orders_closed:
        total_qty = sum(float(o.quantity) for o in orders_closed)
        print(f"  Total qty    : {total_qty:.6f} shares bought")
    engine.dispose()


if __name__ == "__main__":
    main()
