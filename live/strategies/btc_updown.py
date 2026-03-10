"""BTC up/down infrastructure test strategy for the live process.

Signal: BTC momentum over N 1-minute bars.
Execution: Polymarket YES token via quote ticks.
Window transitions: driven by time alerts, not data timestamps.
"""
from collections import deque
from datetime import datetime, timedelta, timezone

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType

from live.strategies.windowed import WindowedPolymarketStrategy, _fmt_ns


class BtcUpDownConfig(StrategyConfig, frozen=True):
    # Ordered list of pre-resolved windows: current first, then upcoming.
    # Both tuples must be the same length.
    pm_instrument_ids: tuple[str, ...]
    window_end_times_ns: tuple[int, ...]

    btc_bar_type: str = "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"
    trade_amount_usdc: float = 5.0
    signal_lookback: int = 5
    warmup_days: int = 0


class BtcUpDownStrategy(WindowedPolymarketStrategy):
    _STATUS_INTERVAL_NS = 30_000_000_000  # 30 seconds

    def __init__(self, config: BtcUpDownConfig):
        super().__init__(config)
        if config.warmup_days < 0:
            raise ValueError("warmup_days must be >= 0")

        self._btc_bar_type = BarType.from_str(config.btc_bar_type)
        self._trade_amount = config.trade_amount_usdc
        self._signal_lookback = config.signal_lookback
        self._btc_closes: deque[float] = deque(maxlen=config.signal_lookback + 1)
        self._warmup_days = config.warmup_days
        self._warmup_complete = config.warmup_days == 0
        self._warmup_request_inflight = False
        self._warmup_history: dict[int, float] = {}
        self._warmup_live_buffer: dict[int, float] = {}
        self._last_btc_bar_ts_ns: int | None = None

    def on_start(self):
        self.subscribe_bars(self._btc_bar_type)
        self._start_btc_warmup()
        self._start_window_lifecycle()
        self.clock.set_timer_ns(
            "status",
            self._STATUS_INTERVAL_NS,
            0,
            0,
            self._on_status_timer,
        )
        self.log.info(
            f"Started | PM={self._pm_instrument_id} "
            f"window_end={_fmt_ns(self._window_end_ns)} UTC | "
            f"{len(self._windows)} window(s) pre-loaded | "
            f"warmup={self._warmup_status_str()}"
        )

    def on_stop(self):
        self.clock.cancel_timer("status")
        self._stop_window_lifecycle()
        self.log.info(f"Stopped | total fills: {self._trade_count}")

    def on_bar(self, bar: Bar):
        close = float(bar.close)
        if not self._warmup_complete:
            self._warmup_live_buffer[bar.ts_event] = close
            self.log.info(
                f"BTC close={bar.close} | WARMING UP "
                f"(hist={len(self._warmup_history)} buffered={len(self._warmup_live_buffer)})"
            )
            return

        self._process_live_bar(bar.ts_event, close, close_str=str(bar.close))

    def on_historical_data(self, data):
        if (
            self._warmup_complete
            or not self._warmup_request_inflight
            or not isinstance(data, Bar)
            or data.bar_type != self._btc_bar_type
        ):
            return

        self._warmup_history[data.ts_event] = float(data.close)

    def _on_status_timer(self, event) -> None:
        entered_str = "YES" if self._entered_this_window else "no"
        self.log.info(
            f"[heartbeat] PM={self._quote_state_str(self.clock.timestamp_ns())} | "
            f"window_end={_fmt_ns(self._window_end_ns)} UTC | "
            f"entered={entered_str} | fills={self._trade_count} | "
            f"warmup={self._warmup_status_str()}"
        )

    def _on_window_end(self, event) -> None:
        self._roll_to_next_window(
            exhausted_message=(
                "No more pre-loaded windows — stopping. "
                "Restart the node for the next session."
            ),
        )

    def _check_entry_exit(self, signal_ts_ns: int) -> None:
        signal = self._compute_signal()
        positions = self.cache.positions_open(instrument_id=self._pm_instrument_id)

        if positions and signal == -1:
            for pos in positions:
                self.close_position(pos)
            return

        if (
            not positions
            and not self._entered_this_window
            and not self._entry_order_pending
            and signal == 1
        ):
            reason = self._entry_guard_reason(signal_ts_ns)
            if reason is not None:
                self.log.info(f"Bullish entry skipped on {self._pm_instrument_id}: {reason}")
                return
            super()._submit_yes_order(self._trade_amount)
            mid_str = f" mid={self._pm_mid:.4f}" if self._pm_mid else ""
            self.log.info(f"BUY ${self._trade_amount} on {self._pm_instrument_id}{mid_str}")

    def _compute_signal(self) -> int:
        return compute_signal(list(self._btc_closes))

    def _start_btc_warmup(self, now: datetime | None = None) -> None:
        if self._warmup_days == 0 or self._warmup_request_inflight or self._warmup_complete:
            return

        end = (now or datetime.now(tz=timezone.utc)).replace(second=0, microsecond=0)
        start = end - timedelta(days=self._warmup_days)
        self._warmup_request_inflight = True

        self.log.info(
            f"Requesting BTC warmup: {self._btc_bar_type} "
            f"{start.isoformat()} -> {end.isoformat()}"
        )
        self.request_bars(
            self._btc_bar_type,
            start=start,
            end=end,
            callback=self._on_warmup_complete,
        )

    def _on_warmup_complete(self, request_id) -> None:
        merged = dict(self._warmup_history)
        merged.update(self._warmup_live_buffer)

        historical_count = len(self._warmup_history)
        buffered_count = len(self._warmup_live_buffer)

        self._warmup_request_inflight = False
        self._warmup_complete = True
        self._warmup_history.clear()
        self._warmup_live_buffer.clear()

        latest_ts_ns: int | None = None
        latest_close: float | None = None
        for ts_ns, close in sorted(merged.items()):
            if self._record_btc_close(ts_ns, close):
                latest_ts_ns = ts_ns
                latest_close = close

        if latest_ts_ns is None or latest_close is None:
            self.log.warning(
                f"BTC warmup complete with no bars (request_id={request_id}); "
                "trading will wait for live bars"
            )
            return

        signal_str = self._signal_status_for_ts(latest_ts_ns)
        self.log.info(
            f"BTC warmup complete: hist={historical_count} buffered={buffered_count} "
            f"merged={len(merged)} latest_close={latest_close:.2f} | {signal_str}"
        )

    def _process_live_bar(self, ts_ns: int, close: float, *, close_str: str) -> None:
        if not self._record_btc_close(ts_ns, close):
            return

        signal_str = self._signal_status_for_ts(ts_ns)
        self.log.info(f"BTC close={close_str} | {signal_str}")

    def _record_btc_close(self, ts_ns: int, close: float) -> bool:
        if self._last_btc_bar_ts_ns is not None and ts_ns <= self._last_btc_bar_ts_ns:
            return False

        self._last_btc_bar_ts_ns = ts_ns
        self._btc_closes.append(close)
        return True

    def _signal_status_for_ts(self, signal_ts_ns: int) -> str:
        if len(self._btc_closes) <= self._signal_lookback:
            return f"WAITING ({len(self._btc_closes)}/{self._signal_lookback + 1} bars)"

        signal = self._compute_signal()
        self._check_entry_exit(signal_ts_ns)
        return {1: "BULLISH ↑", -1: "BEARISH ↓", 0: "NEUTRAL →"}.get(signal, "?")

    def _warmup_status_str(self) -> str:
        if self._warmup_days == 0:
            return "disabled"
        if self._warmup_complete:
            return f"complete ({self._warmup_days}d)"
        if self._warmup_request_inflight:
            return (
                f"loading ({self._warmup_days}d, "
                f"hist={len(self._warmup_history)}, buffered={len(self._warmup_live_buffer)})"
            )
        return f"pending ({self._warmup_days}d)"


def compute_signal(closes: list[float]) -> int:
    """BTC momentum signal over a window of closes."""
    if len(closes) < 2:
        return 0
    if closes[-1] > closes[0]:
        return 1
    if closes[-1] < closes[0]:
        return -1
    return 0
