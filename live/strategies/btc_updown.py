"""BTC up/down infrastructure test strategy for the live process.

Signal: BTC momentum over N 1-minute bars.
Execution: Polymarket YES token via quote ticks.
Window transitions: driven by time alerts, not data timestamps.
"""
from collections import deque

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


class BtcUpDownStrategy(WindowedPolymarketStrategy):
    _STATUS_INTERVAL_NS = 30_000_000_000  # 30 seconds

    def __init__(self, config: BtcUpDownConfig):
        super().__init__(config)
        self._btc_bar_type = BarType.from_str(config.btc_bar_type)
        self._trade_amount = config.trade_amount_usdc
        self._signal_lookback = config.signal_lookback
        self._btc_closes: deque[float] = deque(maxlen=config.signal_lookback + 1)

    def on_start(self):
        self.subscribe_bars(self._btc_bar_type)
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
            f"{len(self._windows)} window(s) pre-loaded"
        )

    def on_stop(self):
        self.clock.cancel_timer("status")
        self._stop_window_lifecycle()
        self.log.info(f"Stopped | total fills: {self._trade_count}")

    def on_bar(self, bar: Bar):
        self._btc_closes.append(float(bar.close))
        if len(self._btc_closes) > self._signal_lookback:
            signal = self._compute_signal()
            signal_str = {1: "BULLISH ↑", -1: "BEARISH ↓", 0: "NEUTRAL →"}.get(signal, "?")
            self._check_entry_exit(bar.ts_event)
        else:
            signal_str = f"WAITING ({len(self._btc_closes)}/{self._signal_lookback + 1} bars)"
        self.log.info(f"BTC close={bar.close} | {signal_str}")

    def _on_status_timer(self, event) -> None:
        entered_str = "YES" if self._entered_this_window else "no"
        self.log.info(
            f"[heartbeat] PM={self._quote_state_str(self.clock.timestamp_ns())} | "
            f"window_end={_fmt_ns(self._window_end_ns)} UTC | "
            f"entered={entered_str} | fills={self._trade_count}"
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


def compute_signal(closes: list[float]) -> int:
    """BTC momentum signal over a window of closes."""
    if len(closes) < 2:
        return 0
    if closes[-1] > closes[0]:
        return 1
    if closes[-1] < closes[0]:
        return -1
    return 0
