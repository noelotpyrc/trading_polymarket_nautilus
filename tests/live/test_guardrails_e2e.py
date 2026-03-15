"""Deterministic end-to-end guardrail scenarios for the live process."""
from datetime import datetime, timezone
from types import SimpleNamespace

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.currencies import USDC, USDT
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from live.resolution import MarketResolution
from live.strategies.btc_updown import BtcUpDownConfig, BtcUpDownStrategy
from live.strategies.windowed import WindowedPolymarketStrategy
from live.wallet_truth import WalletSettlement, WalletTruthSnapshot


BTC = TestInstrumentProvider.btcusdt_perp_binance()
PM = TestInstrumentProvider.binary_option()
BTC_BAR_TYPE = BarType.from_str(f"{BTC.id}-1-MINUTE-LAST-EXTERNAL")

BASE_TS_S = 1_700_000_000
WINDOW_END_NS = (BASE_TS_S + 900) * 1_000_000_000


def _btc_bars_at_offsets(offset_closes: list[tuple[int, float]]) -> list[Bar]:
    bars = []
    for offset_s, close in offset_closes:
        ts_ns = int((BASE_TS_S + offset_s) * 1_000_000_000)
        bars.append(Bar(
            bar_type=BTC_BAR_TYPE,
            open=BTC.make_price(close - 10),
            high=BTC.make_price(close + 10),
            low=BTC.make_price(close - 10),
            close=BTC.make_price(close),
            volume=BTC.make_qty(1.0),
            ts_event=ts_ns,
            ts_init=ts_ns,
        ))
    return bars


def _pm_quotes_at_offsets(offsets_s: list[int], bid: float = 0.50, ask: float = 0.52) -> list[QuoteTick]:
    quotes = []
    for offset_s in offsets_s:
        ts_ns = int((BASE_TS_S + offset_s) * 1_000_000_000)
        quotes.append(QuoteTick(
            instrument_id=PM.id,
            bid_price=PM.make_price(bid),
            ask_price=PM.make_price(ask),
            bid_size=PM.make_qty(100.0),
            ask_size=PM.make_qty(100.0),
            ts_event=ts_ns,
            ts_init=ts_ns,
        ))
    return quotes


class InspectableGapBtcStrategy(BtcUpDownStrategy):
    def __init__(self, config: BtcUpDownConfig):
        super().__init__(config)
        self.submission_bar_timestamps = []

    def submit_order(self, order, *args, **kwargs):
        if order.side == OrderSide.BUY:
            self.submission_bar_timestamps.append(self._last_btc_bar_ts_ns)
        return super().submit_order(order, *args, **kwargs)


def _make_engine():
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
    engine.add_instrument(BTC)
    engine.add_instrument(PM)
    return engine


class TimerEvent:
    def __init__(self, name: str):
        self.name = name


class DummyOrder:
    def __init__(self, client_order_id: str):
        self.client_order_id = client_order_id


class DummyPosition:
    def __init__(self, instrument_id: InstrumentId, quantity: float):
        self.instrument_id = instrument_id
        self.quantity = quantity


class ScenarioConfig(StrategyConfig, frozen=True):
    pm_instrument_ids: tuple[str, ...]
    window_end_times_ns: tuple[int, ...]
    outcome_side: str = "yes"


