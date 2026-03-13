"""Unit tests for the shared windowed live strategy lifecycle."""
from types import SimpleNamespace

import pytest

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId

from live.resolution import MarketResolution
from live.strategies.windowed import WindowedPolymarketStrategy


class HarnessConfig(StrategyConfig, frozen=True):
    pm_instrument_ids: tuple[str, ...]
    window_end_times_ns: tuple[int, ...]
    outcome_side: str = "yes"


class DummyOrder:
    def __init__(self, client_order_id, *, is_closed: bool = False):
        self.client_order_id = client_order_id
        self.is_closed = is_closed


class DummyPosition:
    def __init__(self, instrument_id: InstrumentId, quantity: float):
        self.instrument_id = instrument_id
        self.quantity = quantity


class TimerEvent:
    def __init__(self, name: str):
        self.name = name


class LifecycleHarness(WindowedPolymarketStrategy):
    def __init__(self, config: HarnessConfig):
        super().__init__(config)
        self.canceled_orders = []
        self.canceled_timer_names = []
        self.guard_alerts = {}
        self.guard_alert_history = []
        self.now_ns = 0
        self.positions_by_instrument = {}
        self.min_quantity_by_instrument = {}
        self.exit_submissions = []
        self.subscription_events = []
        self.alerts = []
        self.market_resolution_responses = {}
        self.stop_called = False
        self.submitted_orders = []
        self.entry_order_kwargs = None

    def cancel_order(self, order, *args, **kwargs):
        self.canceled_orders.append(order.client_order_id)

    def submit_order(self, order, *args, **kwargs):
        self.submitted_orders.append(order)

    def _build_entry_order(self, quantity):
        self.entry_order_kwargs = {
            "instrument_id": self._pm_instrument_id,
            "order_side": OrderSide.BUY,
            "quantity": quantity,
            "time_in_force": TimeInForce.IOC,
            "quote_quantity": True,
        }
        return DummyOrder("O-IOC")

    def close_position(self, position, *args, **kwargs):
        self.exit_submissions.append(
            {
                "instrument_id": position.instrument_id,
                "quantity": position.quantity,
                "kwargs": kwargs,
            }
        )

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

    def _open_positions_for_instrument(self, instrument_id):
        return list(self.positions_by_instrument.get(instrument_id, []))

    def _instrument_min_quantity(self, instrument_id):
        return self.min_quantity_by_instrument.get(instrument_id)

    def _set_guard_time_alert(self, name: str, alert_time_ns: int, callback) -> None:
        self.guard_alerts[name] = (alert_time_ns, callback)
        self.guard_alert_history.append((name, alert_time_ns, callback))

    def _cancel_guard_timer(self, name: str) -> None:
        self.canceled_timer_names.append(name)
        self.guard_alerts.pop(name, None)

    def trigger_guard(self, name: str) -> None:
        _, callback = self.guard_alerts[name]
        callback(TimerEvent(name))

    def _fetch_market_resolution(self, instrument_id):
        response = self.market_resolution_responses[instrument_id]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):
            current = response.pop(0)
            if not response:
                self.market_resolution_responses[instrument_id] = current
            return current
        return response

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
        strategy._pm_mid_ts_ns = 0
        strategy._pm_ask_size = 100.0

        reason = strategy._entry_guard_reason(121_000_000_000)

        assert reason == "PM quote stale (121s old)"

    def test_entry_guard_rejects_quote_without_ask_liquidity(self):
        strategy = _strategy()
        strategy._pm_mid_ts_ns = 0
        strategy._pm_bid = 0.51
        strategy._pm_bid_size = 100.0
        strategy._pm_ask = 0.999
        strategy._pm_ask_size = 0.0

        reason = strategy._entry_guard_reason(30_000_000_000)

        assert reason == "PM ask unavailable"

    def test_exit_guard_rejects_quote_without_bid_liquidity(self):
        strategy = _strategy()
        strategy._pm_mid_ts_ns = 0
        strategy._pm_bid = 0.001
        strategy._pm_bid_size = 0.0
        strategy._pm_ask = 0.52
        strategy._pm_ask_size = 100.0

        reason = strategy._exit_guard_reason(30_000_000_000)

        assert reason == "PM bid unavailable"

    def test_submit_entry_order_uses_ioc_and_tracks_order(self):
        strategy = _strategy()

        strategy._submit_entry_order(5.0)

        assert strategy.entry_order_kwargs["instrument_id"] == strategy._pm_instrument_id
        assert strategy.entry_order_kwargs["order_side"] == OrderSide.BUY
        assert strategy.entry_order_kwargs["quote_quantity"] is True
        assert strategy.entry_order_kwargs["time_in_force"] == TimeInForce.IOC
        assert strategy._entry_orders_by_id["O-IOC"].client_order_id == "O-IOC"
        assert strategy.submitted_orders[0].client_order_id == "O-IOC"

    def test_reject_terminal_event_clears_pending_and_unblocks_reentry(self):
        strategy = _strategy()
        strategy._pm_mid_ts_ns = 0
        strategy._pm_ask_size = 100.0
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id

        strategy.on_order_rejected(SimpleNamespace(client_order_id="O-1", reason="nope"))

        assert strategy._entry_order_pending is False
        assert strategy._entry_order_client_id is None
        assert strategy._entry_guard_reason(30_000_000_000) is None

    @pytest.mark.parametrize(
        "handler_name,event",
        [
            ("on_order_denied", SimpleNamespace(client_order_id="O-1", reason="denied")),
            ("on_order_canceled", SimpleNamespace(client_order_id="O-1")),
            ("on_order_expired", SimpleNamespace(client_order_id="O-1")),
        ],
    )
    def test_terminal_events_clear_pending_entry(self, handler_name, event):
        strategy = _strategy()
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id
        strategy._entry_orders_by_id["O-1"] = strategy._entry_order
        strategy._entry_order_timer_names["O-1"] = ("cancel-O-1", "escalate-O-1")
        strategy._entry_order_timeout_order_ids["cancel-O-1"] = "O-1"
        strategy._entry_order_timeout_order_ids["escalate-O-1"] = "O-1"

        getattr(strategy, handler_name)(event)

        assert strategy._entry_order_pending is False
        assert strategy._entry_order_client_id is None
        assert "O-1" not in strategy._entry_orders_by_id
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

    def test_stop_lifecycle_cancels_pending_and_submits_live_compatible_exits(self):
        strategy = _strategy()
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id
        instrument_a = InstrumentId.from_str("a.POLYMARKET")
        instrument_b = InstrumentId.from_str("b.POLYMARKET")
        strategy.positions_by_instrument[instrument_a] = [DummyPosition(instrument_a, 5.0)]
        strategy.positions_by_instrument[instrument_b] = [DummyPosition(instrument_b, 6.0)]

        strategy._stop_window_lifecycle()

        assert strategy.canceled_orders == ["O-1"]
        assert strategy.exit_submissions == [
            {
                "instrument_id": instrument_a,
                "quantity": 5.0,
                "kwargs": {
                    "time_in_force": TimeInForce.IOC,
                    "reduce_only": False,
                    "quote_quantity": False,
                },
            },
            {
                "instrument_id": instrument_b,
                "quantity": 6.0,
                "kwargs": {
                    "time_in_force": TimeInForce.IOC,
                    "reduce_only": False,
                    "quote_quantity": False,
                },
            },
        ]

    def test_exhausted_windows_request_process_stop(self):
        strategy = LifecycleHarness(
            HarnessConfig(
                pm_instrument_ids=("a.POLYMARKET",),
                window_end_times_ns=(1_000,),
            )
        )
        callbacks = []
        strategy.set_process_stop_callback(lambda: callbacks.append("stop"))

        strategy._roll_to_next_window("exhausted")

        assert callbacks == ["stop"]
        assert strategy.stop_called is False

    def test_late_fill_from_prior_window_flattens_old_instrument(self):
        strategy = _strategy()
        old_instrument = InstrumentId.from_str("a.POLYMARKET")
        current_instrument = InstrumentId.from_str("b.POLYMARKET")
        strategy._pm_instrument_id = current_instrument
        strategy._entry_order = DummyOrder("O-1")
        strategy._entry_order_client_id = "O-1"
        strategy._entry_order_pending = True
        strategy._entry_order_instruments["O-1"] = old_instrument
        strategy.positions_by_instrument[old_instrument] = [DummyPosition(old_instrument, 5.0)]

        strategy.on_order_filled(
            SimpleNamespace(
                client_order_id="O-1",
                instrument_id=old_instrument,
                order_side=OrderSide.BUY,
                last_px="0.51",
                last_qty="5",
            )
        )

        assert strategy.exit_submissions == [
            {
                "instrument_id": old_instrument,
                "quantity": 5.0,
                "kwargs": {
                    "time_in_force": TimeInForce.IOC,
                    "reduce_only": False,
                    "quote_quantity": False,
                },
            }
        ]
        assert strategy._entry_order_pending is False
        assert strategy._entered_this_window is False
        assert strategy._trade_count == 1
        assert strategy.guard_alert_history[0][0] == "POSITION-CLEANUP:a.POLYMARKET"

    def test_position_cleanup_timeout_stops_when_position_remains_open(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 5.0)]

        strategy._close_positions_for_instrument(instrument, reason="late fill")
        strategy.now_ns = strategy._POSITION_CLEANUP_TIMEOUT_NS + 1
        strategy.trigger_guard("POSITION-CLEANUP:a.POLYMARKET")

        assert strategy.stop_called is True

    def test_position_cleanup_timeout_carries_ended_window_residual_to_resolution(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 5.0)]
        strategy.now_ns = 1_500

        strategy._close_positions_for_instrument(
            instrument,
            reason="window end",
            allow_resolution_carry=True,
        )
        strategy.now_ns = strategy._POSITION_CLEANUP_TIMEOUT_NS + 1_500
        strategy.trigger_guard("POSITION-CLEANUP:a.POLYMARKET")

        assert strategy.stop_called is False
        assert strategy._resolution_pending_instruments == {instrument: "window end"}
        assert "RESOLUTION-CHECK:a.POLYMARKET" in strategy.guard_alerts
        assert "POSITION-CLEANUP:a.POLYMARKET" not in strategy.guard_alerts

    def test_position_closed_cancels_cleanup_timer_when_flat(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 5.0)]
        strategy._close_positions_for_instrument(instrument, reason="late fill")
        strategy.positions_by_instrument[instrument] = []

        strategy.on_position_closed(SimpleNamespace(instrument_id=instrument))

        assert strategy.canceled_timer_names == ["POSITION-CLEANUP:a.POLYMARKET"]
        assert instrument not in strategy._position_cleanup_states

    def test_position_closed_cancels_residual_entry_order_for_instrument(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy._entry_order_instruments["O-1"] = instrument
        strategy._entry_orders_by_id["O-1"] = DummyOrder("O-1")

        strategy.on_position_closed(SimpleNamespace(instrument_id=instrument))

        assert strategy.canceled_orders == ["O-1"]
        assert "O-1" in strategy._entry_orders_flatten_on_fill

    def test_below_min_cleanup_residual_carries_without_submitting_impossible_exit(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 0.4)]
        strategy.min_quantity_by_instrument[instrument] = 1.0

        strategy._close_positions_for_instrument(instrument, reason="signal exit")

        assert strategy.exit_submissions == []
        assert strategy.stop_called is False
        assert strategy._resolution_pending_instruments == {instrument: "signal exit"}
        assert "RESOLUTION-CHECK:a.POLYMARKET" in strategy.guard_alerts

    def test_below_min_ended_window_residual_carries_to_resolution(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 0.4)]
        strategy.min_quantity_by_instrument[instrument] = 1.0
        strategy.now_ns = 1_500

        strategy._close_positions_for_instrument(
            instrument,
            reason="window end",
            allow_resolution_carry=True,
        )

        assert strategy.exit_submissions == []
        assert strategy.stop_called is False
        assert strategy._resolution_pending_instruments == {instrument: "window end"}
        assert "RESOLUTION-CHECK:a.POLYMARKET" in strategy.guard_alerts

    def test_resolution_check_reschedules_until_market_resolves(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 0.4)]
        strategy.market_resolution_responses[instrument] = [
            MarketResolution(
                condition_id="cond-a",
                token_id="token-a",
                market_closed=False,
                target_token_outcome="Yes",
                winning_token_id=None,
                winning_outcome=None,
            ),
            MarketResolution(
                condition_id="cond-a",
                token_id="token-a",
                market_closed=True,
                target_token_outcome="Yes",
                winning_token_id="token-a",
                winning_outcome="Yes",
            ),
        ]

        strategy._carry_positions_to_resolution(instrument, "window end")
        strategy.trigger_guard("RESOLUTION-CHECK:a.POLYMARKET")

        assert strategy._resolution_pending_instruments == {instrument: "window end"}
        assert "RESOLUTION-CHECK:a.POLYMARKET" in strategy.guard_alerts

        strategy.trigger_guard("RESOLUTION-CHECK:a.POLYMARKET")

        assert instrument not in strategy._resolution_pending_instruments
        assert instrument in strategy._resolution_settled_instruments
        assert "RESOLUTION-CHECK:a.POLYMARKET" not in strategy.guard_alerts

    def test_process_stop_waits_for_resolution_carried_residual_then_dispatches(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 0.4)]
        strategy.min_quantity_by_instrument[instrument] = 1.0
        strategy.now_ns = 1_500
        callbacks = []
        strategy.set_process_stop_callback(lambda: callbacks.append("stop"))
        strategy.market_resolution_responses[instrument] = MarketResolution(
            condition_id="cond-a",
            token_id="token-a",
            market_closed=True,
            target_token_outcome="Yes",
            winning_token_id="token-a",
            winning_outcome="Yes",
        )

        strategy.request_process_stop("test stop")

        assert callbacks == []
        assert strategy._resolution_pending_instruments == {instrument: "test stop"}

        strategy.trigger_guard("RESOLUTION-CHECK:a.POLYMARKET")

        assert callbacks == ["stop"]
        assert strategy.stop_called is False
