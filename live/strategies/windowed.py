"""Shared live lifecycle for windowed Polymarket test strategies."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from nautilus_trader.adapters.polymarket.common.symbol import (
    get_polymarket_condition_id,
    get_polymarket_token_id,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide, OrderStatus, PositionAdjustmentType, TimeInForce
from nautilus_trader.model.events.order import OrderCanceled, OrderExpired
from nautilus_trader.model.events.position import PositionAdjusted
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import AccountBalance, Money, Quantity
from nautilus_trader.trading.strategy import Strategy

from live.order_truth import OrderTruthProvider, OrderTruthStatus
from live.resolution import fetch_market_resolution
from live.sandbox_order import SandboxOrderStore
from live.sandbox_wallet import SandboxWalletStore
from live.wallet_truth import WalletSettlement, WalletTruthProvider, WalletTruthSnapshot


@dataclass(frozen=True)
class _InstrumentCleanupState:
    reason: str
    deadline_ns: int
    allow_resolution_carry: bool


class WindowedPolymarketStrategy(Strategy):
    _QUOTE_STALE_AFTER_NS = 120_000_000_000
    _SIGNAL_BAR_STALE_AFTER_NS = 150_000_000_000
    _ENTRY_ORDER_CANCEL_AFTER_NS = 90_000_000_000
    _ENTRY_ORDER_ESCALATE_AFTER_NS = 180_000_000_000
    _POSITION_CLEANUP_RETRY_INTERVAL_NS = 5_000_000_000
    _POSITION_CLEANUP_TIMEOUT_NS = 60_000_000_000
    _RESOLUTION_POLL_INTERVAL_NS = 30_000_000_000
    _WALLET_TRUTH_POLL_INTERVAL_NS = 30_000_000_000
    _ORDER_TRUTH_POLL_INTERVAL_NS = 30_000_000_000
    _ORDER_TRUTH_CANCEL_RETRY_INTERVAL_NS = 30_000_000_000
    _BALANCE_GUARD_INTERVAL_NS = 30_000_000_000
    _ENTRY_FEE_BUFFER_RATE = 0.10

    def __init__(self, config):
        super().__init__(config)
        assert len(config.pm_instrument_ids) == len(config.window_end_times_ns), (
            "pm_instrument_ids and window_end_times_ns must have equal length"
        )
        assert len(config.pm_instrument_ids) >= 1, "At least one window required"

        self._windows: list[tuple[str, int]] = list(
            zip(config.pm_instrument_ids, config.window_end_times_ns)
        )
        self._outcome_side = getattr(config, "outcome_side", "yes")
        if self._outcome_side not in {"yes", "no"}:
            raise ValueError("outcome_side must be one of: yes, no")
        self._window_idx = 0
        self._pm_instrument_id = InstrumentId.from_str(self._windows[0][0])
        self._window_end_ns = self._windows[0][1]

        self._pm_mid: float | None = None
        self._pm_mid_ts_ns: int | None = None
        self._pm_bid: float | None = None
        self._pm_ask: float | None = None
        self._pm_bid_size: float = 0.0
        self._pm_ask_size: float = 0.0
        self._entered_this_window = False

        self._entry_order = None
        self._entry_order_pending = False
        self._entry_order_client_id = None
        self._entry_order_instruments: dict[object, InstrumentId] = {}
        self._entry_orders_by_id: dict[object, object] = {}
        self._entry_orders_flatten_on_fill: set[object] = set()
        self._entry_order_timer_names: dict[object, tuple[str, str]] = {}
        self._entry_order_timeout_order_ids: dict[str, object] = {}
        self._position_cleanup_timer_names: dict[InstrumentId, str] = {}
        self._position_cleanup_timer_instruments: dict[str, InstrumentId] = {}
        self._position_cleanup_states: dict[InstrumentId, _InstrumentCleanupState] = {}
        self._resolution_timer_names: dict[InstrumentId, str] = {}
        self._resolution_timer_instruments: dict[str, InstrumentId] = {}
        self._resolution_pending_instruments: dict[InstrumentId, str] = {}
        self._resolution_settled_instruments: set[InstrumentId] = set()
        self._sandbox_wallet_store: SandboxWalletStore | None = None
        self._sandbox_order_store: SandboxOrderStore | None = None
        self._wallet_truth_provider: WalletTruthProvider | None = None
        self._wallet_truth_snapshot: WalletTruthSnapshot | None = None
        self._wallet_truth_seen_settlement_token_ids: set[str] = set()
        self._order_truth_provider: OrderTruthProvider | None = None
        self._order_truth_cancel_attempt_ns: dict[object, int] = {}
        self._process_stop_callback: Callable[[], None] | None = None
        self._process_stop_requested = False
        self._process_stop_dispatched = False
        self._process_stop_reason: str | None = None

        self._trade_count = 0

    def set_process_stop_callback(self, callback: Callable[[], None]) -> None:
        self._process_stop_callback = callback

    def set_sandbox_wallet_store(self, wallet_store: SandboxWalletStore) -> None:
        self._sandbox_wallet_store = wallet_store

    def set_sandbox_order_store(self, order_store: SandboxOrderStore) -> None:
        self._sandbox_order_store = order_store

    def set_wallet_truth_provider(self, provider: WalletTruthProvider) -> None:
        self._wallet_truth_provider = provider

    def set_order_truth_provider(self, provider: OrderTruthProvider) -> None:
        self._order_truth_provider = provider

    @property
    def wallet_truth_snapshot(self) -> WalletTruthSnapshot | None:
        return self._wallet_truth_snapshot

    def request_process_stop(self, reason: str) -> None:
        if self._process_stop_requested:
            return

        self._process_stop_requested = True
        self._process_stop_reason = reason
        self.log.warning(reason)
        self._cancel_pending_entry_order(reason)
        self._close_positions_for_all_windows(reason=reason, monitor_cleanup=True)
        self._maybe_finalize_process_stop()

    def _window_alert_name(self) -> str:
        return f"window_end_{self._window_idx}"

    def _start_window_lifecycle(self) -> None:
        self.subscribe_quote_ticks(self._pm_instrument_id)
        self._set_next_window_alert()

    def _stop_window_lifecycle(self) -> None:
        self._cancel_pending_entry_order("strategy stop")
        self._cancel_all_position_cleanup()
        self._cancel_all_resolution_tracking()
        self._close_positions_for_all_windows(reason="strategy stop", monitor_cleanup=False)

    def _start_balance_guard(self) -> None:
        self._check_operating_balance()
        self.clock.set_timer_ns(
            "balance_guard",
            self._BALANCE_GUARD_INTERVAL_NS,
            0,
            0,
            self._on_balance_guard_timer,
        )

    def _stop_balance_guard(self) -> None:
        self._cancel_guard_timer("balance_guard")

    def _set_next_window_alert(self) -> None:
        self.clock.set_time_alert_ns(
            name=self._window_alert_name(),
            alert_time_ns=self._window_end_ns,
            callback=self._on_window_end,
        )

    def _start_wallet_truth_polling(self) -> None:
        if self._wallet_truth_provider is None:
            return

        self._refresh_wallet_truth(log_initial=True)
        self.clock.set_timer_ns(
            "wallet_truth",
            self._WALLET_TRUTH_POLL_INTERVAL_NS,
            0,
            0,
            self._on_wallet_truth_timer,
        )

    def _stop_wallet_truth_polling(self) -> None:
        if self._wallet_truth_provider is None:
            return
        self.clock.cancel_timer("wallet_truth")

    def _start_order_truth_polling(self) -> None:
        if self._order_truth_provider is None:
            return

        self._refresh_order_truth(log_initial=True)
        self.clock.set_timer_ns(
            "order_truth",
            self._ORDER_TRUTH_POLL_INTERVAL_NS,
            0,
            0,
            self._on_order_truth_timer,
        )

    def _stop_order_truth_polling(self) -> None:
        if self._order_truth_provider is None:
            return
        self.clock.cancel_timer("order_truth")

    def _on_wallet_truth_timer(self, event) -> None:
        self._refresh_wallet_truth()

    def _on_order_truth_timer(self, event) -> None:
        self._refresh_order_truth()

    def _on_balance_guard_timer(self, event) -> None:
        self._check_operating_balance()

    def _refresh_wallet_truth(self, *, log_initial: bool = False) -> None:
        if self._wallet_truth_provider is None:
            return

        snapshot = self._wallet_truth_provider.snapshot()
        previous = self._wallet_truth_snapshot
        self._wallet_truth_snapshot = snapshot
        self._sync_account_balance_from_wallet_truth(snapshot)
        self._reconcile_wallet_truth_settlements(snapshot)
        self._reconcile_absent_carried_wallet_positions(snapshot)

        if previous is None:
            if log_initial:
                self.log.info(
                    f"Wallet truth initialized | collateral={snapshot.collateral_balance:.6f} "
                    f"| positions={len(snapshot.positions)} "
                    f"| settlements={len(snapshot.settlements)}"
                )
            return

        if (
            previous.collateral_balance != snapshot.collateral_balance
            or previous.positions != snapshot.positions
            or previous.settlements != snapshot.settlements
        ):
            self.log.info(
                f"Wallet truth updated | collateral={snapshot.collateral_balance:.6f} "
                f"| positions={len(snapshot.positions)} "
                f"| settlements={len(snapshot.settlements)}"
            )

    def _refresh_order_truth(self, *, log_initial: bool = False) -> None:
        if self._order_truth_provider is None:
            return

        suspicious_orders = self._ioc_orders_requiring_reconciliation()
        if log_initial:
            self.log.info(
                f"Order truth initialized | suspicious_ioc_orders={len(suspicious_orders)}"
            )
        if not suspicious_orders:
            return

        for order in suspicious_orders:
            self._reconcile_suspicious_ioc_order(order)

    def _portfolio_account_for_pm_venue(self):
        portfolio = getattr(self, "portfolio", None)
        if portfolio is None:
            return None

        try:
            return portfolio.account(venue=self._pm_instrument_id.venue)
        except Exception:
            return None

    def _sync_account_balance_from_wallet_truth(self, snapshot: WalletTruthSnapshot) -> None:
        account = self._portfolio_account_for_pm_venue()
        if account is None:
            return

        try:
            current_balance = account.balance()
        except Exception:
            current_balance = None

        if current_balance is None:
            currency = getattr(account, "base_currency", None)
            if currency is None:
                return
        else:
            currency = current_balance.currency
            if (
                float(current_balance.total) == snapshot.collateral_balance
                and float(current_balance.locked) == 0.0
                and float(current_balance.free) == snapshot.collateral_balance
            ):
                return

        balance = Money(snapshot.collateral_balance, currency)
        account.update_balances(
            [
                AccountBalance(
                    total=balance,
                    locked=Money(0, currency),
                    free=balance,
                )
            ]
        )

    def _reconcile_wallet_truth_settlements(self, snapshot: WalletTruthSnapshot) -> None:
        for settlement in snapshot.settlements:
            token_id = str(settlement.token_id)
            if token_id in self._wallet_truth_seen_settlement_token_ids:
                continue
            if self._reconcile_wallet_settlement(settlement):
                self._wallet_truth_seen_settlement_token_ids.add(token_id)

    def _reconcile_absent_carried_wallet_positions(self, snapshot: WalletTruthSnapshot) -> None:
        carried_instruments = (
            set(self._resolution_pending_instruments) | self._resolution_settled_instruments
        )
        for instrument_id in list(carried_instruments):
            token_id = self._wallet_token_id_for_instrument(instrument_id)
            if token_id is None or snapshot.position_for_token(token_id) is not None:
                continue
            self._reconcile_carried_instrument_from_wallet_truth(instrument_id, settlement=None)

    def _reconcile_wallet_settlement(self, settlement: WalletSettlement) -> bool:
        instrument_id = self._instrument_for_wallet_token(settlement.token_id)
        if instrument_id is None:
            return True
        return self._reconcile_carried_instrument_from_wallet_truth(
            instrument_id,
            settlement=settlement,
        )

    def _instrument_for_wallet_token(self, token_id: str) -> InstrumentId | None:
        token_id = str(token_id)
        for instrument_id_str, _ in self._windows:
            instrument_id = InstrumentId.from_str(instrument_id_str)
            try:
                instrument_token_id = str(get_polymarket_token_id(instrument_id))
            except Exception:
                continue
            if instrument_token_id == token_id:
                return instrument_id
        return None

    def _wallet_token_id_for_instrument(self, instrument_id: InstrumentId) -> str | None:
        try:
            return str(get_polymarket_token_id(instrument_id))
        except Exception:
            return None

    def _reconcile_carried_instrument_from_wallet_truth(
        self,
        instrument_id: InstrumentId,
        *,
        settlement: WalletSettlement | None,
    ) -> bool:
        is_carried = (
            instrument_id in self._resolution_pending_instruments
            or instrument_id in self._resolution_settled_instruments
        )
        positions = list(self._open_positions_for_instrument(instrument_id))
        if not is_carried and not positions:
            return True

        if positions:
            reconciled_count = 0
            for position in positions:
                if self._reconcile_position_from_wallet_settlement(position, settlement):
                    reconciled_count += 1
            if reconciled_count != len(positions):
                self.log.error(
                    f"Wallet truth could not reconcile all carried positions on {instrument_id} "
                    f"({reconciled_count}/{len(positions)})"
                )
                return False
        else:
            reconciled_count = 0

        self._cancel_position_cleanup(instrument_id)
        self._cancel_resolution_tracking(instrument_id)
        self._resolution_settled_instruments.discard(instrument_id)

        if settlement is None:
            self.log.warning(
                f"Wallet truth no longer reports carried residual on {instrument_id} "
                f"— reconciling local state"
            )
        else:
            self.log.warning(
                f"Wallet settlement reconciled carried residual on {instrument_id} "
                f"(settlement={settlement.settlement_price:.2f}, "
                f"credit={settlement.collateral_credit:.6f}, "
                f"positions={reconciled_count})"
            )

        self._maybe_finalize_process_stop()
        return True

    def _reconcile_position_from_wallet_settlement(
        self,
        position,
        settlement: WalletSettlement | None,
    ) -> bool:
        signed_qty = float(getattr(position, "signed_qty", float(position.quantity)))
        if signed_qty == 0.0:
            return True

        adjustment = PositionAdjusted(
            trader_id=position.trader_id,
            strategy_id=position.strategy_id,
            instrument_id=position.instrument_id,
            position_id=position.id,
            account_id=position.account_id,
            adjustment_type=PositionAdjustmentType.FUNDING,
            quantity_change=-signed_qty,
            pnl_change=None,
            reason=(
                "wallet settlement reconciliation"
                if settlement is None
                else f"wallet settlement reconciliation ({settlement.token_id})"
            ),
            event_id=UUID4(),
            ts_event=self._now_ns(),
            ts_init=self._now_ns(),
        )
        position.apply_adjustment(adjustment)
        self.cache.update_position(position)
        self.cache.purge_position(position.id)
        return True

    def _minimum_operating_balance(self) -> float | None:
        trade_amount = getattr(self, "_trade_amount", None)
        if trade_amount is None:
            return None

        trade_amount = float(trade_amount)
        if trade_amount <= 0:
            return None
        return trade_amount + self._entry_fee_buffer(trade_amount)

    def _entry_fee_buffer(self, trade_amount: float) -> float:
        return max(0.0, trade_amount * self._ENTRY_FEE_BUFFER_RATE)

    def _free_collateral_balance(self) -> float | None:
        account = self._portfolio_account_for_pm_venue()
        if account is None:
            return None

        try:
            balance = account.balance_free()
        except Exception:
            return None

        if balance is None:
            return None

        return float(balance)

    def _operating_balance_shortfall_reason(self) -> str | None:
        minimum_balance = self._minimum_operating_balance()
        if minimum_balance is None:
            return None

        free_balance = self._free_collateral_balance()
        if free_balance is None:
            return None

        if free_balance + 1e-9 >= minimum_balance:
            return None

        return f"Free collateral {free_balance:.6f} below required entry cash {minimum_balance:.6f}"

    def _has_any_open_positions(self) -> bool:
        seen: set[InstrumentId] = set()
        for instrument_id_str, _ in self._windows:
            instrument_id = InstrumentId.from_str(instrument_id_str)
            if instrument_id in seen:
                continue
            seen.add(instrument_id)
            if self._has_open_position_on_instrument(instrument_id):
                return True
        return False

    def _is_idle_for_low_balance_stop(self) -> bool:
        if self._entry_order_pending:
            return False
        if self._position_cleanup_states:
            return False
        if self._resolution_pending_instruments:
            return False
        if self._has_any_open_positions():
            return False
        return True

    def _check_operating_balance(self) -> None:
        if self._process_stop_requested or self._process_stop_dispatched:
            return

        shortfall_reason = self._operating_balance_shortfall_reason()
        if shortfall_reason is None:
            return

        if not self._is_idle_for_low_balance_stop():
            return

        self.request_process_stop(
            f"{shortfall_reason} — stopping"
        )

    def _ioc_orders_requiring_reconciliation(self) -> list[object]:
        try:
            orders = self.cache.orders(venue=self._pm_instrument_id.venue)
        except Exception:
            return []
        return [
            order
            for order in orders
            if self._order_requires_truth_reconciliation(order)
        ]

    def _order_requires_truth_reconciliation(self, order) -> bool:
        if getattr(order, "time_in_force", None) != TimeInForce.IOC:
            return False
        status = getattr(order, "status", None)
        if status == OrderStatus.PARTIALLY_FILLED:
            return not self._is_order_closed(order)
        if status != OrderStatus.PENDING_CANCEL:
            return False
        filled_qty = getattr(order, "filled_qty", None)
        try:
            return filled_qty is not None and float(filled_qty) > 0 and not self._is_order_closed(order)
        except (TypeError, ValueError):
            return False

    def _reconcile_suspicious_ioc_order(self, order) -> None:
        client_order_id = getattr(order, "client_order_id", None)
        venue_order_id = getattr(order, "venue_order_id", None)
        client_order_ref = None if client_order_id is None else self._client_order_ref(client_order_id)
        venue_order_ref = None if venue_order_id is None else str(venue_order_id)

        truth = self._order_truth_provider.order_status(
            client_order_id=client_order_ref,
            venue_order_id=venue_order_ref,
        )
        if truth.status is OrderTruthStatus.OPEN:
            self._cancel_open_ioc_remainder(order)
            return
        if truth.status.is_terminal:
            self._purge_reconciled_ioc_order(order, truth.status)
            return

        self.log.warning(
            f"Order truth unavailable for stale IOC remainder {client_order_ref} "
            f"(venue={venue_order_ref}, reason={truth.reason or 'unknown'})"
        )

    def _cancel_open_ioc_remainder(self, order) -> None:
        client_order_id = getattr(order, "client_order_id", None)
        if client_order_id is None:
            return

        now_ns = self._now_ns()
        last_attempt_ns = self._order_truth_cancel_attempt_ns.get(client_order_id)
        if (
            last_attempt_ns is not None
            and now_ns - last_attempt_ns < self._ORDER_TRUTH_CANCEL_RETRY_INTERVAL_NS
        ):
            return

        self._order_truth_cancel_attempt_ns[client_order_id] = now_ns
        self._cancel_order_for_reconciliation(
            order,
            reason=(
                "order truth still reports partially filled IOC remainder as open"
            ),
        )

    def _purge_reconciled_ioc_order(self, order, truth_status: OrderTruthStatus) -> None:
        client_order_id = getattr(order, "client_order_id", None)
        if client_order_id is None:
            return

        client_order_ref = self._client_order_ref(client_order_id)
        self._order_truth_cancel_attempt_ns.pop(client_order_id, None)
        self._forget_entry_order_tracking(client_order_id)
        if not self._terminalize_ioc_order_from_truth(order, truth_status):
            self.log.error(
                f"Order truth proved IOC remainder {client_order_ref} is {truth_status.value}, "
                "but local Nautilus order state could not be terminalized"
            )
            return
        purged = self._purge_order_from_cache(order)
        outcome = "purged local cache record" if purged else "left closed local cache record"
        self.log.warning(
            f"Order truth reconciled stale IOC remainder {client_order_ref} "
            f"as {truth_status.value}; {outcome}"
        )
        self._maybe_finalize_process_stop()

    def _forget_entry_order_tracking(self, client_order_id) -> None:
        self._clear_active_entry_order(client_order_id)
        self._entry_order_instruments.pop(client_order_id, None)
        self._entry_orders_by_id.pop(client_order_id, None)
        self._entry_orders_flatten_on_fill.discard(client_order_id)
        self._cancel_entry_order_timeouts(client_order_id)

    def _terminalize_ioc_order_from_truth(self, order, truth_status: OrderTruthStatus) -> bool:
        if self._is_order_closed(order):
            return True

        if not hasattr(order, "apply"):
            status = (
                OrderStatus.EXPIRED
                if truth_status is OrderTruthStatus.EXPIRED
                else OrderStatus.CANCELED
            )
            try:
                order.status = status
                order.is_closed = True
            except Exception:
                return False
            return True

        client_order_id = getattr(order, "client_order_id", None)
        instrument_id = getattr(order, "instrument_id", None)
        trader_id = getattr(order, "trader_id", None)
        strategy_id = getattr(order, "strategy_id", None)
        if (
            client_order_id is None
            or instrument_id is None
            or trader_id is None
            or strategy_id is None
        ):
            return False

        now_ns = self._now_ns()
        event_cls = OrderExpired if truth_status is OrderTruthStatus.EXPIRED else OrderCanceled
        try:
            event = event_cls(
                trader_id=trader_id,
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=getattr(order, "venue_order_id", None),
                account_id=getattr(order, "account_id", None),
                event_id=UUID4(),
                ts_event=now_ns,
                ts_init=now_ns,
                reconciliation=True,
            )
            order.apply(event)
            self.cache.update_order(order)
            portfolio = getattr(self, "portfolio", None)
            if portfolio is not None and hasattr(portfolio, "update_order"):
                portfolio.update_order(event)
        except Exception:
            return False

        return self._is_order_closed(order)

    def _purge_order_from_cache(self, order) -> bool:
        try:
            self.cache.purge_order(order.client_order_id)
        except Exception:
            return False
        try:
            return self.cache.order(order.client_order_id) is None
        except Exception:
            return False

    def _cancel_order_for_reconciliation(self, order, *, reason: str) -> None:
        self.cancel_order(order)
        self._record_order_cancel_in_sandbox_truth(order)
        self.log.warning(
            f"Canceling stale IOC remainder ({reason}): "
            f"{self._client_order_ref(order.client_order_id)}"
        )

    def _sync_order_in_sandbox_truth(self, order) -> None:
        if self._sandbox_order_store is None or order is None:
            return

        client_order_id = getattr(order, "client_order_id", None)
        if client_order_id is None:
            return
        if getattr(order, "time_in_force", None) != TimeInForce.IOC:
            return

        venue_order_id = getattr(order, "venue_order_id", None)
        instrument_id = str(getattr(order, "instrument_id", "")) or None
        status = getattr(order, "status", None)
        remaining_qty = _order_remaining_qty(order)

        if status == OrderStatus.PARTIALLY_FILLED:
            # Simulate venue truth for IOC: once partially filled, the remainder is
            # no longer resting on the venue even if Nautilus keeps the order object open.
            truth_status = OrderTruthStatus.NOT_FOUND
        elif status == OrderStatus.FILLED:
            truth_status = OrderTruthStatus.CLOSED
        elif status == OrderStatus.CANCELED:
            truth_status = OrderTruthStatus.CANCELED
        elif status == OrderStatus.EXPIRED:
            truth_status = OrderTruthStatus.EXPIRED
        else:
            truth_status = OrderTruthStatus.OPEN

        self._sandbox_order_store.set_order_status(
            client_order_id=self._client_order_ref(client_order_id),
            venue_order_id=None if venue_order_id is None else str(venue_order_id),
            instrument_id=instrument_id,
            status=truth_status,
            remaining_qty=remaining_qty,
        )

    def _record_order_cancel_in_sandbox_truth(self, order) -> None:
        if self._sandbox_order_store is None or order is None:
            return

        client_order_id = getattr(order, "client_order_id", None)
        if client_order_id is None:
            return
        self._sandbox_order_store.set_order_status(
            client_order_id=self._client_order_ref(client_order_id),
            venue_order_id=None
            if getattr(order, "venue_order_id", None) is None
            else str(order.venue_order_id),
            instrument_id=str(getattr(order, "instrument_id", "")) or None,
            status=OrderTruthStatus.CANCELED,
            remaining_qty=0.0,
        )

    def on_quote_tick(self, tick) -> None:
        if tick.instrument_id == self._pm_instrument_id:
            self._pm_bid = float(tick.bid_price)
            self._pm_ask = float(tick.ask_price)
            self._pm_bid_size = float(tick.bid_size)
            self._pm_ask_size = float(tick.ask_size)
            if self._pm_bid_size > 0 and self._pm_ask_size > 0:
                self._pm_mid = (self._pm_bid + self._pm_ask) / 2
            else:
                self._pm_mid = None
            self._pm_mid_ts_ns = tick.ts_event

    def _entry_guard_reason(self, signal_ts_ns: int) -> str | None:
        balance_reason = self._operating_balance_shortfall_reason()
        if balance_reason is not None:
            return balance_reason
        if self._entry_order_pending:
            return "entry order still pending"
        return self._quote_guard_reason(signal_ts_ns, require_side="ask")

    def _exit_guard_reason(self, signal_ts_ns: int) -> str | None:
        return self._quote_guard_reason(signal_ts_ns, require_side="bid")

    def _quote_guard_reason(self, signal_ts_ns: int, *, require_side: str | None) -> str | None:
        if self._pm_mid_ts_ns is None:
            return "PM quote unavailable"
        quote_age_ns = max(0, signal_ts_ns - self._pm_mid_ts_ns)
        if quote_age_ns > self._QUOTE_STALE_AFTER_NS:
            return f"PM quote stale ({quote_age_ns // 1_000_000_000}s old)"
        if require_side == "ask" and self._pm_ask_size <= 0:
            return "PM ask unavailable"
        if require_side == "bid" and self._pm_bid_size <= 0:
            return "PM bid unavailable"

        return None

    def _now_ns(self) -> int:
        return self.clock.timestamp_ns()

    def _signal_bar_stale_reason(self, signal_ts_ns: int, *, now_ns: int | None = None) -> str | None:
        current_ns = self._now_ns() if now_ns is None else now_ns
        bar_age_ns = max(0, current_ns - signal_ts_ns)
        if bar_age_ns > self._SIGNAL_BAR_STALE_AFTER_NS:
            return f"BTC bar stale ({bar_age_ns // 1_000_000_000}s old)"
        return None

    def _quote_state_str(self, now_ns: int) -> str:
        if self._pm_mid_ts_ns is None:
            return "n/a"
        quote_age_ns = max(0, now_ns - self._pm_mid_ts_ns)
        bid_str = "n/a" if self._pm_bid is None else f"{self._pm_bid:.4f}x{self._pm_bid_size:.3f}"
        ask_str = "n/a" if self._pm_ask is None else f"{self._pm_ask:.4f}x{self._pm_ask_size:.3f}"
        mid_str = "n/a" if self._pm_mid is None else f"{self._pm_mid:.4f}"
        return (
            f"mid={mid_str} bid={bid_str} ask={ask_str} "
            f"age={quote_age_ns // 1_000_000_000}s"
        )

    def _quote_execution_str(self, side: OrderSide) -> str:
        if side == OrderSide.BUY:
            if self._pm_ask is None:
                return ""
            return f" ask={self._pm_ask:.4f} ask_sz={self._pm_ask_size:.3f}"
        if self._pm_bid is None:
            return ""
        return f" bid={self._pm_bid:.4f} bid_sz={self._pm_bid_size:.3f}"

    def _build_entry_order(self, quantity: Quantity):
        return self.order_factory.market(
            instrument_id=self._pm_instrument_id,
            order_side=OrderSide.BUY,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
            quote_quantity=True,
        )

    def _submit_entry_order(self, trade_amount: float) -> None:
        qty_str = f"{trade_amount:.6f}".rstrip("0").rstrip(".")
        order = self._build_entry_order(Quantity.from_str(qty_str))
        self._entry_order = order
        self._entry_order_pending = True
        self._entry_order_client_id = order.client_order_id
        self._entry_order_instruments[order.client_order_id] = self._pm_instrument_id
        self._entry_orders_by_id[order.client_order_id] = order
        self._sync_order_in_sandbox_truth(order)
        self._schedule_entry_order_timeouts(order.client_order_id)
        self.submit_order(order)

    def _selected_outcome_label(self) -> str:
        return self._outcome_side.upper()

    def _cancel_pending_entry_order(self, reason: str) -> None:
        if not self._entry_order_pending or self._entry_order is None or self._entry_order_client_id is None:
            return

        self._entry_orders_flatten_on_fill.add(self._entry_order_client_id)
        self._cancel_order_for_reconciliation(
            self._entry_order,
            reason=reason,
        )

    def _open_positions_for_instrument(self, instrument_id: InstrumentId):
        return list(self.cache.positions_open(instrument_id=instrument_id))

    def _close_positions_for_instrument(
        self,
        instrument_id: InstrumentId,
        reason: str,
        *,
        monitor_cleanup: bool = True,
        retry: bool = False,
        allow_resolution_carry: bool = False,
    ) -> None:
        self._cancel_tracked_entry_orders_for_instrument(
            instrument_id,
            reason=reason,
            skip_client_order_id=self._entry_order_client_id,
        )
        positions = self._open_positions_for_instrument(instrument_id)
        if not positions:
            if monitor_cleanup:
                self._cancel_position_cleanup(instrument_id)
                self._cancel_resolution_tracking(instrument_id)
                self._resolution_settled_instruments.discard(instrument_id)
                self._maybe_finalize_process_stop()
            return

        if (
            instrument_id in self._resolution_pending_instruments
            or instrument_id in self._resolution_settled_instruments
        ):
            if monitor_cleanup:
                self._maybe_finalize_process_stop()
            return

        if monitor_cleanup and instrument_id in self._position_cleanup_states and not retry:
            return

        if monitor_cleanup:
            existing = self._position_cleanup_states.get(instrument_id)
            deadline_ns = (
                existing.deadline_ns
                if existing is not None
                else self._now_ns() + self._POSITION_CLEANUP_TIMEOUT_NS
            )
            self._position_cleanup_states[instrument_id] = _InstrumentCleanupState(
                reason=reason,
                deadline_ns=deadline_ns,
                allow_resolution_carry=allow_resolution_carry,
            )

        for pos in positions:
            if not self._submit_exit_order_for_position(
                pos,
                reason=reason,
                allow_resolution_carry=allow_resolution_carry,
            ):
                return

        if monitor_cleanup:
            self._schedule_position_cleanup_retry(instrument_id)

    def _close_positions_for_all_windows(self, *, reason: str, monitor_cleanup: bool) -> None:
        seen = set()
        for instrument_id_str, _ in self._windows:
            instrument_id = InstrumentId.from_str(instrument_id_str)
            if instrument_id in seen:
                continue
            seen.add(instrument_id)
            self._close_positions_for_instrument(
                instrument_id,
                reason=reason,
                monitor_cleanup=monitor_cleanup,
                allow_resolution_carry=monitor_cleanup and self._window_has_ended(instrument_id),
            )

    def _has_open_position_on_instrument(self, instrument_id: InstrumentId) -> bool:
        return bool(self._open_positions_for_instrument(instrument_id))

    def _window_has_ended(self, instrument_id: InstrumentId) -> bool:
        if (
            instrument_id in self._resolution_pending_instruments
            or instrument_id in self._resolution_settled_instruments
        ):
            return True

        instrument_str = str(instrument_id)
        matching_indices = [
            index
            for index, (window_instrument_id, _) in enumerate(self._windows)
            if window_instrument_id == instrument_str
        ]
        if not matching_indices:
            return False

        if any(index < self._window_idx for index in matching_indices):
            return True

        end_ns = max(
            window_end_ns
            for window_instrument_id, window_end_ns in self._windows
            if window_instrument_id == instrument_str
        )
        if self._now_ns() >= end_ns:
            return True

        return self._window_idx >= len(self._windows) and instrument_id == self._pm_instrument_id

    def _guard_timer_name(self, prefix: str, suffix: str) -> str:
        return f"{prefix}:{suffix}"

    def _is_order_closed(self, order) -> bool:
        is_closed = getattr(order, "is_closed", None)
        if callable(is_closed):
            return bool(is_closed())
        if is_closed is None:
            return False
        return bool(is_closed)

    def _cancel_tracked_entry_orders_for_instrument(
        self,
        instrument_id: InstrumentId,
        *,
        reason: str,
        skip_client_order_id=None,
    ) -> None:
        for client_order_id, tracked_instrument_id in list(self._entry_order_instruments.items()):
            if tracked_instrument_id != instrument_id or client_order_id == skip_client_order_id:
                continue

            order = self._entry_orders_by_id.get(client_order_id)
            if order is None or self._is_order_closed(order):
                continue

            self._entry_orders_flatten_on_fill.add(client_order_id)
            self._cancel_order_for_reconciliation(order, reason=reason)

    def _client_order_ref(self, client_order_id) -> str:
        if hasattr(client_order_id, "to_str"):
            return client_order_id.to_str()
        return str(client_order_id)

    def _guard_event_name(self, event) -> str:
        if hasattr(event, "name"):
            return str(event.name)
        if hasattr(event, "to_str"):
            return event.to_str()
        return str(event)

    def _set_guard_time_alert(self, name: str, alert_time_ns: int, callback) -> None:
        self.clock.set_time_alert_ns(
            name=name,
            alert_time_ns=alert_time_ns,
            callback=callback,
        )

    def _cancel_guard_timer(self, name: str) -> None:
        try:
            self.clock.cancel_timer(name)
        except Exception:
            return

    def _schedule_entry_order_timeouts(self, client_order_id) -> None:
        order_ref = self._client_order_ref(client_order_id)
        cancel_name = self._guard_timer_name("ENTRY-CANCEL", order_ref)
        escalate_name = self._guard_timer_name("ENTRY-ESCALATE", order_ref)
        now_ns = self._now_ns()

        self._entry_order_timer_names[client_order_id] = (cancel_name, escalate_name)
        self._entry_order_timeout_order_ids[cancel_name] = client_order_id
        self._entry_order_timeout_order_ids[escalate_name] = client_order_id

        self._set_guard_time_alert(
            cancel_name,
            now_ns + self._ENTRY_ORDER_CANCEL_AFTER_NS,
            self._on_entry_order_cancel_timeout,
        )
        self._set_guard_time_alert(
            escalate_name,
            now_ns + self._ENTRY_ORDER_ESCALATE_AFTER_NS,
            self._on_entry_order_escalation_timeout,
        )

    def _cancel_entry_order_timeouts(self, client_order_id) -> None:
        timer_names = self._entry_order_timer_names.pop(client_order_id, None)
        if timer_names is None:
            return

        for timer_name in timer_names:
            self._entry_order_timeout_order_ids.pop(timer_name, None)
            self._cancel_guard_timer(timer_name)

    def _handle_entry_order_cancel_timeout_for(self, client_order_id) -> None:
        if (
            not self._entry_order_pending
            or self._entry_order is None
            or client_order_id != self._entry_order_client_id
        ):
            return

        self._entry_orders_flatten_on_fill.add(client_order_id)
        self._cancel_order_for_reconciliation(
            self._entry_order,
            reason=(
                f"entry order pending too long "
                f"({self._ENTRY_ORDER_CANCEL_AFTER_NS // 1_000_000_000}s)"
            ),
        )

    def _handle_entry_order_escalation_timeout_for(self, client_order_id) -> None:
        if not self._entry_order_pending or client_order_id != self._entry_order_client_id:
            return

        reason = (
            f"Entry order unresolved after cancel grace "
            f"({self._ENTRY_ORDER_ESCALATE_AFTER_NS // 1_000_000_000}s) "
            f"— stopping for manual reconciliation: {client_order_id}"
        )
        self.log.error(reason)
        self._dispatch_process_stop(reason)

    def _on_entry_order_cancel_timeout(self, event) -> None:
        client_order_id = self._entry_order_timeout_order_ids.get(self._guard_event_name(event))
        if client_order_id is not None:
            self._handle_entry_order_cancel_timeout_for(client_order_id)

    def _on_entry_order_escalation_timeout(self, event) -> None:
        client_order_id = self._entry_order_timeout_order_ids.get(self._guard_event_name(event))
        if client_order_id is not None:
            self._handle_entry_order_escalation_timeout_for(client_order_id)

    def _position_cleanup_timer_name(self, instrument_id: InstrumentId) -> str:
        return self._guard_timer_name("POSITION-CLEANUP", str(instrument_id))

    def _schedule_position_cleanup_retry(self, instrument_id: InstrumentId) -> None:
        timer_name = self._position_cleanup_timer_name(instrument_id)
        previous_name = self._position_cleanup_timer_names.get(instrument_id)
        if previous_name is not None:
            self._position_cleanup_timer_instruments.pop(previous_name, None)
            self._cancel_guard_timer(previous_name)

        self._position_cleanup_timer_names[instrument_id] = timer_name
        self._position_cleanup_timer_instruments[timer_name] = instrument_id
        self._set_guard_time_alert(
            timer_name,
            self._now_ns() + self._POSITION_CLEANUP_RETRY_INTERVAL_NS,
            self._on_position_cleanup_retry,
        )

    def _cancel_position_cleanup(self, instrument_id: InstrumentId) -> None:
        timer_name = self._position_cleanup_timer_names.pop(instrument_id, None)
        if timer_name is not None:
            self._position_cleanup_timer_instruments.pop(timer_name, None)
            self._cancel_guard_timer(timer_name)
        self._position_cleanup_states.pop(instrument_id, None)

    def _cancel_all_position_cleanup(self) -> None:
        for instrument_id in list(self._position_cleanup_timer_names):
            self._cancel_position_cleanup(instrument_id)

    def _resolution_timer_name(self, instrument_id: InstrumentId) -> str:
        return self._guard_timer_name("RESOLUTION-CHECK", str(instrument_id))

    def _schedule_resolution_check(self, instrument_id: InstrumentId) -> None:
        timer_name = self._resolution_timer_name(instrument_id)
        previous_name = self._resolution_timer_names.get(instrument_id)
        if previous_name is not None:
            self._resolution_timer_instruments.pop(previous_name, None)
            self._cancel_guard_timer(previous_name)

        self._resolution_timer_names[instrument_id] = timer_name
        self._resolution_timer_instruments[timer_name] = instrument_id
        self._set_guard_time_alert(
            timer_name,
            self._now_ns() + self._RESOLUTION_POLL_INTERVAL_NS,
            self._on_resolution_check,
        )

    def _cancel_resolution_tracking(self, instrument_id: InstrumentId) -> None:
        timer_name = self._resolution_timer_names.pop(instrument_id, None)
        if timer_name is not None:
            self._resolution_timer_instruments.pop(timer_name, None)
            self._cancel_guard_timer(timer_name)
        self._resolution_pending_instruments.pop(instrument_id, None)

    def _cancel_resolution_polling(self, instrument_id: InstrumentId) -> None:
        timer_name = self._resolution_timer_names.pop(instrument_id, None)
        if timer_name is not None:
            self._resolution_timer_instruments.pop(timer_name, None)
            self._cancel_guard_timer(timer_name)

    def _cancel_all_resolution_tracking(self) -> None:
        for instrument_id in list(self._resolution_timer_names):
            self._cancel_resolution_tracking(instrument_id)

    def _carry_positions_to_resolution(self, instrument_id: InstrumentId, reason: str) -> None:
        self._cancel_position_cleanup(instrument_id)
        self._resolution_settled_instruments.discard(instrument_id)
        self._resolution_pending_instruments[instrument_id] = reason
        self.log.warning(
            f"Carrying residual position on {instrument_id} to market resolution ({reason})"
        )
        self._schedule_resolution_check(instrument_id)

    def _fetch_market_resolution(self, instrument_id: InstrumentId):
        return fetch_market_resolution(
            get_polymarket_condition_id(instrument_id),
            get_polymarket_token_id(instrument_id),
        )

    def _handle_resolution_check_for(self, instrument_id: InstrumentId) -> None:
        reason = self._resolution_pending_instruments.get(instrument_id)
        if reason is None:
            self._cancel_resolution_tracking(instrument_id)
            return

        if not self._has_open_position_on_instrument(instrument_id):
            self.log.info(
                f"Residual position on {instrument_id} closed before resolution ({reason})"
            )
            self._cancel_resolution_tracking(instrument_id)
            self._resolution_settled_instruments.discard(instrument_id)
            self._maybe_finalize_process_stop()
            return

        try:
            resolution = self._fetch_market_resolution(instrument_id)
        except Exception as exc:
            self.log.warning(f"Resolution check failed for {instrument_id}: {exc}")
            self._schedule_resolution_check(instrument_id)
            return

        if not resolution.resolved or resolution.token_won is None:
            self._schedule_resolution_check(instrument_id)
            return

        settlement_value = "1.00" if resolution.token_won else "0.00"
        result_label = "WIN" if resolution.token_won else "LOSS"
        winner_label = resolution.winning_outcome or "unknown"
        token_label = resolution.target_token_outcome or self._selected_outcome_label()
        self.log.warning(
            f"Market resolved for carried residual on {instrument_id}: {token_label} {result_label} "
            f"(winner={winner_label}, settlement={settlement_value}). "
            "Awaiting external wallet settlement reconciliation."
        )
        self._cancel_resolution_polling(instrument_id)

    def _on_resolution_check(self, event) -> None:
        instrument_id = self._resolution_timer_instruments.get(self._guard_event_name(event))
        if instrument_id is not None:
            self._handle_resolution_check_for(instrument_id)

    def _instrument_min_quantity(self, instrument_id: InstrumentId) -> float | None:
        instrument = self.cache.instrument(instrument_id)
        if instrument is None or instrument.min_quantity is None:
            return None
        return float(instrument.min_quantity)

    def _submit_exit_order_for_position(
        self,
        position,
        *,
        reason: str,
        allow_resolution_carry: bool,
    ) -> bool:
        min_qty = self._instrument_min_quantity(position.instrument_id)
        quantity = float(position.quantity)
        if min_qty is not None and quantity < min_qty:
            self.log.warning(
                f"Residual on {position.instrument_id} below minimum close size "
                f"({quantity:.6f} < {min_qty:.6f}) — carrying to resolution ({reason})"
            )
            self._carry_positions_to_resolution(position.instrument_id, reason)
            return False

        self.close_position(
            position,
            time_in_force=TimeInForce.IOC,
            reduce_only=False,
            quote_quantity=False,
        )
        self.log.info(
            f"Submitted exit on {position.instrument_id} qty={position.quantity} ({reason})"
        )
        return True

    def _on_position_cleanup_retry(self, event) -> None:
        instrument_id = self._position_cleanup_timer_instruments.get(self._guard_event_name(event))
        if instrument_id is None:
            return

        state = self._position_cleanup_states.get(instrument_id)
        if state is None:
            self._cancel_position_cleanup(instrument_id)
            return

        if not self._has_open_position_on_instrument(instrument_id):
            self.log.info(f"Position cleanup complete on {instrument_id} — flat again")
            self._cancel_position_cleanup(instrument_id)
            self._maybe_finalize_process_stop()
            return

        if self._now_ns() >= state.deadline_ns:
            if state.allow_resolution_carry:
                self.log.warning(
                    f"Position cleanup did not flatten {instrument_id} within "
                    f"{self._POSITION_CLEANUP_TIMEOUT_NS // 1_000_000_000}s "
                    f"({state.reason}) — carrying residual to resolution"
                )
                self._carry_positions_to_resolution(instrument_id, state.reason)
            else:
                failure_reason = (
                    f"Position cleanup did not flatten {instrument_id} within "
                    f"{self._POSITION_CLEANUP_TIMEOUT_NS // 1_000_000_000}s "
                    f"({state.reason}) — stopping"
                )
                self._cancel_position_cleanup(instrument_id)
                self.log.error(failure_reason)
                self._dispatch_process_stop(failure_reason)
            return

        self.log.warning(
            f"Position cleanup still open on {instrument_id} ({state.reason}) — retrying exit"
        )
        self._close_positions_for_instrument(
            instrument_id,
            reason=state.reason,
            monitor_cleanup=True,
            retry=True,
            allow_resolution_carry=state.allow_resolution_carry,
        )

    def _maybe_finalize_process_stop(self) -> None:
        if not self._process_stop_requested or self._process_stop_dispatched:
            return
        if self._entry_order_pending:
            return
        if self._position_cleanup_states:
            return
        if self._resolution_pending_instruments:
            return
        if self._ioc_orders_requiring_reconciliation():
            return

        for instrument_id_str, _ in self._windows:
            instrument_id = InstrumentId.from_str(instrument_id_str)
            if self._has_open_position_on_instrument(instrument_id):
                return

        self._dispatch_process_stop()

    def _dispatch_process_stop(self, reason: str | None = None) -> None:
        if self._process_stop_dispatched:
            return

        self._process_stop_dispatched = True
        stop_reason = reason or self._process_stop_reason or "process stop requested"
        self.log.warning(f"Stopping node: {stop_reason}")

        if self._process_stop_callback is not None:
            self._process_stop_callback()
        else:
            self.stop()

    def _advance_to_next_window(self, exhausted_message: str) -> None:
        old_instrument_id = self._pm_instrument_id

        self._window_idx += 1
        if self._window_idx >= len(self._windows):
            self.request_process_stop(exhausted_message)
            return

        self._pm_instrument_id = InstrumentId.from_str(self._windows[self._window_idx][0])
        self._window_end_ns = self._windows[self._window_idx][1]
        self._entered_this_window = False
        self._pm_mid = None
        self._pm_mid_ts_ns = None
        self._pm_bid = None
        self._pm_ask = None
        self._pm_bid_size = 0.0
        self._pm_ask_size = 0.0

        self.subscribe_quote_ticks(self._pm_instrument_id)
        self.unsubscribe_quote_ticks(old_instrument_id)
        self._set_next_window_alert()

        remaining = len(self._windows) - self._window_idx - 1
        self.log.info(
            f"Now trading {self._pm_instrument_id} | "
            f"ends {_fmt_ns(self._window_end_ns)} UTC | "
            f"{remaining} window(s) remaining"
        )

    def _roll_to_next_window(self, exhausted_message: str) -> None:
        self.log.info(f"Window ending ({_fmt_ns(self._window_end_ns)} UTC) — rolling over")

        self._cancel_pending_entry_order("window rollover")
        self._close_positions_for_instrument(
            self._pm_instrument_id,
            reason="window end",
            allow_resolution_carry=True,
        )
        self._advance_to_next_window(exhausted_message)

    def _clear_active_entry_order(self, client_order_id) -> None:
        if client_order_id != self._entry_order_client_id:
            return
        self._cancel_entry_order_timeouts(client_order_id)
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
        tracked_order = self._entry_orders_by_id.get(client_order_id)
        self._mark_entry_order_inactive(client_order_id)
        self._entry_orders_by_id.pop(client_order_id, None)
        self._record_order_terminal_in_sandbox_truth(
            client_order_id,
            order=tracked_order,
            message=message,
        )
        self.log.warning(message)
        self._maybe_finalize_process_stop()

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
        self._record_fill_in_sandbox_wallet(event)
        self._record_fill_in_sandbox_order_truth(event)
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
            self._close_positions_for_instrument(
                tracked_instrument_id,
                reason="late fill",
                allow_resolution_carry=self._window_has_ended(tracked_instrument_id),
            )
            self._clear_active_entry_order(event.client_order_id)
        elif (
            event.client_order_id == self._entry_order_client_id
            and event.order_side == OrderSide.BUY
        ):
            self._entered_this_window = True
            self._clear_active_entry_order(event.client_order_id)

        self._trade_count += 1
        self.log.info(f"Fill #{self._trade_count}: price={event.last_px} qty={event.last_qty}")

    def _record_fill_in_sandbox_wallet(self, event) -> None:
        if self._sandbox_wallet_store is None:
            return

        quantity = float(event.last_qty)
        if event.order_side == OrderSide.BUY:
            delta_size = quantity
        elif event.order_side == OrderSide.SELL:
            delta_size = -quantity
        else:
            return

        self._sandbox_wallet_store.apply_trade(
            token_id=str(get_polymarket_token_id(event.instrument_id)),
            delta_size=delta_size,
            price=float(event.last_px),
        )

    def on_position_closed(self, event) -> None:
        self._cancel_tracked_entry_orders_for_instrument(
            event.instrument_id,
            reason="position closed",
        )

        if event.instrument_id in self._resolution_pending_instruments:
            self.log.info(f"Carried residual closed on {event.instrument_id} before resolution")
            self._cancel_resolution_tracking(event.instrument_id)
            self._resolution_settled_instruments.discard(event.instrument_id)
            self._maybe_finalize_process_stop()
        elif event.instrument_id in self._resolution_settled_instruments:
            self._resolution_settled_instruments.discard(event.instrument_id)
            self._maybe_finalize_process_stop()

        if (
            event.instrument_id in self._position_cleanup_states
            and not self._has_open_position_on_instrument(event.instrument_id)
        ):
            self.log.info(f"Position cleanup complete on {event.instrument_id} — flat again")
            self._cancel_position_cleanup(event.instrument_id)
            self._maybe_finalize_process_stop()

    def _record_fill_in_sandbox_order_truth(self, event) -> None:
        if self._sandbox_order_store is None:
            return

        order = None
        try:
            order = self.cache.order(event.client_order_id)
        except Exception:
            order = None

        if order is None:
            order = self._entry_orders_by_id.get(event.client_order_id)
        self._sync_order_in_sandbox_truth(order)

    def _record_order_terminal_in_sandbox_truth(self, client_order_id, *, order, message: str) -> None:
        if self._sandbox_order_store is None:
            return

        normalized = message.lower()
        if "expired" in normalized:
            status = OrderTruthStatus.EXPIRED
        elif "canceled" in normalized:
            status = OrderTruthStatus.CANCELED
        elif "rejected" in normalized or "denied" in normalized:
            status = OrderTruthStatus.NOT_FOUND
        else:
            status = OrderTruthStatus.CLOSED

        self._sandbox_order_store.set_order_status(
            client_order_id=self._client_order_ref(client_order_id),
            venue_order_id=None
            if order is None or getattr(order, "venue_order_id", None) is None
            else str(order.venue_order_id),
            instrument_id=None if order is None else str(getattr(order, "instrument_id", "")) or None,
            status=status,
            remaining_qty=0.0,
        )


def _fmt_ns(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%H:%M:%S")


def _order_remaining_qty(order) -> float | None:
    leaves_qty = getattr(order, "leaves_qty", None)
    if leaves_qty is None:
        return None
    try:
        return float(leaves_qty)
    except (TypeError, ValueError):
        return None