class GuardrailScenarioStrategy(WindowedPolymarketStrategy):
    def __init__(self, config: ScenarioConfig):
        super().__init__(config)
        self.now_ns = 0
        self.subscription_events = []
        self.guard_alerts: dict[str, tuple[int, object]] = {}
        self.window_alerts = []
        self.canceled_orders = []
        self.closed_instruments = []
        self.submitted_order_ids = []
        self.open_positions = set()
        self.min_quantity_by_instrument = {}
        self.market_resolution_responses = {}
        self.wallet_reconciliations = []
        self.stop_called = False

    def on_start(self):
        self._start_window_lifecycle()

    def on_stop(self):
        self._stop_window_lifecycle()

    def _on_window_end(self, event) -> None:
        self._roll_to_next_window("Scenario exhausted")

    def _now_ns(self) -> int:
        return self.now_ns

    def maybe_submit_entry(self, signal_ts_ns: int, client_order_id: str = "O-1") -> str | None:
        reason = self._entry_guard_reason(signal_ts_ns)
        if reason is not None:
            return reason
        if self._entry_order_pending or self._entered_this_window:
            return "entry not allowed"

        order = DummyOrder(client_order_id)
        self._entry_order = order
        self._entry_order_pending = True
        self._entry_order_client_id = client_order_id
        self._entry_order_instruments[client_order_id] = self._pm_instrument_id
        self._schedule_entry_order_timeouts(client_order_id)
        self.submitted_order_ids.append(client_order_id)
        return None

    def trigger_guard(self, name: str) -> None:
        _, callback = self.guard_alerts[name]
        callback(TimerEvent(name))

    def trigger_window_end(self) -> None:
        self._on_window_end(TimerEvent(self._window_alert_name()))

    def subscribe_quote_ticks(self, instrument_id):
        self.subscription_events.append(("sub", instrument_id))

    def unsubscribe_quote_ticks(self, instrument_id):
        self.subscription_events.append(("unsub", instrument_id))

    def cancel_order(self, order, *args, **kwargs):
        self.canceled_orders.append(order.client_order_id)

    def _open_positions_for_instrument(self, instrument_id):
        if instrument_id not in self.open_positions:
            return []
        return [DummyPosition(instrument_id, 5.0)]

    def _instrument_min_quantity(self, instrument_id):
        return self.min_quantity_by_instrument.get(instrument_id)

    def _fetch_market_resolution(self, instrument_id):
        response = self.market_resolution_responses[instrument_id]
        if isinstance(response, Exception):
            raise response
        return response

    def _submit_exit_order_for_position(
        self,
        position,
        *,
        reason: str,
        allow_resolution_carry: bool,
    ) -> bool:
        self.closed_instruments.append((position.instrument_id, reason))
        return super()._submit_exit_order_for_position(
            position,
            reason=reason,
            allow_resolution_carry=allow_resolution_carry,
        )

    def close_position(self, position, *args, **kwargs):
        return None

    def _has_open_position_on_instrument(self, instrument_id):
        return instrument_id in self.open_positions

    def _set_guard_time_alert(self, name: str, alert_time_ns: int, callback) -> None:
        self.guard_alerts[name] = (alert_time_ns, callback)

    def _cancel_guard_timer(self, name: str) -> None:
        self.guard_alerts.pop(name, None)

    def _set_next_window_alert(self) -> None:
        self.window_alerts.append((self._window_alert_name(), self._window_end_ns))

    def stop(self):
        self.stop_called = True

    def _wallet_token_id_for_instrument(self, instrument_id):
        return "token-a"

    def _instrument_for_wallet_token(self, token_id: str):
        if token_id != "token-a":
            return None
        return InstrumentId.from_str("a.POLYMARKET")

    def _reconcile_position_from_wallet_settlement(self, position, settlement) -> bool:
        self.open_positions.discard(position.instrument_id)
        self.wallet_reconciliations.append(
            {
                "instrument_id": position.instrument_id,
                "settlement_token_id": None if settlement is None else settlement.token_id,
            }
        )
        return True


class WarmupScenarioHarness(BtcUpDownStrategy):
    def __init__(self, config: BtcUpDownConfig, *, start_now: datetime):
        super().__init__(config)
        self._start_now = start_now
        self.now_ns = 0
        self.guard_alerts: dict[str, tuple[int, object]] = {}
        self.window_alerts = []
        self.requested_ranges = []
        self.bars_subscribed = []
        self.quotes_subscribed = []
        self.stop_called = False

    def on_start(self):
        self.subscribe_bars(self._btc_bar_type)
        self._start_btc_warmup(now=self._start_now)
        self._start_window_lifecycle()

    def on_stop(self):
        self._stop_window_lifecycle()

    def request_bars(self, bar_type, start, end=None, callback=None, **kwargs):
        self.requested_ranges.append((bar_type, start, end, callback))
        return "REQ-1"

    def subscribe_bars(self, bar_type):
        self.bars_subscribed.append(bar_type)

    def subscribe_quote_ticks(self, instrument_id):
        self.quotes_subscribed.append(instrument_id)

    def _set_next_window_alert(self) -> None:
        self.window_alerts.append((self._window_alert_name(), self._window_end_ns))

    def _set_guard_time_alert(self, name: str, alert_time_ns: int, callback) -> None:
        self.guard_alerts[name] = (alert_time_ns, callback)

    def _cancel_guard_timer(self, name: str) -> None:
        self.guard_alerts.pop(name, None)

    def _now_ns(self) -> int:
        return self.now_ns

    def trigger_guard(self, name: str) -> None:
        _, callback = self.guard_alerts[name]
        callback(TimerEvent(name))

    def stop(self):
        self.stop_called = True

    def _on_window_end(self, event) -> None:
        raise NotImplementedError


