"""Shared live lifecycle for windowed Polymarket test strategies."""
from datetime import datetime, timezone

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


class WindowedPolymarketStrategy(Strategy):
    _QUOTE_STALE_AFTER_NS = 120_000_000_000

    def __init__(self, config):
        super().__init__(config)
        assert len(config.pm_instrument_ids) == len(config.window_end_times_ns), (
            "pm_instrument_ids and window_end_times_ns must have equal length"
        )
        assert len(config.pm_instrument_ids) >= 1, "At least one window required"

        self._windows: list[tuple[str, int]] = list(
            zip(config.pm_instrument_ids, config.window_end_times_ns)
        )
        self._window_idx = 0
        self._pm_instrument_id = InstrumentId.from_str(self._windows[0][0])
        self._window_end_ns = self._windows[0][1]

        self._pm_mid: float | None = None
        self._pm_mid_ts_ns: int | None = None
        self._entered_this_window = False

        self._entry_order = None
        self._entry_order_pending = False
        self._entry_order_client_id = None
        self._entry_order_instruments: dict[object, InstrumentId] = {}
        self._entry_orders_flatten_on_fill: set[object] = set()

        self._trade_count = 0

    def _window_alert_name(self) -> str:
        return f"window_end_{self._window_idx}"

    def _start_window_lifecycle(self) -> None:
        self.subscribe_quote_ticks(self._pm_instrument_id)
        self._set_next_window_alert()

    def _stop_window_lifecycle(self) -> None:
        self._cancel_pending_entry_order("strategy stop")
        self._close_positions_for_all_windows()

    def _set_next_window_alert(self) -> None:
        self.clock.set_time_alert_ns(
            name=self._window_alert_name(),
            alert_time_ns=self._window_end_ns,
            callback=self._on_window_end,
        )

    def on_quote_tick(self, tick) -> None:
        if tick.instrument_id == self._pm_instrument_id:
            self._pm_mid = (float(tick.bid_price) + float(tick.ask_price)) / 2
            self._pm_mid_ts_ns = tick.ts_event

    def _entry_guard_reason(self, signal_ts_ns: int) -> str | None:
        if self._entry_order_pending:
            return "entry order still pending"
        if self._pm_mid is None or self._pm_mid_ts_ns is None:
            return "PM quote unavailable"

        quote_age_ns = max(0, signal_ts_ns - self._pm_mid_ts_ns)
        if quote_age_ns > self._QUOTE_STALE_AFTER_NS:
            return f"PM quote stale ({quote_age_ns // 1_000_000_000}s old)"

        return None

    def _quote_state_str(self, now_ns: int) -> str:
        if self._pm_mid is None or self._pm_mid_ts_ns is None:
            return "n/a"
        quote_age_ns = max(0, now_ns - self._pm_mid_ts_ns)
        return f"{self._pm_mid:.4f} age={quote_age_ns // 1_000_000_000}s"

    def _submit_yes_order(self, trade_amount: float) -> None:
        qty_str = f"{trade_amount:.6f}".rstrip("0").rstrip(".")
        order = self.order_factory.market(
            instrument_id=self._pm_instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str(qty_str),
            quote_quantity=True,
        )
        self._entry_order = order
        self._entry_order_pending = True
        self._entry_order_client_id = order.client_order_id
        self._entry_order_instruments[order.client_order_id] = self._pm_instrument_id
        self.submit_order(order)

    def _cancel_pending_entry_order(self, reason: str) -> None:
        if not self._entry_order_pending or self._entry_order is None or self._entry_order_client_id is None:
            return

        self._entry_orders_flatten_on_fill.add(self._entry_order_client_id)
        self.cancel_order(self._entry_order)
        self.log.warning(f"Canceling pending entry order ({reason}): {self._entry_order_client_id}")

    def _close_positions_for_instrument(self, instrument_id: InstrumentId, reason: str) -> None:
        for pos in self.cache.positions_open(instrument_id=instrument_id):
            self.close_position(pos)
            self.log.info(f"Closed position on {instrument_id} ({reason})")

    def _close_positions_for_all_windows(self) -> None:
        seen = set()
        for instrument_id_str, _ in self._windows:
            instrument_id = InstrumentId.from_str(instrument_id_str)
            if instrument_id in seen:
                continue
            seen.add(instrument_id)
            self._close_positions_for_instrument(instrument_id, reason="strategy stop")

    def _roll_to_next_window(self, exhausted_message: str) -> None:
        self.log.info(f"Window ending ({_fmt_ns(self._window_end_ns)} UTC) — rolling over")

        self._cancel_pending_entry_order("window rollover")
        self._close_positions_for_instrument(self._pm_instrument_id, reason="window end")

        old_instrument_id = self._pm_instrument_id

        self._window_idx += 1
        if self._window_idx >= len(self._windows):
            self.log.warning(exhausted_message)
            self.stop()
            return

        self._pm_instrument_id = InstrumentId.from_str(self._windows[self._window_idx][0])
        self._window_end_ns = self._windows[self._window_idx][1]
        self._entered_this_window = False
        self._pm_mid = None
        self._pm_mid_ts_ns = None

        self.subscribe_quote_ticks(self._pm_instrument_id)
        self.unsubscribe_quote_ticks(old_instrument_id)
        self._set_next_window_alert()

        remaining = len(self._windows) - self._window_idx - 1
        self.log.info(
            f"Now trading {self._pm_instrument_id} | "
            f"ends {_fmt_ns(self._window_end_ns)} UTC | "
            f"{remaining} window(s) remaining"
        )

    def _clear_active_entry_order(self, client_order_id) -> None:
        if client_order_id != self._entry_order_client_id:
            return
        self._entry_order = None
        self._entry_order_pending = False
        self._entry_order_client_id = None

    def _mark_entry_order_inactive(self, client_order_id, *, flatten_on_fill: bool = True) -> None:
        if flatten_on_fill:
            self._entry_orders_flatten_on_fill.add(client_order_id)
        self._clear_active_entry_order(client_order_id)

    def _handle_entry_order_terminal(self, client_order_id, message: str) -> None:
        if client_order_id not in self._entry_order_instruments and client_order_id != self._entry_order_client_id:
            return
        self._mark_entry_order_inactive(client_order_id)
        self.log.warning(message)

    def on_order_denied(self, event) -> None:
        self._handle_entry_order_terminal(
            event.client_order_id,
            f"Entry order denied: {event.reason}",
        )

    def on_order_rejected(self, event) -> None:
        self._handle_entry_order_terminal(
            event.client_order_id,
            f"Entry order rejected: {event.reason}",
        )

    def on_order_canceled(self, event) -> None:
        self._handle_entry_order_terminal(
            event.client_order_id,
            f"Entry order canceled: {event.client_order_id}",
        )

    def on_order_expired(self, event) -> None:
        self._handle_entry_order_terminal(
            event.client_order_id,
            f"Entry order expired: {event.client_order_id}",
        )

    def on_order_filled(self, event) -> None:
        tracked_instrument_id = self._entry_order_instruments.get(event.client_order_id)
        is_late_fill = (
            tracked_instrument_id is not None
            and (
                event.client_order_id in self._entry_orders_flatten_on_fill
                or tracked_instrument_id != self._pm_instrument_id
            )
        )

        if is_late_fill and event.order_side == OrderSide.BUY:
            self.log.error(
                f"Late fill detected on {tracked_instrument_id} "
                f"(current={self._pm_instrument_id}) — flattening immediately"
            )
            self._close_positions_for_instrument(tracked_instrument_id, reason="late fill")
            self._clear_active_entry_order(event.client_order_id)
            self._entry_orders_flatten_on_fill.discard(event.client_order_id)
        elif (
            event.client_order_id == self._entry_order_client_id
            and event.order_side == OrderSide.BUY
        ):
            self._entered_this_window = True
            self._clear_active_entry_order(event.client_order_id)

        self._trade_count += 1
        self.log.info(f"Fill #{self._trade_count}: price={event.last_px} qty={event.last_qty}")


def _fmt_ns(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%H:%M:%S")
