"""Unit tests for live run scripts."""
from types import SimpleNamespace

import pytest

from live.runs import btc_updown as btc_updown_run
from live.runs import random_signal as random_signal_run
from live.strategies.btc_updown import BtcUpDownStrategy
from live.strategies.random_signal import RandomSignalStrategy


class FakeTrader:
    def __init__(self):
        self.strategies = []

    def add_strategy(self, strategy) -> None:
        self.strategies.append(strategy)


class FakeNode:
    def __init__(self):
        self.trader = FakeTrader()
        self.build_called = False
        self.run_called = False

    def build(self) -> None:
        self.build_called = True

    def run(self) -> None:
        self.run_called = True


def _args():
    return SimpleNamespace(
        slug_pattern="btc-updown-15m",
        hours_ahead=4,
        run_secs=180,
        sandbox=True,
        binance_us=False,
    )


class TestBtcUpDownRunScript:
    def test_run_builds_node_and_strategy(self, monkeypatch):
        node = FakeNode()
        build_args = {}
        stop_calls = {}

        monkeypatch.setattr(
            btc_updown_run,
            "prepare_run",
            lambda **kwargs: [("a.POLYMARKET", 1), ("b.POLYMARKET", 2)],
        )

        def _build_node(pm_ids, sandbox, binance_us):
            build_args["pm_ids"] = pm_ids
            build_args["sandbox"] = sandbox
            build_args["binance_us"] = binance_us
            return node

        monkeypatch.setattr(btc_updown_run, "build_node", _build_node)
        monkeypatch.setattr(
            btc_updown_run,
            "schedule_stop",
            lambda node_arg, run_secs: stop_calls.update({"node": node_arg, "run_secs": run_secs}),
        )

        btc_updown_run.run(_args())

        assert build_args == {
            "pm_ids": ["a.POLYMARKET", "b.POLYMARKET"],
            "sandbox": True,
            "binance_us": False,
        }
        assert stop_calls == {"node": node, "run_secs": 180}
        assert node.build_called is True
        assert node.run_called is True
        assert len(node.trader.strategies) == 1
        strategy = node.trader.strategies[0]
        assert isinstance(strategy, BtcUpDownStrategy)
        assert strategy._windows == [("a.POLYMARKET", 1), ("b.POLYMARKET", 2)]

    def test_run_exits_when_preflight_fails(self, monkeypatch):
        monkeypatch.setattr(
            btc_updown_run,
            "prepare_run",
            lambda **kwargs: (_ for _ in ()).throw(SystemExit("boom")),
        )

        with pytest.raises(SystemExit, match="boom"):
            btc_updown_run.run(_args())


class TestRandomSignalRunScript:
    def test_run_builds_node_and_strategy(self, monkeypatch):
        node = FakeNode()
        build_args = {}
        stop_calls = {}

        monkeypatch.setattr(
            random_signal_run,
            "prepare_run",
            lambda **kwargs: [("x.POLYMARKET", 10)],
        )

        def _build_node(pm_ids, sandbox, binance_us):
            build_args["pm_ids"] = pm_ids
            build_args["sandbox"] = sandbox
            build_args["binance_us"] = binance_us
            return node

        monkeypatch.setattr(random_signal_run, "build_node", _build_node)
        monkeypatch.setattr(
            random_signal_run,
            "schedule_stop",
            lambda node_arg, run_secs: stop_calls.update({"node": node_arg, "run_secs": run_secs}),
        )

        random_signal_run.run(_args())

        assert build_args == {
            "pm_ids": ["x.POLYMARKET"],
            "sandbox": True,
            "binance_us": False,
        }
        assert stop_calls == {"node": node, "run_secs": 180}
        assert node.build_called is True
        assert node.run_called is True
        assert len(node.trader.strategies) == 1
        strategy = node.trader.strategies[0]
        assert isinstance(strategy, RandomSignalStrategy)
        assert strategy._windows == [("x.POLYMARKET", 10)]

    def test_run_exits_when_preflight_fails(self, monkeypatch):
        monkeypatch.setattr(
            random_signal_run,
            "prepare_run",
            lambda **kwargs: (_ for _ in ()).throw(SystemExit("boom")),
        )

        with pytest.raises(SystemExit, match="boom"):
            random_signal_run.run(_args())
