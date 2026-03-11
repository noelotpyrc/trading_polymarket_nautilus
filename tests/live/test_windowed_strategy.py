"""Unit tests for the shared windowed live strategy lifecycle."""
from types import SimpleNamespace

import pytest

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from live.strategies.windowed import WindowedPolymarketStrategy


class HarnessConfig(StrategyConfig, frozen=True):
    pm_instrument_ids: tuple[str, ...]
    window_end_times_ns: tuple[int, ...]
    outcome_side: str = "yes"


class DummyOrder:
    def __init__(self, client_order_id):
        self.client_order_id = client_order_id


class LifecycleHarness(WindowedPolymarketStrategy):
    def __init__(self, config: HarnessConfig):
        super().__init__(config)
        self.canceled_orders = []
        self.canceled_timer_names = []
        self.closed_instruments = []
        self.guard_alerts = []
        self.now_ns = 0
        self.open_positions = set()
        self.subscription_events = []
        self.alerts = []
        self.stop_called = False

    def cancel_order(self, order, *args, **kwargs):
        self.canceled_orders.append(order.client_order_id)

    def _close_positions_for_instrument(self, instrument_id, reason: str) -> None:
        self.closed_instruments.append((instrument_id, reason))

    def subscribe_quote_ticks(self, instrument_id):
        self.subscription_events.append(("sub", instrument_id))

    def unsubscribe_quote_ticks(self, instrument_id):
        self.subscription_events.append(("unsub", instrument_id))

    def _set_next_window_alert(self) -> None:
        self.alerts.append((self._window_alert_name(), self._window_end_ns))

    def stop(self):
        self.stop_called = True

    def _now_ns(self) -> int:
        return self.now_ns

    def _has_open_position_on_instrument(self, instrument_id):
        return instrument_id in self.open_positions

    def _set_guard_time_alert(self, name: str, alert_time_ns: int, callback) -> None:
        self.guard_alerts.append((name, alert_time_ns, callback))

    def _cancel_guard_timer(self, name: str) -> None:
        self.canceled_timer_names.append(name)

    def _on_window_end(self, event) -> None:
        raise NotImplementedError


def _strategy() -> LifecycleHarness:
    return LifecycleHarness(
        HarnessConfig(
            pm_instrument_ids=("a.POLYMARKET", "b.POLYMARKET"),
            window_end_times_ns=(1_000, 2_000),
        )
    )


