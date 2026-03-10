"""Unit tests for live run scripts."""
from types import SimpleNamespace

import pytest

from live.runs import btc_updown as btc_updown_run
from live.runs import random_signal as random_signal_run


def _args():
    return SimpleNamespace(
        slug_pattern="btc-updown-15m",
        hours_ahead=4,
        run_secs=180,
        outcome_side="no",
        sandbox=True,
        binance_us=False,
    )


class TestBtcUpDownRunScript:
    def test_run_delegates_to_shared_runner(self, monkeypatch):
        calls = {}

        monkeypatch.setattr(
            btc_updown_run,
            "run_strategy",
            lambda strategy_name, **kwargs: calls.update(
                {"strategy_name": strategy_name, **kwargs}
            ),
        )

        btc_updown_run.run(_args())

        assert calls == {
            "strategy_name": "btc_updown",
            "slug_pattern": "btc-updown-15m",
            "hours_ahead": 4,
            "outcome_side": "no",
            "sandbox": True,
            "binance_us": False,
            "run_secs": 180,
        }

    def test_run_bubbles_up_shared_runner_failure(self, monkeypatch):
        monkeypatch.setattr(
            btc_updown_run,
            "run_strategy",
            lambda strategy_name, **kwargs: (_ for _ in ()).throw(SystemExit("boom")),
        )

        with pytest.raises(SystemExit, match="boom"):
            btc_updown_run.run(_args())


class TestRandomSignalRunScript:
    def test_run_delegates_to_shared_runner(self, monkeypatch):
        calls = {}

        monkeypatch.setattr(
            random_signal_run,
            "run_strategy",
            lambda strategy_name, **kwargs: calls.update(
                {"strategy_name": strategy_name, **kwargs}
            ),
        )

        random_signal_run.run(_args())

        assert calls == {
            "strategy_name": "random_signal",
            "slug_pattern": "btc-updown-15m",
            "hours_ahead": 4,
            "outcome_side": "no",
            "sandbox": True,
            "binance_us": False,
            "run_secs": 180,
        }

    def test_run_bubbles_up_shared_runner_failure(self, monkeypatch):
        monkeypatch.setattr(
            random_signal_run,
            "run_strategy",
            lambda strategy_name, **kwargs: (_ for _ in ()).throw(SystemExit("boom")),
        )

        with pytest.raises(SystemExit, match="boom"):
            random_signal_run.run(_args())
