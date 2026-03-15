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
        sandbox_starting_usdc=None,
        sandbox_wallet_state_path=None,
        entry_threshold=None,
        exit_threshold=None,
        trade_amount_usdc=None,
        disable_signal_exit=False,
        carry_window_end_position=False,
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
            "sandbox_starting_usdc": None,
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
            "sandbox_starting_usdc": None,
        }

    def test_run_bubbles_up_shared_runner_failure(self, monkeypatch):
        monkeypatch.setattr(
            random_signal_run,
            "run_strategy",
            lambda strategy_name, **kwargs: (_ for _ in ()).throw(SystemExit("boom")),
        )

        with pytest.raises(SystemExit, match="boom"):
            random_signal_run.run(_args())

    def test_run_passes_random_signal_strategy_overrides(self, monkeypatch):
        calls = {}
        args = _args()
        args.entry_threshold = 0.0
        args.exit_threshold = 0.8
        args.trade_amount_usdc = 7.5
        args.disable_signal_exit = True
        args.carry_window_end_position = True

        monkeypatch.setattr(
            random_signal_run,
            "run_strategy",
            lambda strategy_name, **kwargs: calls.update(
                {"strategy_name": strategy_name, **kwargs}
            ),
        )

        random_signal_run.run(args)

        assert calls["strategy_name"] == "random_signal"
        assert calls["strategy_config"] == {
            "entry_threshold": 0.0,
            "exit_threshold": 0.8,
            "trade_amount_usdc": 7.5,
            "disable_signal_exit": True,
            "carry_window_end_position": True,
        }