class TestWindowedPolymarketStrategy:
    def test_invalid_outcome_side_rejected(self):
        with pytest.raises(ValueError, match="outcome_side must be one of"):
            LifecycleHarness(
                HarnessConfig(
                    pm_instrument_ids=("a.POLYMARKET",),
                    window_end_times_ns=(1_000,),
                    outcome_side="down",
                )
            )

    def test_entry_guard_requires_quote(self):
        strategy = _strategy()

        assert strategy._entry_guard_reason(1_000_000_000) == "PM quote unavailable"

    def test_entry_guard_rejects_stale_quote(self):
        strategy = _strategy()
        strategy._pm_mid = 0.51
        strategy._pm_mid_ts_ns = 0

        reason = strategy._entry_guard_reason(121_000_000_000)

        assert reason == "PM quote stale (121s old)"

    def test_reject_terminal_event_clears_pending_and_unblocks_reentry(self):
        strategy = _strategy()
        strategy._pm_mid = 0.51
        strategy._pm_mid_ts_ns = 0
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id

        strategy.on_order_rejected(SimpleNamespace(client_order_id="O-1", reason="nope"))

        assert strategy._entry_order_pending is False
        assert strategy._entry_order_client_id is None
        assert strategy._entry_guard_reason(30_000_000_000) is None

    @pytest.mark.parametrize("handler_name,event", [
        ("on_order_denied", SimpleNamespace(client_order_id="O-1", reason="denied")),
        ("on_order_canceled", SimpleNamespace(client_order_id="O-1")),
        ("on_order_expired", SimpleNamespace(client_order_id="O-1")),
    ])
    def test_terminal_events_clear_pending_entry(self, handler_name, event):
        strategy = _strategy()
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id
        strategy._entry_order_timer_names["O-1"] = ("cancel-O-1", "escalate-O-1")
        strategy._entry_order_timeout_order_ids["cancel-O-1"] = "O-1"
        strategy._entry_order_timeout_order_ids["escalate-O-1"] = "O-1"

        getattr(strategy, handler_name)(event)

        assert strategy._entry_order_pending is False
        assert strategy._entry_order_client_id is None
        assert strategy.canceled_timer_names == ["cancel-O-1", "escalate-O-1"]

    def test_entry_order_cancel_timeout_requests_cancel_and_keeps_pending(self):
        strategy = _strategy()
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id

        strategy._handle_entry_order_cancel_timeout_for("O-1")

        assert strategy.canceled_orders == ["O-1"]
        assert strategy._entry_order_pending is True
        assert "O-1" in strategy._entry_orders_flatten_on_fill

    def test_entry_order_escalation_timeout_stops_when_order_is_unresolved(self):
        strategy = _strategy()
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id

        strategy._handle_entry_order_escalation_timeout_for("O-1")

        assert strategy.stop_called is True

    def test_rollover_cancels_pending_and_subscribes_new_before_old(self):
        strategy = _strategy()
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id

        first_instrument = strategy._pm_instrument_id
        second_instrument = InstrumentId.from_str("b.POLYMARKET")

        strategy._roll_to_next_window("stop")

        assert strategy.canceled_orders == ["O-1"]
        assert strategy._entry_order_pending is True
        assert strategy._window_idx == 1
        assert strategy._pm_instrument_id == second_instrument
        assert strategy.subscription_events == [
            ("sub", second_instrument),
            ("unsub", first_instrument),
        ]

    def test_stop_lifecycle_cancels_pending_and_closes_all_windows(self):
        strategy = _strategy()
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id

        strategy._stop_window_lifecycle()

        assert strategy.canceled_orders == ["O-1"]
        assert strategy.closed_instruments == [
            (InstrumentId.from_str("a.POLYMARKET"), "strategy stop"),
            (InstrumentId.from_str("b.POLYMARKET"), "strategy stop"),
        ]

    def test_late_fill_from_prior_window_flattens_old_instrument(self):
        strategy = _strategy()
        old_instrument = InstrumentId.from_str("a.POLYMARKET")
        current_instrument = InstrumentId.from_str("b.POLYMARKET")
        strategy._pm_instrument_id = current_instrument
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = old_instrument

        strategy.on_order_filled(
            SimpleNamespace(
                client_order_id="O-1",
                instrument_id=old_instrument,
                order_side=OrderSide.BUY,
                last_px="0.51",
                last_qty="5",
            )
        )

        assert strategy.closed_instruments == [(old_instrument, "late fill")]
        assert strategy._entry_order_pending is False
        assert strategy._entered_this_window is False
        assert strategy._trade_count == 1
        assert strategy.guard_alerts[0][0] == "LATE-FILL-CHECK:a.POLYMARKET"

    def test_late_fill_cleanup_timeout_stops_when_position_remains_open(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.open_positions.add(instrument)
        strategy._schedule_late_fill_cleanup_timeout(instrument)

        strategy._handle_late_fill_cleanup_timeout_for(instrument)

        assert strategy.stop_called is True

    def test_position_closed_cancels_late_fill_cleanup_timer_when_flat(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.open_positions.add(instrument)
        strategy._schedule_late_fill_cleanup_timeout(instrument)
        strategy.open_positions.clear()

        strategy.on_position_closed(SimpleNamespace(instrument_id=instrument))

        assert strategy.canceled_timer_names == ["LATE-FILL-CHECK:a.POLYMARKET"]
        assert instrument not in strategy._late_fill_cleanup_pending
