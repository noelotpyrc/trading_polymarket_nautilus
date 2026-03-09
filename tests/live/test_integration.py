"""Integration tests for BtcUpDownStrategy using BacktestEngine.

Tests that the strategy correctly:
- Buys YES token on bullish BTC signal (last close > first close)
- Does not enter twice in the same window
- Does not buy on bearish signal
- Rolls to the next window on time alert
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.model.currencies import USDC, USDT
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from live.strategies.btc_updown import BtcUpDownConfig, BtcUpDownStrategy

class InspectableBtcUpDownStrategy(BtcUpDownStrategy):
    def __init__(self, config: BtcUpDownConfig):
        super().__init__(config)
        self.submitted_orders = []
        self.submitted_quote_flags = []

    def submit_order(self, order, *args, **kwargs):
        self.submitted_orders.append(order)
        self.submitted_quote_flags.append(order.is_quote_quantity)
        return super().submit_order(order, *args, **kwargs)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_TS_S = 1_700_000_000  # arbitrary epoch (2023-11-14T22:13:20 UTC)
INTERVAL_S = 900           # 15-minute window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    """Fresh BacktestEngine with BINANCE + POLYMARKET venues and test instruments."""
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    pm = TestInstrumentProvider.binary_option()
    window_end_ns = (BASE_TS_S + INTERVAL_S) * 1_000_000_000

    engine = BacktestEngine(config=BacktestEngineConfig(trader_id="TESTER-001"))
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
        base_currency=USDC,
        starting_balances=[Money(500, USDC)],
    )
    engine.add_instrument(btc)
    engine.add_instrument(pm)
    return engine, btc, pm, window_end_ns


def _btc_bars(btc, closes: list[float], base_ts_s: int = BASE_TS_S) -> list[Bar]:
    """Create 1-minute BTC perp bars from a list of close prices."""
    bar_type = BarType.from_str(f"{btc.id}-1-MINUTE-LAST-EXTERNAL")
    bars = []
    for i, close in enumerate(closes):
        ts_ns = int((base_ts_s + i * 60) * 1_000_000_000)
        bars.append(Bar(
            bar_type=bar_type,
            open=btc.make_price(close - 10),
            high=btc.make_price(close + 10),
            low=btc.make_price(close - 10),
            close=btc.make_price(close),
            volume=btc.make_qty(1.0),
            ts_event=ts_ns,
            ts_init=ts_ns,
        ))
    return bars


def _pm_quotes(pm, bid: float = 0.50, ask: float = 0.52,
               base_ts_s: int = BASE_TS_S, n: int = 10) -> list[QuoteTick]:
    """Create n PM quote ticks at 1-minute intervals starting from base_ts_s."""
    quotes = []
    for i in range(n):
        ts_ns = int((base_ts_s + i * 60) * 1_000_000_000)
        quotes.append(QuoteTick(
            instrument_id=pm.id,
            bid_price=pm.make_price(bid),
            ask_price=pm.make_price(ask),
            bid_size=pm.make_qty(100.0),
            ask_size=pm.make_qty(100.0),
            ts_event=ts_ns,
            ts_init=ts_ns,
        ))
    return quotes


def _strategy(pm, window_end_ns: int, signal_lookback: int = 5) -> InspectableBtcUpDownStrategy:
    return InspectableBtcUpDownStrategy(BtcUpDownConfig(
        pm_instrument_ids=(str(pm.id),),
        window_end_times_ns=(window_end_ns,),
        signal_lookback=signal_lookback,
        trade_amount_usdc=5.0,
    ))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBtcUpDownStrategyIntegration:

    def test_buys_on_bullish_signal(self):
        """Strategy submits 1 BUY order when BTC close trend is up."""
        engine, btc, pm, window_end_ns = _make_engine()

        # 7 bars with upward closes: signal fires on bar 6 (oldest < newest)
        closes = [50_000.0 + i * 100 for i in range(7)]
        engine.add_data(_btc_bars(btc, closes))
        engine.add_data(_pm_quotes(pm))

        strategy = _strategy(pm, window_end_ns)
        engine.add_strategy(strategy)
        engine.run()

        orders = engine.cache.orders()
        buy_orders = [o for o in orders if o.side == OrderSide.BUY]
        assert len(buy_orders) == 1
        assert buy_orders[0].instrument_id == pm.id
        assert strategy.submitted_quote_flags[0] is True
        engine.dispose()

    def test_no_duplicate_entries_per_window(self):
        """Strategy enters at most once per window despite repeated bullish signals."""
        engine, btc, pm, window_end_ns = _make_engine()

        # 12 bullish bars — signal fires on bars 6–12, but only 1 entry allowed
        closes = [50_000.0 + i * 100 for i in range(12)]
        engine.add_data(_btc_bars(btc, closes))
        engine.add_data(_pm_quotes(pm, n=12))

        strategy = _strategy(pm, window_end_ns)
        engine.add_strategy(strategy)
        engine.run()

        buy_orders = [o for o in engine.cache.orders() if o.side == OrderSide.BUY]
        assert len(buy_orders) == 1
        engine.dispose()

    def test_no_entry_on_bearish_signal(self):
        """Strategy does not buy when BTC close trend is down."""
        engine, btc, pm, window_end_ns = _make_engine()

        # 7 bars with downward closes
        closes = [51_000.0 - i * 100 for i in range(7)]
        engine.add_data(_btc_bars(btc, closes))
        engine.add_data(_pm_quotes(pm))

        strategy = _strategy(pm, window_end_ns)
        engine.add_strategy(strategy)
        engine.run()

        assert len(engine.cache.orders()) == 0
        engine.dispose()

    def test_no_entry_on_flat_signal(self):
        """Strategy does not buy when BTC close is flat (first == last)."""
        engine, btc, pm, window_end_ns = _make_engine()

        # Bars that start and end at the same price
        closes = [50_000.0, 50_100.0, 49_900.0, 50_000.0, 50_200.0, 50_000.0]
        engine.add_data(_btc_bars(btc, closes))
        engine.add_data(_pm_quotes(pm))

        strategy = _strategy(pm, window_end_ns)
        engine.add_strategy(strategy)
        engine.run()

        assert len(engine.cache.orders()) == 0
        engine.dispose()

    def test_no_entry_before_lookback_fills(self):
        """Strategy does not buy until signal_lookback+1 bars have arrived."""
        engine, btc, pm, window_end_ns = _make_engine()

        # Only 5 bars — not enough for signal_lookback=5 (need 6)
        closes = [50_000.0 + i * 100 for i in range(5)]
        engine.add_data(_btc_bars(btc, closes))
        engine.add_data(_pm_quotes(pm))

        strategy = _strategy(pm, window_end_ns, signal_lookback=5)
        engine.add_strategy(strategy)
        engine.run()

        assert len(engine.cache.orders()) == 0
        engine.dispose()

    def test_window_roll_stops_when_no_next_window(self):
        """Strategy stops itself when time alert fires and no next window is configured."""
        engine, btc, pm, window_end_ns = _make_engine()

        # Bars that extend past the window end so the time alert fires
        n_bars = INTERVAL_S // 60 + 5  # 20 bars — past 15m window end
        closes = [50_000.0 + i * 50 for i in range(n_bars)]
        engine.add_data(_btc_bars(btc, closes))
        engine.add_data(_pm_quotes(pm, n=n_bars))

        strategy = _strategy(pm, window_end_ns)
        engine.add_strategy(strategy)
        engine.run()

        # Strategy should have stopped itself (no crash, clean run)
        assert strategy._window_idx == 1  # advanced past the single window
        engine.dispose()
