"""Unit tests for the shared windowed live strategy lifecycle."""
from types import SimpleNamespace

import pytest

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId

from live.order_truth import OrderTruthRecord, OrderTruthStatus
from live.sandbox_order import SandboxOrderStore, SandboxOrderTruthProvider
from live.resolution import MarketResolution
from live.sandbox_wallet import SandboxWalletStore
from live.wallet_truth import WalletSettlement, WalletTokenPosition, WalletTruthSnapshot
from live.strategies.windowed import WindowedPolymarketStrategy


class HarnessConfig(StrategyConfig, frozen=True):
    pm_instrument_ids: tuple[str, ...]
    window_end_times_ns: tuple[int, ...]
    outcome_side: str = "yes"


class DummyOrder:
    def __init__(
        self,
        client_order_id,
        *,
        is_closed: bool = False,
        time_in_force: TimeInForce = TimeInForce.IOC,
        status: OrderStatus = OrderStatus.SUBMITTED,
        venue_order_id: str | None = None,
        instrument_id: InstrumentId | None = None,
        filled_qty: float = 0.0,
        leaves_qty: float = 0.0,
    ):
        self.client_order_id = client_order_id
        self.is_closed = is_closed
        self.time_in_force = time_in_force
        self.status = status
        self.venue_order_id = venue_order_id
        self.instrument_id = instrument_id
        self.filled_qty = filled_qty
        self.leaves_qty = leaves_qty


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
        self.wallet_balance_syncs = []
        self.wallet_reconciliations = []
        self.orders_for_reconciliation = []
        self.purged_order_ids = []
        self.order_cache = {}

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

    def _ioc_orders_requiring_reconciliation(self):
        return list(self.orders_for_reconciliation)

    def _purge_order_from_cache(self, order) -> None:
        if not self._is_order_closed(order):
            return False
        self.purged_order_ids.append(order.client_order_id)
        self.order_cache.pop(order.client_order_id, None)
        return True

    def _on_window_end(self, event) -> None:
        raise NotImplementedError

    def _sync_account_balance_from_wallet_truth(self, snapshot: WalletTruthSnapshot) -> None:
        self.wallet_balance_syncs.append(snapshot.collateral_balance)

    def _reconcile_position_from_wallet_settlement(self, position, settlement) -> bool:
        positions = self.positions_by_instrument.get(position.instrument_id, [])
        self.positions_by_instrument[position.instrument_id] = [
            candidate for candidate in positions if candidate is not position
        ]
        self.wallet_reconciliations.append(
            {
                "instrument_id": position.instrument_id,
                "settlement_token_id": None if settlement is None else settlement.token_id,
            }
        )
        return True

    @property
    def cache(self):
        return SimpleNamespace(
            order=lambda client_order_id: self.order_cache.get(client_order_id),
        )


class FakeWalletTruthProvider:
    def __init__(self, *snapshots: WalletTruthSnapshot):
        self._snapshots = list(snapshots)

    def snapshot(self) -> WalletTruthSnapshot:
        if len(self._snapshots) == 1:
            return self._snapshots[0]
        return self._snapshots.pop(0)


class FakeOrderTruthProvider:
    def __init__(self, statuses: dict[tuple[str | None, str | None], OrderTruthRecord]):
        self._statuses = statuses

    def order_status(self, *, client_order_id: str | None, venue_order_id: str | None) -> OrderTruthRecord:
        return self._statuses[(client_order_id, venue_order_id)]


def _strategy() -> LifecycleHarness:
    return LifecycleHarness(
        HarnessConfig(
            pm_instrument_ids=("a.POLYMARKET", "b.POLYMARKET"),
            window_end_times_ns=(1_000, 2_000),
        )
    )


