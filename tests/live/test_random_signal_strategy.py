"""Unit tests for random-signal sandbox resolution knobs."""
from types import SimpleNamespace

from nautilus_trader.model.identifiers import InstrumentId

from live.strategies.random_signal import RandomSignalConfig, RandomSignalStrategy


class DummyPosition:
    def __init__(self, instrument_id: InstrumentId, quantity: float):
        self.instrument_id = instrument_id
        self.quantity = quantity


class RandomSignalHarness(RandomSignalStrategy):
    def __init__(self, config: RandomSignalConfig):
        super().__init__(config)
        self.now_ns = 0
        self.positions_by_instrument = {}
        self.exit_requests = []
        self.carry_requests = []
        self.subscription_events = []
        self.alerts = []
        self.canceled_reasons = []
        self.stop_reasons = []

    def subscribe_bars(self, bar_type):
        return None

    def subscribe_quote_ticks(self, instrument_id):
        self.subscription_events.append(("sub", instrument_id))

    def unsubscribe_quote_ticks(self, instrument_id):
        self.subscription_events.append(("unsub", instrument_id))

    def _set_next_window_alert(self) -> None:
        self.alerts.append((self._window_alert_name(), self._window_end_ns))

    def _now_ns(self) -> int:
        return self.now_ns

    def _open_positions_for_instrument(self, instrument_id):
        return list(self.positions_by_instrument.get(instrument_id, []))

    def _cancel_pending_entry_order(self, reason: str) -> None:
        self.canceled_reasons.append(reason)

    def _close_positions_for_instrument(
        self,
        instrument_id,
        reason: str,
        *,
        monitor_cleanup: bool = True,
        retry: bool = False,
        allow_resolution_carry: bool = False,
    ) -> None:
        self.exit_requests.append(
            {
                "instrument_id": instrument_id,
                "reason": reason,
                "monitor_cleanup": monitor_cleanup,
                "retry": retry,
                "allow_resolution_carry": allow_resolution_carry,
            }
        )

    def _carry_positions_to_resolution(self, instrument_id, reason: str) -> None:
        self.carry_requests.append((instrument_id, reason))

    def request_process_stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)


def _strategy(**overrides) -> RandomSignalHarness:
    return RandomSignalHarness(
        RandomSignalConfig(
            pm_instrument_ids=("a.POLYMARKET", "b.POLYMARKET"),
            window_end_times_ns=(1_000, 2_000),
            **overrides,
        )
    )


class TestRandomSignalStrategy:
    def test_disable_signal_exit_skips_exit_close(self, monkeypatch):
        strategy = _strategy(disable_signal_exit=True)
        strategy.now_ns = 10
        strategy._pm_mid_ts_ns = 10
        strategy._pm_bid = 0.55
        strategy._pm_bid_size = 10.0
        strategy._pm_ask = 0.56
        strategy._pm_ask_size = 10.0
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 5.0)]
        monkeypatch.setattr("live.strategies.random_signal.random.random", lambda: 0.95)

        strategy.on_bar(SimpleNamespace(close=10_000, ts_event=10))

        assert strategy.exit_requests == []

    def test_window_end_can_force_position_carry_without_exit_submission(self):
        strategy = _strategy(carry_window_end_position=True)
        instrument_a = InstrumentId.from_str("a.POLYMARKET")
        instrument_b = InstrumentId.from_str("b.POLYMARKET")
        strategy.positions_by_instrument[instrument_a] = [DummyPosition(instrument_a, 5.0)]

        strategy._on_window_end(SimpleNamespace(name="window_end_0"))

        assert strategy.canceled_reasons == ["window rollover"]
        assert strategy.carry_requests == [
            (instrument_a, "window end (forced sandbox residual)")
        ]
        assert strategy.exit_requests == []
        assert strategy._window_idx == 1
        assert strategy._pm_instrument_id == instrument_b
        assert strategy.subscription_events == [
            ("sub", instrument_b),
            ("unsub", instrument_a),
        ]

    def test_window_end_force_carry_exhaustion_requests_process_stop(self):
        strategy = RandomSignalHarness(
            RandomSignalConfig(
                pm_instrument_ids=("a.POLYMARKET",),
                window_end_times_ns=(1_000,),
                carry_window_end_position=True,
            )
        )
        instrument = InstrumentId.from_str("a.POLYMARKET")
        strategy.positions_by_instrument[instrument] = [DummyPosition(instrument, 5.0)]

        strategy._on_window_end(SimpleNamespace(name="window_end_0"))

        assert strategy.carry_requests == [
            (instrument, "window end (forced sandbox residual)")
        ]
        assert strategy.stop_reasons == [
            "No more pre-loaded windows — stopping. Restart the node for the next session."
        ]
