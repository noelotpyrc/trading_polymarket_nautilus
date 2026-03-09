"""Tests for checked-in live runner profiles."""
import json

import pytest

from live.profiles import ProfileError, RunnerProfile, available_profile_names, load_profile
from live.runs.common import build_strategy, validate_strategy_config
from live.runs import profile as profile_run
from live.runs.profiles import btc_updown_15m_live as btc_updown_15m_live_run
from live.runs.profiles import btc_updown_15m_sandbox as btc_updown_15m_sandbox_run
from live.runs.profiles import random_signal_15m_sandbox as random_signal_15m_sandbox_run


class TestRunnerProfiles:
    def test_catalog_lists_expected_profiles(self):
        assert available_profile_names() == [
            "btc_updown_15m_live",
            "btc_updown_15m_sandbox",
            "random_signal_15m_sandbox",
        ]

    def test_loads_checked_in_profile(self):
        profile = load_profile("btc_updown_15m_live")

        assert profile.name == "btc_updown_15m_live"
        assert profile.strategy == "btc_updown"
        assert profile.slug_pattern == "btc-updown-15m"
        assert profile.hours_ahead == 4
        assert profile.mode == "live"
        assert profile.binance_feed == "global"
        assert profile.run_secs is None
        assert profile.strategy_config == {
            "trade_amount_usdc": 5.0,
            "signal_lookback": 5,
        }

    def test_rejects_unknown_profile_key(self, tmp_path):
        path = tmp_path / "bad.toml"
        path.write_text(
            'strategy = "btc_updown"\n'
            'slug_pattern = "btc-updown-15m"\n'
            'hours_ahead = 4\n'
            'mode = "live"\n'
            'binance_feed = "global"\n'
            'unexpected = "boom"\n'
        )

        with pytest.raises(ProfileError, match="unknown key"):
            load_profile(str(path))

    def test_rejects_invalid_mode(self, tmp_path):
        path = tmp_path / "bad_mode.toml"
        path.write_text(
            'strategy = "btc_updown"\n'
            'slug_pattern = "btc-updown-15m"\n'
            'hours_ahead = 4\n'
            'mode = "paper"\n'
            'binance_feed = "global"\n'
        )

        with pytest.raises(ProfileError, match="mode must be one of"):
            load_profile(str(path))

    def test_run_secs_override_requires_positive_value(self):
        profile = RunnerProfile(
            name="demo",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="live",
            binance_feed="global",
        )

        with pytest.raises(ProfileError, match="positive integer"):
            profile.with_run_secs(0)


class TestSharedStrategyLauncher:
    def test_build_strategy_applies_profile_overrides(self):
        strategy = build_strategy(
            "btc_updown",
            windows=[("a.POLYMARKET", 1), ("b.POLYMARKET", 2)],
            strategy_config={"trade_amount_usdc": 7.5, "signal_lookback": 8},
        )

        assert strategy._windows == [("a.POLYMARKET", 1), ("b.POLYMARKET", 2)]
        assert strategy._trade_amount == 7.5
        assert strategy._signal_lookback == 8

    def test_validate_strategy_config_rejects_unknown_field(self):
        with pytest.raises(ValueError, match="Unknown btc_updown strategy config field"):
            validate_strategy_config("btc_updown", {"made_up": 1})

    def test_validate_strategy_config_rejects_reserved_window_fields(self):
        with pytest.raises(ValueError, match="reserved window keys"):
            validate_strategy_config("btc_updown", {"pm_instrument_ids": []})


class TestProfileRunner:
    def test_run_profile_delegates_to_shared_runner(self, monkeypatch):
        calls = {}
        profile = RunnerProfile(
            name="demo",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="sandbox",
            binance_feed="us",
            run_secs=300,
            strategy_config={"trade_amount_usdc": 6.0},
        )

        monkeypatch.setattr(
            profile_run,
            "validate_strategy_config",
            lambda strategy_name, strategy_config: calls.update(
                {"validated": (strategy_name, strategy_config)}
            ),
        )
        monkeypatch.setattr(
            profile_run,
            "run_strategy",
            lambda strategy_name, **kwargs: calls.update(
                {"strategy_name": strategy_name, **kwargs}
            ),
        )

        profile_run.run_profile(profile)

        assert calls == {
            "validated": ("btc_updown", {"trade_amount_usdc": 6.0}),
            "strategy_name": "btc_updown",
            "slug_pattern": "btc-updown-15m",
            "hours_ahead": 4,
            "sandbox": True,
            "binance_us": True,
            "run_secs": 300,
            "strategy_config": {"trade_amount_usdc": 6.0},
        }

    def test_main_lists_profiles(self, monkeypatch, capsys):
        monkeypatch.setattr(
            profile_run,
            "available_profile_names",
            lambda: ["a_profile", "b_profile"],
        )

        profile_run.main(["--list"])

        assert capsys.readouterr().out == "a_profile\nb_profile\n"

    def test_main_for_profile_applies_run_secs_override(self, monkeypatch):
        seen = {}
        profile = RunnerProfile(
            name="btc_updown_15m_live",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="live",
            binance_feed="global",
            run_secs=None,
            strategy_config={"trade_amount_usdc": 5.0},
        )

        monkeypatch.setattr(profile_run, "load_profile", lambda name: profile)
        monkeypatch.setattr(
            profile_run,
            "run_profile",
            lambda loaded_profile: seen.update({"profile": loaded_profile}),
        )

        profile_run.main_for_profile("btc_updown_15m_live", ["--run-secs", "90"])

        assert seen["profile"].run_secs == 90

    def test_main_for_profile_prints_resolved_profile(self, monkeypatch, capsys):
        profile = RunnerProfile(
            name="btc_updown_15m_live",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="live",
            binance_feed="global",
            strategy_config={"trade_amount_usdc": 5.0},
        )
        monkeypatch.setattr(profile_run, "load_profile", lambda name: profile)

        profile_run.main_for_profile("btc_updown_15m_live", ["--print-profile"])

        rendered = json.loads(capsys.readouterr().out)
        assert rendered["name"] == "btc_updown_15m_live"
        assert rendered["strategy_config"] == {"trade_amount_usdc": 5.0}

    @pytest.mark.parametrize(
        ("module", "expected_name"),
        [
            (btc_updown_15m_live_run, "btc_updown_15m_live"),
            (btc_updown_15m_sandbox_run, "btc_updown_15m_sandbox"),
            (random_signal_15m_sandbox_run, "random_signal_15m_sandbox"),
        ],
    )
    def test_fixed_profile_entrypoints_delegate_to_main_for_profile(
        self,
        monkeypatch,
        module,
        expected_name,
    ):
        calls = {}
        monkeypatch.setattr(
            module,
            "main_for_profile",
            lambda profile_name, argv=None: calls.update(
                {"profile_name": profile_name, "argv": argv}
            ),
        )

        module.main(["--print-profile"])

        assert calls == {
            "profile_name": expected_name,
            "argv": ["--print-profile"],
        }
