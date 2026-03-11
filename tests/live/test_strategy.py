"""Unit tests for live strategy pure logic."""
from datetime import datetime, timezone

import pytest

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from live.strategies.btc_updown import BtcUpDownConfig, BtcUpDownStrategy, compute_signal

BTC = TestInstrumentProvider.btcusdt_perp_binance()
BTC_BAR_TYPE = BarType.from_str(f"{BTC.id}-1-MINUTE-LAST-EXTERNAL")


def _btc_bar(close: float, ts_ns: int) -> Bar:
    return Bar(
        bar_type=BTC_BAR_TYPE,
        open=BTC.make_price(close - 10),
        high=BTC.make_price(close + 10),
        low=BTC.make_price(close - 10),
        close=BTC.make_price(close),
        volume=BTC.make_qty(1.0),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


class WarmupHarness(BtcUpDownStrategy):
    def __init__(self, config: BtcUpDownConfig):
        super().__init__(config)
        self.canceled_timer_names = []
        self.requests = []
        self.scheduled_alerts = []
        self.entry_checks = []
        self.now_ns = 0
        self.stop_called = False

    def request_bars(self, bar_type, start, end=None, callback=None, **kwargs):
        self.requests.append(
            {
                "bar_type": bar_type,
                "start": start,
                "end": end,
                "callback": callback,
            }
        )
        return "REQ-1"

    def stop(self):
        self.stop_called = True

    def _now_ns(self) -> int:
        return self.now_ns

    def _set_guard_time_alert(self, name: str, alert_time_ns: int, callback) -> None:
        self.scheduled_alerts.append((name, alert_time_ns, callback))

    def _cancel_guard_timer(self, name: str) -> None:
        self.canceled_timer_names.append(name)

    def _check_entry_exit(self, signal_ts_ns: int) -> None:
        self.entry_checks.append(signal_ts_ns)


def _warmup_strategy(*, signal_lookback: int = 2, warmup_days: int = 14) -> WarmupHarness:
    return WarmupHarness(
        BtcUpDownConfig(
            pm_instrument_ids=("a.POLYMARKET",),
            window_end_times_ns=(1_000_000_000,),
            signal_lookback=signal_lookback,
            warmup_days=warmup_days,
        )
    )


class TestComputeSignal:
    def test_bullish(self):
        # Last close > first close → bullish
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        assert compute_signal(closes) == 1

    def test_bearish(self):
        # Last close < first close → bearish
        closes = [105.0, 104.0, 103.0, 102.0, 101.0, 100.0]
        assert compute_signal(closes) == -1

    def test_neutral_flat(self):
        # Last == first → neutral (regardless of middle values)
        closes = [100.0, 110.0, 90.0, 100.0]
        assert compute_signal(closes) == 0

    def test_two_element_up(self):
        assert compute_signal([99.0, 100.0]) == 1

    def test_two_element_down(self):
        assert compute_signal([100.0, 99.0]) == -1

    def test_single_element_returns_neutral(self):
        assert compute_signal([100.0]) == 0

    def test_empty_returns_neutral(self):
        assert compute_signal([]) == 0

    def test_only_last_and_first_matter(self):
        # Middle values are irrelevant to the signal
        assert compute_signal([100.0, 50.0, 200.0, 150.0, 101.0]) == 1
        assert compute_signal([100.0, 50.0, 200.0, 150.0, 99.0]) == -1


class TestBtcWarmup:
    def test_rejects_negative_warmup_days(self):
        with pytest.raises(ValueError, match="warmup_days must be >= 0"):
            BtcUpDownStrategy(
                BtcUpDownConfig(
                    pm_instrument_ids=("a.POLYMARKET",),
                    window_end_times_ns=(1_000_000_000,),
                    warmup_days=-1,
                )
            )

    def test_start_btc_warmup_requests_expected_range(self):
        strategy = _warmup_strategy()
        now = datetime(2026, 3, 9, 15, 27, 42, tzinfo=timezone.utc)
        strategy.now_ns = 5_000

        strategy._start_btc_warmup(now=now)

        assert strategy._warmup_request_inflight is True
        assert strategy.requests == [
            {
                "bar_type": BTC_BAR_TYPE,
                "start": datetime(2026, 2, 23, 15, 27, tzinfo=timezone.utc),
                "end": datetime(2026, 3, 9, 15, 27, tzinfo=timezone.utc),
                "callback": strategy._on_warmup_complete,
            }
        ]
        assert strategy.scheduled_alerts == [
            ("btc_warmup_timeout", 300_000_005_000, strategy._on_warmup_timeout)
        ]

    def test_warmup_timeout_stops_strategy(self):
        strategy = _warmup_strategy()
        strategy.now_ns = 10_000

        strategy._start_btc_warmup(now=datetime(2026, 3, 9, 15, 27, tzinfo=timezone.utc))
        strategy._on_warmup_timeout(None)

        assert strategy._warmup_request_inflight is False
        assert strategy.stop_called is True

    def test_live_bars_buffer_until_warmup_complete(self):
        strategy = _warmup_strategy()

        strategy.on_bar(_btc_bar(100.0, 1_000))

        assert list(strategy._btc_closes) == []
        assert strategy._warmup_live_buffer == {1_000: 100.0}
        assert strategy.entry_checks == []

    def test_warmup_complete_merges_history_and_buffered_live_bars(self):
        strategy = _warmup_strategy(signal_lookback=2)
        strategy._start_btc_warmup(now=datetime(2026, 3, 9, 15, 27, tzinfo=timezone.utc))

        strategy.on_historical_data(_btc_bar(100.0, 1_000))
        strategy.on_historical_data(_btc_bar(101.0, 2_000))
        strategy.on_bar(_btc_bar(102.0, 3_000))

        strategy._on_warmup_complete("REQ-1")

        assert strategy._warmup_complete is True
        assert strategy._warmup_request_inflight is False
        assert strategy._warmup_history == {}
        assert strategy._warmup_live_buffer == {}
        assert list(strategy._btc_closes) == [100.0, 101.0, 102.0]
        assert strategy.entry_checks == [3_000]
        assert strategy.canceled_timer_names == ["btc_warmup_timeout"]

    def test_warmup_complete_dedupes_overlap_and_prefers_live_buffer(self):
        strategy = _warmup_strategy(signal_lookback=2)
        strategy._start_btc_warmup(now=datetime(2026, 3, 9, 15, 27, tzinfo=timezone.utc))

        strategy.on_historical_data(_btc_bar(100.0, 1_000))
        strategy.on_historical_data(_btc_bar(101.0, 2_000))
        strategy.on_bar(_btc_bar(101.5, 2_000))
        strategy.on_bar(_btc_bar(102.0, 3_000))

        strategy._on_warmup_complete("REQ-1")

        assert list(strategy._btc_closes) == [100.0, 101.5, 102.0]
        assert strategy.entry_checks == [3_000]

    def test_warmup_complete_with_no_historical_bars_stops_strategy(self):
        strategy = _warmup_strategy(signal_lookback=2)
        strategy._start_btc_warmup(now=datetime(2026, 3, 9, 15, 27, tzinfo=timezone.utc))
        strategy.on_bar(_btc_bar(102.0, 3_000))

        strategy._on_warmup_complete("REQ-1")

        assert strategy.stop_called is True
        assert strategy.entry_checks == []

    def test_first_post_warmup_live_bar_uses_history_to_form_signal(self):
        strategy = _warmup_strategy(signal_lookback=2)
        strategy._start_btc_warmup(now=datetime(2026, 3, 9, 15, 27, tzinfo=timezone.utc))

        strategy.on_historical_data(_btc_bar(100.0, 1_000))
        strategy.on_historical_data(_btc_bar(99.0, 2_000))

        strategy._on_warmup_complete("REQ-1")

        assert list(strategy._btc_closes) == [100.0, 99.0]
        assert strategy.entry_checks == []

        strategy.on_bar(_btc_bar(101.0, 3_000))

        assert list(strategy._btc_closes) == [100.0, 99.0, 101.0]
        assert strategy._compute_signal() == 1
        assert strategy.entry_checks == [3_000]

    def test_post_warmup_live_bar_uses_merged_history_and_buffered_live_bars(self):
        strategy = _warmup_strategy(signal_lookback=3)
        strategy._start_btc_warmup(now=datetime(2026, 3, 9, 15, 27, tzinfo=timezone.utc))

        strategy.on_historical_data(_btc_bar(100.0, 1_000))
        strategy.on_historical_data(_btc_bar(101.0, 2_000))
        strategy.on_bar(_btc_bar(102.0, 3_000))

        strategy._on_warmup_complete("REQ-1")

        assert list(strategy._btc_closes) == [100.0, 101.0, 102.0]
        assert strategy.entry_checks == []

        strategy.on_bar(_btc_bar(99.0, 4_000))

        assert list(strategy._btc_closes) == [100.0, 101.0, 102.0, 99.0]
        assert strategy._compute_signal() == -1
        assert strategy.entry_checks == [4_000]

    def test_stale_live_bar_blocks_signal_generation(self):
        strategy = _warmup_strategy(signal_lookback=2, warmup_days=0)

        strategy.now_ns = 60_000_000_000
        strategy.on_bar(_btc_bar(100.0, 60_000_000_000))
        strategy.now_ns = 120_000_000_000
        strategy.on_bar(_btc_bar(101.0, 120_000_000_000))
        strategy.now_ns = 331_000_000_000
        strategy.on_bar(_btc_bar(102.0, 180_000_000_000))

        assert list(strategy._btc_closes) == [100.0, 101.0, 102.0]
        assert strategy.entry_checks == []
        assert strategy._signal_guard_reason(180_000_000_000) == "BTC bar stale (151s old)"

    def test_gap_blocks_signal_until_contiguous_window_recovers(self):
        strategy = _warmup_strategy(signal_lookback=2, warmup_days=0)

        for ts_ns, close in [
            (60_000_000_000, 100.0),
            (120_000_000_000, 101.0),
            (240_000_000_000, 102.0),  # Missing 180s bar
            (300_000_000_000, 103.0),
            (360_000_000_000, 104.0),
        ]:
            strategy.now_ns = ts_ns
            strategy.on_bar(_btc_bar(close, ts_ns))

        assert strategy.entry_checks == [360_000_000_000]
        assert strategy._gap_recovery_bars == 0
        assert list(strategy._btc_closes) == [102.0, 103.0, 104.0]

    def test_warmup_gap_requires_post_gap_bars_before_signal(self):
        strategy = _warmup_strategy(signal_lookback=2)
        strategy.now_ns = 0
        strategy._start_btc_warmup(now=datetime(2026, 3, 9, 15, 27, tzinfo=timezone.utc))

        strategy.on_historical_data(_btc_bar(100.0, 60_000_000_000))
        strategy.on_historical_data(_btc_bar(101.0, 120_000_000_000))
        strategy.on_historical_data(_btc_bar(102.0, 240_000_000_000))  # Missing 180s bar

        strategy._on_warmup_complete("REQ-1")
        assert strategy.entry_checks == []

        strategy.now_ns = 300_000_000_000
        strategy.on_bar(_btc_bar(103.0, 300_000_000_000))
        assert strategy.entry_checks == []

        strategy.now_ns = 360_000_000_000
        strategy.on_bar(_btc_bar(104.0, 360_000_000_000))
        assert strategy.entry_checks == [360_000_000_000]