def _wallet_strategy() -> LifecycleHarness:
    return LifecycleHarness(
        HarnessConfig(
            pm_instrument_ids=("conda-tokena.POLYMARKET", "condb-tokenb.POLYMARKET"),
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

    def test_entry_guard_rejects_when_free_collateral_below_trade_amount_plus_fee_buffer(self):
        strategy = _strategy()
        strategy._trade_amount = 5.0
        strategy._free_collateral_balance = lambda: 5.0
        strategy._pm_mid_ts_ns = 0
        strategy._pm_bid = 0.51
        strategy._pm_bid_size = 100.0
        strategy._pm_ask = 0.52
        strategy._pm_ask_size = 100.0

        reason = strategy._entry_guard_reason(30_000_000_000)

        assert reason == "Free collateral 5.000000 below required entry cash 5.500000"

    def test_entry_guard_rejects_when_process_stop_requested(self):
        strategy = _strategy()
        strategy._process_stop_requested = True
        strategy._pm_mid_ts_ns = 0
        strategy._pm_bid = 0.51
        strategy._pm_bid_size = 100.0
        strategy._pm_ask = 0.52
        strategy._pm_ask_size = 100.0

        reason = strategy._entry_guard_reason(30_000_000_000)

        assert reason == "process stop requested"

    def test_entry_guard_allows_when_free_collateral_covers_trade_amount_plus_fee_buffer(self):
        strategy = _strategy()
        strategy._trade_amount = 5.0
        strategy._free_collateral_balance = lambda: 5.5
        strategy._pm_mid_ts_ns = 0
        strategy._pm_bid = 0.51
        strategy._pm_bid_size = 100.0
        strategy._pm_ask = 0.52
        strategy._pm_ask_size = 100.0

        reason = strategy._entry_guard_reason(30_000_000_000)

        assert reason is None

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

    def test_submit_entry_ack_timeout_clears_local_pending_state(self):
        strategy = _strategy()

        strategy._submit_entry_order(5.0)
        strategy._handle_entry_submit_ack_timeout_for("O-IOC")

        assert strategy._entry_order is None
        assert strategy._entry_order_pending is False
        assert strategy._entry_order_client_id is None
        assert "O-IOC" not in strategy._entry_order_instruments
        assert "O-IOC" not in strategy._entry_orders_by_id

    def test_submit_entry_ack_timeout_callback_uses_timer_event_name(self):
        strategy = _strategy()

        strategy._submit_entry_order(5.0)
        timer_name = strategy._entry_submit_ack_timer_names["O-IOC"]

        strategy.trigger_guard(timer_name)

        assert strategy._entry_order is None
        assert strategy._entry_order_pending is False
        assert strategy._entry_order_client_id is None
        assert "O-IOC" not in strategy._entry_order_instruments
        assert "O-IOC" not in strategy._entry_orders_by_id

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

    def test_fill_updates_attached_sandbox_wallet_store(self):
        strategy = _strategy()
        store = SandboxWalletStore(wallet_address="sandbox", collateral_balance=20.0)
        strategy.set_sandbox_wallet_store(store)

        strategy.on_order_filled(
            SimpleNamespace(
                client_order_id="O-1",
                instrument_id=InstrumentId.from_str("cond1-yes1.POLYMARKET"),
                order_side=OrderSide.BUY,
                last_px="0.60",
                last_qty="5",
            )
        )
        strategy.on_order_filled(
            SimpleNamespace(
                client_order_id="O-2",
                instrument_id=InstrumentId.from_str("cond1-yes1.POLYMARKET"),
                order_side=OrderSide.SELL,
                last_px="0.80",
                last_qty="2",
            )
        )

        assert store.collateral_balance == pytest.approx(18.6)
        assert store.positions()["yes1"] == pytest.approx(3.0)

    def test_fill_updates_attached_sandbox_order_store(self):
        strategy = _strategy()
        store = SandboxOrderStore()
        strategy.set_sandbox_order_store(store)
        order = DummyOrder(
            "O-1",
            status=OrderStatus.PARTIALLY_FILLED,
            venue_order_id="V-1",
            instrument_id=InstrumentId.from_str("cond1-yes1.POLYMARKET"),
            filled_qty=5.0,
            leaves_qty=3.0,
        )
        strategy.order_cache["O-1"] = order

        strategy.on_order_filled(
            SimpleNamespace(
                client_order_id="O-1",
                instrument_id=InstrumentId.from_str("cond1-yes1.POLYMARKET"),
                order_side=OrderSide.BUY,
                last_px="0.60",
                last_qty="5",
            )
        )

        provider = SandboxOrderTruthProvider(order_store=store)
        truth = provider.order_status(client_order_id="O-1", venue_order_id="V-1")

        assert truth.status is OrderTruthStatus.NOT_FOUND
        assert truth.remaining_qty == pytest.approx(3.0)

    def test_order_truth_cancels_open_stale_ioc_remainder(self):
        strategy = _strategy()
        order = DummyOrder(
            "O-1",
            status=OrderStatus.PARTIALLY_FILLED,
            venue_order_id="V-1",
            instrument_id=strategy._pm_instrument_id,
            filled_qty=2.0,
            leaves_qty=3.0,
        )
        strategy.orders_for_reconciliation = [order]
        strategy.set_order_truth_provider(
            FakeOrderTruthProvider({
                ("O-1", "V-1"): OrderTruthRecord(
                    client_order_id="O-1",
                    venue_order_id="V-1",
                    status=OrderTruthStatus.OPEN,
                )
            })
        )

        strategy._refresh_order_truth(log_initial=True)

        assert strategy.canceled_orders == ["O-1"]
        assert strategy.purged_order_ids == []

    def test_order_truth_purges_terminal_stale_ioc_remainder_and_clears_tracking(self):
        strategy = _strategy()
        order = DummyOrder(
            "O-1",
            status=OrderStatus.PARTIALLY_FILLED,
            venue_order_id="V-1",
            instrument_id=strategy._pm_instrument_id,
            filled_qty=2.0,
            leaves_qty=3.0,
        )
        strategy.orders_for_reconciliation = [order]
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id
        strategy._entry_orders_by_id["O-1"] = order
        strategy.set_order_truth_provider(
            FakeOrderTruthProvider({
                ("O-1", "V-1"): OrderTruthRecord(
                    client_order_id="O-1",
                    venue_order_id="V-1",
                    status=OrderTruthStatus.NOT_FOUND,
                )
            })
        )

        strategy._refresh_order_truth(log_initial=True)

        assert strategy.purged_order_ids == ["O-1"]
        assert order.status == OrderStatus.CANCELED
        assert order.is_closed is True
        assert "O-1" not in strategy._entry_order_instruments
        assert "O-1" not in strategy._entry_orders_by_id

    def test_refresh_wallet_truth_stores_snapshot(self):
        strategy = _strategy()

        class FakeWalletTruthProvider:
            def snapshot(self):
                return WalletTruthSnapshot(
                    wallet_address="0xabc",
                    collateral_balance=12.5,
                    positions=(
                        WalletTokenPosition(
                            condition_id="cond1",
                            token_id="yes1",
                            instrument_id="cond1-yes1.POLYMARKET",
                            outcome_side="yes",
                            outcome_label="Up",
                            size=2.0,
                            redeemable=False,
                            mergeable=False,
                            window_slug="slug-1",
                            window_end_ns=1_000,
                        ),
                    ),
                    settlements=(),
                )

        strategy.set_wallet_truth_provider(FakeWalletTruthProvider())

        strategy._refresh_wallet_truth(log_initial=True)

        assert strategy.wallet_truth_snapshot.collateral_balance == 12.5
        assert strategy.wallet_truth_snapshot.positions[0].token_id == "yes1"
        assert strategy.wallet_balance_syncs == [12.5]

    def test_wallet_truth_absent_carried_position_reconciles_local_state(self):
        strategy = _wallet_strategy()
        instrument = InstrumentId.from_str("conda-tokena.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 2.0)]
        strategy._resolution_pending_instruments[instrument] = "window end"

        class FakeWalletTruthProvider:
            def snapshot(self):
                return WalletTruthSnapshot(
                    wallet_address="0xabc",
                    collateral_balance=8.0,
                    positions=(),
                    settlements=(),
                )

        strategy.set_wallet_truth_provider(FakeWalletTruthProvider())

        strategy._refresh_wallet_truth(log_initial=True)

        assert strategy.positions_by_instrument[instrument] == []
        assert strategy._resolution_pending_instruments == {}
        assert instrument not in strategy._resolution_settled_instruments
        assert strategy.wallet_reconciliations == [
            {
                "instrument_id": instrument,
                "settlement_token_id": None,
            }
        ]

    def test_wallet_truth_settlement_reconciles_settled_residual(self):
        strategy = _wallet_strategy()
        instrument = InstrumentId.from_str("conda-tokena.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 2.0)]
        strategy._resolution_settled_instruments.add(instrument)

        class FakeWalletTruthProvider:
            def snapshot(self):
                return WalletTruthSnapshot(
                    wallet_address="0xabc",
                    collateral_balance=11.0,
                    positions=(),
                    settlements=(
                        WalletSettlement(
                            token_id="tokena",
                            position_size=2.0,
                            settlement_price=1.0,
                            collateral_credit=2.0,
                        ),
                    ),
                )

        strategy.set_wallet_truth_provider(FakeWalletTruthProvider())

        strategy._refresh_wallet_truth(log_initial=True)
        strategy._refresh_wallet_truth()

        assert strategy.positions_by_instrument[instrument] == []
        assert strategy._resolution_settled_instruments == set()
        assert strategy.wallet_reconciliations == [
            {
                "instrument_id": instrument,
                "settlement_token_id": "tokena",
            }
        ]

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

    def test_dispatch_process_stop_persists_explicit_reason(self):
        strategy = _strategy()

        strategy._dispatch_process_stop("manual reconciliation")

        assert strategy.stop_called is True
        assert strategy._process_stop_reason == "manual reconciliation"

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

    def test_late_buy_fill_on_current_window_consumes_entry_budget(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy._pm_instrument_id = instrument
        strategy._entry_order_instruments["O-1"] = instrument
        strategy._entry_orders_flatten_on_fill.add("O-1")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 5.0)]

        strategy.on_order_filled(SimpleNamespace(
            client_order_id="O-1",
            instrument_id=instrument,
            order_side=OrderSide.BUY,
            last_px="0.99",
            last_qty="5.0",
        ))

        assert strategy._entered_this_window is True
        assert len(strategy.exit_submissions) == 1
        assert strategy.exit_submissions[0]["instrument_id"] == instrument
        assert strategy.exit_submissions[0]["quantity"] == 5.0

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

        assert strategy._resolution_pending_instruments == {instrument: "window end"}
        assert instrument not in strategy._resolution_settled_instruments
        assert "RESOLUTION-CHECK:a.POLYMARKET" not in strategy.guard_alerts

    def test_process_stop_keeps_live_position_open_until_it_becomes_carried(self):
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
        assert strategy.exit_submissions == []
        assert strategy._process_stop_requested is True
        assert strategy._resolution_pending_instruments == {}

        strategy._carry_positions_to_resolution(instrument, "window end")

        assert callbacks == ["stop"]
        assert strategy._resolution_pending_instruments == {instrument: "window end"}
        assert "RESOLUTION-CHECK:a.POLYMARKET" in strategy.guard_alerts
        assert instrument not in strategy._resolution_settled_instruments
        assert strategy.stop_called is False

    def test_wallet_truth_still_reconciles_carried_residual_after_process_stop_dispatch(self):
        strategy = _wallet_strategy()
        instrument = InstrumentId.from_str("conda-tokena.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 2.0)]
        strategy.min_quantity_by_instrument[instrument] = 5.0
        strategy._resolution_pending_instruments[instrument] = "test stop"
        callbacks = []
        strategy.set_process_stop_callback(lambda: callbacks.append("stop"))

        class FakeWalletTruthProvider:
            def snapshot(self):
                return WalletTruthSnapshot(
                    wallet_address="0xabc",
                    collateral_balance=8.0,
                    positions=(),
                    settlements=(
                        WalletSettlement(
                            token_id="tokena",
                            position_size=2.0,
                            settlement_price=1.0,
                            collateral_credit=2.0,
                        ),
                    ),
                )

        strategy.set_wallet_truth_provider(FakeWalletTruthProvider())

        strategy.request_process_stop("test stop")
        assert callbacks == ["stop"]

        strategy._refresh_wallet_truth()

        assert callbacks == ["stop"]
        assert strategy.stop_called is False
        assert strategy._resolution_pending_instruments == {}
        assert strategy.positions_by_instrument[instrument] == []

    def test_stop_window_lifecycle_preserves_carried_resolution_positions(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 5.0)]
        strategy._carry_positions_to_resolution(instrument, "window end")

        strategy._stop_window_lifecycle()

        assert strategy._resolution_pending_instruments == {instrument: "window end"}
        assert "RESOLUTION-CHECK:a.POLYMARKET" not in strategy.guard_alerts
        assert strategy.exit_submissions == []
        assert strategy.positions_by_instrument[instrument][0].quantity == 5.0

    def test_process_stop_still_waits_for_non_resolution_open_position(self):
        strategy = _strategy()
        instrument = strategy._pm_instrument_id
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 1.0)]
        callbacks = []
        strategy.set_process_stop_callback(lambda: callbacks.append("stop"))
        strategy._process_stop_requested = True
        strategy._process_stop_reason = "test stop"

        strategy._maybe_finalize_process_stop()

        assert callbacks == []
        assert strategy._process_stop_dispatched is False

    def test_process_stop_waits_for_stale_ioc_order_truth_reconciliation(self):
        strategy = _strategy()
        order = DummyOrder(
            "O-1",
            status=OrderStatus.PARTIALLY_FILLED,
            venue_order_id="V-1",
            instrument_id=strategy._pm_instrument_id,
            filled_qty=2.0,
            leaves_qty=3.0,
        )
        strategy.orders_for_reconciliation = [order]
        strategy._entry_order_instruments["O-1"] = strategy._pm_instrument_id
        strategy._entry_orders_by_id["O-1"] = order
        callbacks = []
        strategy.set_process_stop_callback(lambda: callbacks.append("stop"))
        strategy.set_order_truth_provider(
            FakeOrderTruthProvider({
                ("O-1", "V-1"): OrderTruthRecord(
                    client_order_id="O-1",
                    venue_order_id="V-1",
                    status=OrderTruthStatus.NOT_FOUND,
                )
            })
        )

        strategy.request_process_stop("test stop")

        assert callbacks == []

        strategy._refresh_order_truth()
        strategy.orders_for_reconciliation = []
        strategy._maybe_finalize_process_stop()

        assert strategy.purged_order_ids == ["O-1"]
        assert order.status == OrderStatus.CANCELED
        assert callbacks == ["stop"]
        assert strategy.stop_called is False

    def test_operating_balance_guard_does_not_stop_when_low_balance_but_position_open(self):
        strategy = _strategy()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy._trade_amount = 5.0
        strategy._free_collateral_balance = lambda: 5.0
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 1.0)]

        strategy._check_operating_balance()

        assert strategy.stop_called is False

    def test_operating_balance_guard_stops_when_low_balance_and_idle(self):
        strategy = _strategy()
        strategy._trade_amount = 5.0
        strategy._free_collateral_balance = lambda: 5.0

        strategy._check_operating_balance()

        assert strategy.stop_called is True

    def test_operating_balance_guard_does_not_stop_when_free_cash_is_sufficient(self):
        strategy = _strategy()
        strategy._trade_amount = 5.0
        strategy._free_collateral_balance = lambda: 5.5

        strategy._check_operating_balance()

        assert strategy.stop_called is False
