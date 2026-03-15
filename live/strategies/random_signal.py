"""Random infrastructure test strategy for the live process.

Subscribes to BTC 1-minute bars and Polymarket quote ticks. On every bar,
rolls `random()`; if above threshold, enters immediately. This is intended to
exercise feed wiring, order lifecycle, and window roll behavior quickly.

Usage:
    python live/runs/random_signal.py --slug-pattern btc-updown-15m --sandbox
"""
import random

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide

from live.strategies.windowed import WindowedPolymarketStrategy, _fmt_ns


class RandomSignalConfig(StrategyConfig, frozen=True):
    pm_instrument_ids: tuple[str, ...]
    window_end_times_ns: tuple[int, ...]

    btc_bar_type: str = "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"
    entry_threshold: float = 0.5
    exit_threshold: float = 0.7
    trade_amount_usdc: float = 5.0
    disable_signal_exit: bool = False
    carry_window_end_position: bool = False
    outcome_side: str = "yes"


class RandomSignalStrategy(WindowedPolymarketStrategy):
    def __init__(self, config: RandomSignalConfig):
        super().__init__(config)
        self._btc_bar_type = BarType.from_str(config.btc_bar_type)
        self._trade_amount = config.trade_amount_usdc
        self._entry_threshold = config.entry_threshold
        self._exit_threshold = config.exit_threshold
        self._disable_signal_exit = config.disable_signal_exit
        self._carry_window_end_position = config.carry_window_end_position

    def on_start(self):
        self.subscribe_bars(self._btc_bar_type)
        self._start_window_lifecycle()
        self._start_balance_guard()
        self._start_wallet_truth_polling()
        self._start_order_truth_polling()
        self.log.info(
            f"Started | PM={self._pm_instrument_id} | "
            f"window_end={_fmt_ns(self._window_end_ns)} UTC | "
            f"outcome={self._selected_outcome_label()} | "
            f"entry_threshold={self._entry_threshold} | exit_threshold={self._exit_threshold} | "
            f"disable_signal_exit={self._disable_signal_exit} | "
            f"carry_window_end_position={self._carry_window_end_position}"
        )

    def on_stop(self):
        self._stop_order_truth_polling()
        self._stop_wallet_truth_polling()
        self._stop_balance_guard()
        self._stop_window_lifecycle()
        self.log.info(f"Stopped | total fills: {self._trade_count}")

    def on_bar(self, bar: Bar):
        value = random.random()
        quote_str = self._quote_state_str(bar.ts_event)
        positions = self._open_positions_for_instrument(self._pm_instrument_id)
        stale_reason = self._signal_bar_stale_reason(bar.ts_event)

        if stale_reason is not None:
            self.log.warning(
                f"BTC={bar.close} rand={value:.3f} | {quote_str} | "
                f"signal blocked ({stale_reason})"
            )
            return

        if positions and value > self._exit_threshold:
            if self._disable_signal_exit:
                self.log.info(
                    f"BTC={bar.close} rand={value:.3f} > {self._exit_threshold} "
                    f"→ EXIT skipped (disabled for sandbox resolution test) | {quote_str}"
                )
                return
            reason = self._exit_guard_reason(bar.ts_event)
            if reason is not None:
                self.log.info(
                    f"BTC={bar.close} rand={value:.3f} → EXIT skipped ({reason}) | {quote_str}"
                )
                return
            self.log.info(
                f"BTC={bar.close} rand={value:.3f} > {self._exit_threshold} → EXIT  {quote_str}"
            )
            self._close_positions_for_instrument(
                self._pm_instrument_id,
                reason="signal exit",
            )
            return

        if (
            not positions
            and not self._entered_this_window
            and not self._entry_order_pending
            and value > self._entry_threshold
        ):
            reason = self._entry_guard_reason(bar.ts_event)
            if reason is not None:
                self.log.info(
                    f"BTC={bar.close} rand={value:.3f} → ENTER skipped ({reason}) | {quote_str}"
                )
                return
            self.log.info(
                f"BTC={bar.close} rand={value:.3f} > {self._entry_threshold} → ENTER  {quote_str}"
            )
            super()._submit_entry_order(self._trade_amount)
            self.log.info(
                f"BUY {self._selected_outcome_label()} ${self._trade_amount} "
                f"on {self._pm_instrument_id}{self._quote_execution_str(OrderSide.BUY)}"
            )
        else:
            self.log.info(
                f"BTC={bar.close} rand={value:.3f} | {quote_str} | "
                f"entered={self._entered_this_window} | positions={len(positions)}"
            )

    def _on_window_end(self, event) -> None:
        if self._carry_window_end_position:
            self.log.info(
                f"Window ending ({_fmt_ns(self._window_end_ns)} UTC) — rolling over with forced residual carry"
            )
            self._cancel_pending_entry_order("window rollover")
            if self._open_positions_for_instrument(self._pm_instrument_id):
                self._carry_positions_to_resolution(
                    self._pm_instrument_id,
                    "window end (forced sandbox residual)",
                )
            self._advance_to_next_window(
                exhausted_message=(
                    "No more pre-loaded windows — stopping. "
                    "Restart the node for the next session."
                ),
            )
            return
        self._roll_to_next_window(
            exhausted_message=(
                "No more pre-loaded windows — stopping. "
                "Restart the node for the next session."
            ),
        )