class TestGuardrailFaultInjectionE2E:
    def test_binance_gap_blocks_entry_until_recovery_window_is_clean(self):
        engine = _make_engine()
        offsets = [0, 60, 180, 240, 300]  # Missing the 120s bar
        engine.add_data(_btc_bars_at_offsets([
            (0, 50_000.0),
            (60, 50_100.0),
            (180, 50_200.0),
            (240, 50_300.0),
            (300, 50_400.0),
        ]))
        engine.add_data(_pm_quotes_at_offsets(offsets))

        strategy = InspectableGapBtcStrategy(BtcUpDownConfig(
            pm_instrument_ids=(str(PM.id),),
            window_end_times_ns=(WINDOW_END_NS,),
            signal_lookback=2,
            trade_amount_usdc=5.0,
        ))
        engine.add_strategy(strategy)
        engine.run()

        assert strategy.submission_bar_timestamps == [int((BASE_TS_S + 300) * 1_000_000_000)]
        engine.dispose()

    def test_pending_entry_timeout_cancels_then_stops_when_never_resolved(self):
        strategy = GuardrailScenarioStrategy(ScenarioConfig(
            pm_instrument_ids=("a.POLYMARKET", "b.POLYMARKET"),
            window_end_times_ns=(1_000_000_000, 2_000_000_000),
        ))
        strategy.on_start()
        strategy.on_quote_tick(SimpleNamespace(
            instrument_id=InstrumentId.from_str("a.POLYMARKET"),
            bid_price=0.50,
            ask_price=0.52,
            bid_size=100.0,
            ask_size=100.0,
            ts_event=100,
        ))

        assert strategy.maybe_submit_entry(100) is None
        assert sorted(strategy.guard_alerts) == ["ENTRY-CANCEL:O-1", "ENTRY-ESCALATE:O-1"]

        strategy.trigger_guard("ENTRY-CANCEL:O-1")
        assert strategy.canceled_orders == ["O-1"]
        assert strategy._entry_order_pending is True

        strategy.trigger_guard("ENTRY-ESCALATE:O-1")
        assert strategy.stop_called is True

    def test_late_fill_cleanup_recovers_when_flatten_succeeds(self):
        strategy = GuardrailScenarioStrategy(ScenarioConfig(
            pm_instrument_ids=("a.POLYMARKET", "b.POLYMARKET"),
            window_end_times_ns=(1_000_000_000, 2_000_000_000),
        ))
        strategy.on_start()
        old_instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.on_quote_tick(SimpleNamespace(
            instrument_id=old_instrument,
            bid_price=0.50,
            ask_price=0.52,
            bid_size=100.0,
            ask_size=100.0,
            ts_event=100,
        ))
        assert strategy.maybe_submit_entry(100) is None

        strategy.trigger_window_end()
        strategy.open_positions.add(old_instrument)
        strategy.on_order_filled(SimpleNamespace(
            client_order_id="O-1",
            instrument_id=old_instrument,
            order_side=OrderSide.BUY,
            last_px="0.51",
            last_qty="5",
        ))

        assert strategy.closed_instruments == [(old_instrument, "late fill")]
        assert "POSITION-CLEANUP:a.POLYMARKET" in strategy.guard_alerts

        strategy.open_positions.clear()
        strategy.on_position_closed(SimpleNamespace(instrument_id=old_instrument))

        assert strategy.stop_called is False
        assert "POSITION-CLEANUP:a.POLYMARKET" not in strategy.guard_alerts

    def test_late_fill_cleanup_carries_residual_to_resolution_when_window_has_ended(self):
        strategy = GuardrailScenarioStrategy(ScenarioConfig(
            pm_instrument_ids=("a.POLYMARKET", "b.POLYMARKET"),
            window_end_times_ns=(1_000_000_000, 2_000_000_000),
        ))
        strategy.on_start()
        old_instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.on_quote_tick(SimpleNamespace(
            instrument_id=old_instrument,
            bid_price=0.50,
            ask_price=0.52,
            bid_size=100.0,
            ask_size=100.0,
            ts_event=100,
        ))
        assert strategy.maybe_submit_entry(100) is None

        strategy.trigger_window_end()
        strategy.open_positions.add(old_instrument)
        strategy.on_order_filled(SimpleNamespace(
            client_order_id="O-1",
            instrument_id=old_instrument,
            order_side=OrderSide.BUY,
            last_px="0.51",
            last_qty="5",
        ))

        strategy.now_ns = strategy._POSITION_CLEANUP_TIMEOUT_NS + 1
        strategy.trigger_guard("POSITION-CLEANUP:a.POLYMARKET")

        assert strategy.stop_called is False
        assert strategy._resolution_pending_instruments == {old_instrument: "late fill"}
        assert "RESOLUTION-CHECK:a.POLYMARKET" in strategy.guard_alerts

    def test_active_window_below_min_residual_carries_instead_of_stopping(self):
        strategy = GuardrailScenarioStrategy(ScenarioConfig(
            pm_instrument_ids=("a.POLYMARKET", "b.POLYMARKET"),
            window_end_times_ns=(1_000_000_000, 2_000_000_000),
        ))
        strategy.on_start()
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.open_positions.add(instrument)
        strategy.min_quantity_by_instrument[instrument] = 10.0

        strategy._close_positions_for_instrument(instrument, reason="signal exit")

        assert strategy.stop_called is False
        assert strategy._resolution_pending_instruments == {instrument: "signal exit"}
        assert "RESOLUTION-CHECK:a.POLYMARKET" in strategy.guard_alerts

    def test_process_stop_dispatches_after_carried_residual_settles_in_wallet_truth(self):
        strategy = GuardrailScenarioStrategy(ScenarioConfig(
            pm_instrument_ids=("a.POLYMARKET",),
            window_end_times_ns=(1_000_000_000,),
        ))
        callbacks = []
        strategy.set_process_stop_callback(lambda: callbacks.append("stop"))
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.open_positions.add(instrument)
        strategy.min_quantity_by_instrument[instrument] = 10.0
        strategy.now_ns = 1_500_000_000
        strategy.market_resolution_responses[instrument] = MarketResolution(
            condition_id="cond-a",
            token_id="token-a",
            market_closed=True,
            target_token_outcome="Yes",
            winning_token_id="token-a",
            winning_outcome="Yes",
        )

        strategy.request_process_stop("stop for daily restart")

        assert callbacks == []
        assert strategy._resolution_pending_instruments == {instrument: "stop for daily restart"}

        strategy.trigger_guard("RESOLUTION-CHECK:a.POLYMARKET")

        assert callbacks == []
        assert strategy._resolution_pending_instruments == {instrument: "stop for daily restart"}

        class FakeWalletTruthProvider:
            def snapshot(self):
                return WalletTruthSnapshot(
                    wallet_address="0xabc",
                    collateral_balance=12.0,
                    positions=(),
                    settlements=(
                        WalletSettlement(
                            token_id="token-a",
                            position_size=5.0,
                            settlement_price=1.0,
                            collateral_credit=5.0,
                        ),
                    ),
                )

        strategy.set_wallet_truth_provider(FakeWalletTruthProvider())
        strategy._refresh_wallet_truth()

        assert callbacks == ["stop"]
        assert strategy.stop_called is False
        assert strategy.wallet_reconciliations == [
            {
                "instrument_id": instrument,
                "settlement_token_id": "token-a",
            }
        ]

    def test_warmup_timeout_stops_from_startup_flow(self):
        strategy = WarmupScenarioHarness(
            BtcUpDownConfig(
                pm_instrument_ids=("a.POLYMARKET",),
                window_end_times_ns=(1_000_000_000,),
                warmup_days=14,
                signal_lookback=2,
            ),
            start_now=datetime(2026, 3, 11, 15, 0, tzinfo=timezone.utc),
        )
        strategy.now_ns = 1_000

        strategy.on_start()

        assert strategy.bars_subscribed == [BTC_BAR_TYPE]
        assert strategy.quotes_subscribed == [InstrumentId.from_str("a.POLYMARKET")]
        assert len(strategy.requested_ranges) == 1
        assert "btc_warmup_timeout" in strategy.guard_alerts

        strategy.trigger_guard("btc_warmup_timeout")

        assert strategy._warmup_request_inflight is False
        assert strategy.stop_called is True
