"""Tests for checked-in live runner profiles."""
import json

import pytest

from live.market_metadata import ResolvedWindowMetadata
from live.profiles import ProfileError, RunnerProfile, available_profile_names, load_profile
from live.runs import common as common_run
from live.runs.common import build_strategy, validate_strategy_config
from live.runs import profile as profile_run
from live.runs.profiles import btc_updown_15m_live as btc_updown_15m_live_run
from live.runs.profiles import btc_updown_15m_live_no as btc_updown_15m_live_no_run
from live.runs.profiles import btc_updown_15m_sandbox as btc_updown_15m_sandbox_run
from live.runs.profiles import btc_updown_15m_sandbox_no as btc_updown_15m_sandbox_no_run
from live.runs.profiles import random_signal_15m_resolution_sandbox as random_signal_15m_resolution_sandbox_run
from live.runs.profiles import random_signal_15m_sandbox as random_signal_15m_sandbox_run
from live.runs.profiles import random_signal_15m_sandbox_no as random_signal_15m_sandbox_no_run


class TestRunnerProfiles:
    def test_catalog_lists_expected_profiles(self):
        assert available_profile_names() == [
            "btc_updown_15m_live",
            "btc_updown_15m_live_no",
            "btc_updown_15m_sandbox",
            "btc_updown_15m_sandbox_no",
            "random_signal_15m_resolution_sandbox",
            "random_signal_15m_sandbox",
            "random_signal_15m_sandbox_no",
        ]

    def test_loads_checked_in_profile(self):
        profile = load_profile("btc_updown_15m_live")

        assert profile.name == "btc_updown_15m_live"
        assert profile.strategy == "btc_updown"
        assert profile.slug_pattern == "btc-updown-15m"
        assert profile.hours_ahead == 4
        assert profile.mode == "live"
        assert profile.binance_feed == "global"
        assert profile.outcome_side == "yes"
        assert profile.run_secs is None
        assert profile.strategy_config == {
            "trade_amount_usdc": 5.0,
            "signal_lookback": 5,
            "warmup_days": 14,
        }

    def test_loads_checked_in_warmup_sandbox_profile(self):
        profile = load_profile("btc_updown_15m_sandbox")

        assert profile.name == "btc_updown_15m_sandbox"
        assert profile.mode == "sandbox"
        assert profile.outcome_side == "yes"
        assert profile.run_secs == 600
        assert profile.strategy_config == {
            "trade_amount_usdc": 5.0,
            "signal_lookback": 5,
            "warmup_days": 14,
        }

    def test_loads_checked_in_no_profiles(self):
        live_profile = load_profile("btc_updown_15m_live_no")
        warmup_sandbox_profile = load_profile("btc_updown_15m_sandbox_no")
        sandbox_profile = load_profile("random_signal_15m_sandbox_no")

        assert live_profile.outcome_side == "no"
        assert live_profile.strategy == "btc_updown"
        assert warmup_sandbox_profile.outcome_side == "no"
        assert warmup_sandbox_profile.strategy == "btc_updown"
        assert sandbox_profile.outcome_side == "no"
        assert sandbox_profile.strategy == "random_signal"

    def test_loads_checked_in_resolution_sandbox_profile(self):
        profile = load_profile("random_signal_15m_resolution_sandbox")

        assert profile.mode == "sandbox"
        assert profile.binance_feed == "global"
        assert profile.run_secs == 3600
        assert profile.sandbox_starting_usdc == 10.0
        assert profile.strategy == "random_signal"
        assert profile.strategy_config == {
            "trade_amount_usdc": 5.0,
            "entry_threshold": 0.0,
            "exit_threshold": 0.7,
            "disable_signal_exit": True,
            "carry_window_end_position": True,
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

    def test_rejects_invalid_outcome_side(self, tmp_path):
        path = tmp_path / "bad_side.toml"
        path.write_text(
            'strategy = "btc_updown"\n'
            'slug_pattern = "btc-updown-15m"\n'
            'hours_ahead = 4\n'
            'mode = "live"\n'
            'binance_feed = "global"\n'
            'outcome_side = "down"\n'
        )

        with pytest.raises(ProfileError, match="outcome_side must be one of"):
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

    def test_hours_ahead_override_requires_positive_value(self):
        profile = RunnerProfile(
            name="demo",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="live",
            binance_feed="global",
        )

        with pytest.raises(ProfileError, match="positive integer"):
            profile.with_hours_ahead(0)

    def test_sandbox_starting_usdc_override_requires_sandbox_profile(self):
        profile = RunnerProfile(
            name="demo",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="live",
            binance_feed="global",
        )

        with pytest.raises(ProfileError, match="only valid in sandbox mode"):
            profile.with_sandbox_starting_usdc(10.0)


class TestSharedStrategyLauncher:
    def test_build_strategy_applies_profile_overrides(self):
        strategy = build_strategy(
            "btc_updown",
            windows=[("a.POLYMARKET", 1), ("b.POLYMARKET", 2)],
            outcome_side="no",
            strategy_config={"trade_amount_usdc": 7.5, "signal_lookback": 8, "warmup_days": 14},
        )

        assert strategy._windows == [("a.POLYMARKET", 1), ("b.POLYMARKET", 2)]
        assert strategy._trade_amount == 7.5
        assert strategy._signal_lookback == 8
        assert strategy._warmup_days == 14
        assert strategy._outcome_side == "no"

    def test_validate_strategy_config_rejects_unknown_field(self):
        with pytest.raises(ValueError, match="Unknown btc_updown strategy config field"):
            validate_strategy_config("btc_updown", {"made_up": 1})

    def test_validate_strategy_config_rejects_reserved_window_fields(self):
        with pytest.raises(ValueError, match="reserved runtime keys"):
            validate_strategy_config("btc_updown", {"pm_instrument_ids": []})

    def test_validate_strategy_config_rejects_reserved_outcome_side(self):
        with pytest.raises(ValueError, match="reserved runtime keys"):
            validate_strategy_config("btc_updown", {"outcome_side": "no"})

    def test_run_strategy_uses_strategy_managed_process_stop(self, monkeypatch):
        calls = {"node_stop": 0}

        class FakeNode:
            def __init__(self):
                self.trader = self

            def add_strategy(self, strategy):
                calls["added_strategy"] = strategy

            def build(self):
                calls["built"] = True

            def run(self):
                calls["ran"] = True

            def stop(self):
                calls["node_stop"] += 1

        class FakeStrategy:
            def __init__(self):
                self.stop_callback = None
                self.requested_stop_reasons = []

            def set_process_stop_callback(self, callback):
                self.stop_callback = callback

            def request_process_stop(self, reason):
                self.requested_stop_reasons.append(reason)

        strategy = FakeStrategy()
        node = FakeNode()
        timer_callbacks = []
        canceled = []

        class FakeTimer:
            def __init__(self, callback):
                self._callback = callback

            def cancel(self):
                canceled.append(True)

        monkeypatch.setattr(
            common_run,
            "prepare_run_metadata",
            lambda **kwargs: [
                ResolvedWindowMetadata(
                    slug="btc-updown-15m-1000",
                    condition_id="cond-1",
                    window_end_ns=1_000,
                    yes_token_id="yes-1",
                    no_token_id="no-1",
                    yes_outcome_label="Up",
                    no_outcome_label="Down",
                    selected_outcome_side="yes",
                )
            ],
        )
        monkeypatch.setattr(common_run, "build_node", lambda *args, **kwargs: node)
        monkeypatch.setattr(common_run, "build_strategy", lambda *args, **kwargs: strategy)
        monkeypatch.setattr(
            common_run,
            "schedule_stop",
            lambda stop_target, run_secs: timer_callbacks.append(stop_target) or FakeTimer(stop_target),
        )

        common_run.run_strategy(
            "btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            outcome_side="yes",
            sandbox=True,
            binance_us=False,
            run_secs=180,
            strategy_config={"trade_amount_usdc": 5.0},
        )

        assert calls["added_strategy"] is strategy
        assert calls["built"] is True
        assert calls["ran"] is True
        assert strategy.stop_callback == node.stop
        assert len(timer_callbacks) == 1

        timer_callbacks[0]()

        assert strategy.requested_stop_reasons == ["Auto-stop timer elapsed after 180s"]
        assert calls["node_stop"] == 0
        assert canceled == [True]

    def test_run_strategy_seeds_sandbox_wallet_store_with_starting_balance(self, monkeypatch):
        seen = {}

        class FakeNode:
            def __init__(self):
                self.trader = self

            def add_strategy(self, strategy):
                self.strategy = strategy

            def build(self):
                return None

            def run(self):
                return None

            def stop(self):
                return None

        class FakeStrategy:
            def set_process_stop_callback(self, callback):
                self.stop_callback = callback

            def set_sandbox_wallet_store(self, wallet_store):
                self.wallet_store = wallet_store

            def set_wallet_truth_provider(self, provider):
                self.wallet_truth_provider = provider

            def request_process_stop(self, reason):
                self.reason = reason

        monkeypatch.setenv("POLYMARKET_TEST_WALLET_ADDRESS", "0xtest")
        monkeypatch.setattr(
            common_run,
            "prepare_run_metadata",
            lambda **kwargs: [
                ResolvedWindowMetadata(
                    slug="btc-updown-15m-1000",
                    condition_id="cond-1",
                    window_end_ns=1_000,
                    yes_token_id="yes-1",
                    no_token_id="no-1",
                    yes_outcome_label="Up",
                    no_outcome_label="Down",
                    selected_outcome_side="yes",
                )
            ],
        )
        monkeypatch.setattr(common_run, "build_node", lambda *args, **kwargs: FakeNode())
        monkeypatch.setattr(common_run, "build_strategy", lambda *args, **kwargs: FakeStrategy())
        monkeypatch.setattr(common_run, "schedule_stop", lambda stop_target, run_secs: None)

        class FakeWalletStore:
            def __init__(self, *, wallet_address, collateral_balance, state_path):
                seen["wallet_address"] = wallet_address
                seen["collateral_balance"] = collateral_balance
                seen["state_path"] = state_path

        monkeypatch.setattr(common_run, "SandboxWalletStore", FakeWalletStore)
        monkeypatch.setattr(
            common_run,
            "SandboxWalletTruthProvider",
            lambda wallet_store, registry: ("provider", wallet_store, registry),
        )

        common_run.run_strategy(
            "btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            outcome_side="yes",
            sandbox=True,
            binance_us=False,
            run_secs=180,
            strategy_config={"trade_amount_usdc": 5.0},
            sandbox_wallet_state_path="/tmp/wallet.json",
            sandbox_starting_usdc=12.5,
        )

        assert seen == {
            "wallet_address": "0xtest",
            "collateral_balance": 12.5,
            "state_path": "/tmp/wallet.json",
        }


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
            outcome_side="no",
            run_secs=300,
            sandbox_starting_usdc=12.5,
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

        profile_run.run_profile(profile, sandbox_wallet_state_path="/tmp/wallet.json")

        assert calls == {
            "validated": ("btc_updown", {"trade_amount_usdc": 6.0}),
            "strategy_name": "btc_updown",
            "slug_pattern": "btc-updown-15m",
            "hours_ahead": 4,
            "outcome_side": "no",
            "sandbox": True,
            "binance_us": True,
            "run_secs": 300,
            "sandbox_starting_usdc": 12.5,
            "strategy_config": {"trade_amount_usdc": 6.0},
            "sandbox_wallet_state_path": "/tmp/wallet.json",
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
            outcome_side="yes",
            run_secs=None,
            strategy_config={"trade_amount_usdc": 5.0},
        )

        monkeypatch.setattr(profile_run, "load_profile", lambda name: profile)
        monkeypatch.setattr(
            profile_run,
            "run_profile",
            lambda loaded_profile, sandbox_wallet_state_path=None: seen.update(
                {
                    "profile": loaded_profile,
                    "sandbox_wallet_state_path": sandbox_wallet_state_path,
                }
            ),
        )

        profile_run.main_for_profile("btc_updown_15m_live", ["--run-secs", "90"])

        assert seen["profile"].run_secs == 90
        assert seen["sandbox_wallet_state_path"] is None

    def test_main_for_profile_applies_hours_ahead_override(self, monkeypatch):
        seen = {}
        profile = RunnerProfile(
            name="btc_updown_15m_live",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="live",
            binance_feed="global",
            outcome_side="yes",
            run_secs=None,
            strategy_config={"trade_amount_usdc": 5.0},
        )

        monkeypatch.setattr(profile_run, "load_profile", lambda name: profile)
        monkeypatch.setattr(
            profile_run,
            "run_profile",
            lambda loaded_profile, sandbox_wallet_state_path=None: seen.update(
                {
                    "profile": loaded_profile,
                    "sandbox_wallet_state_path": sandbox_wallet_state_path,
                }
            ),
        )

        profile_run.main_for_profile("btc_updown_15m_live", ["--hours-ahead", "8"])

        assert seen["profile"].hours_ahead == 8
        assert seen["sandbox_wallet_state_path"] is None

    def test_main_for_profile_forwards_sandbox_wallet_state_path(self, monkeypatch):
        seen = {}
        profile = RunnerProfile(
            name="btc_updown_15m_sandbox",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="sandbox",
            binance_feed="global",
            outcome_side="yes",
            run_secs=300,
            strategy_config={"trade_amount_usdc": 5.0},
        )

        monkeypatch.setattr(profile_run, "load_profile", lambda name: profile)
        monkeypatch.setattr(
            profile_run,
            "run_profile",
            lambda loaded_profile, sandbox_wallet_state_path=None: seen.update(
                {
                    "profile": loaded_profile,
                    "sandbox_wallet_state_path": sandbox_wallet_state_path,
                }
            ),
        )

        profile_run.main_for_profile(
            "btc_updown_15m_sandbox",
            ["--sandbox-wallet-state-path", "/tmp/wallet.json"],
        )

        assert seen["profile"] is profile
        assert seen["sandbox_wallet_state_path"] == "/tmp/wallet.json"

    def test_main_for_profile_applies_sandbox_starting_usdc_override(self, monkeypatch):
        seen = {}
        profile = RunnerProfile(
            name="btc_updown_15m_sandbox",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="sandbox",
            binance_feed="global",
            outcome_side="yes",
            run_secs=300,
            strategy_config={"trade_amount_usdc": 5.0},
        )

        monkeypatch.setattr(profile_run, "load_profile", lambda name: profile)
        monkeypatch.setattr(
            profile_run,
            "run_profile",
            lambda loaded_profile, sandbox_wallet_state_path=None: seen.update(
                {
                    "profile": loaded_profile,
                    "sandbox_wallet_state_path": sandbox_wallet_state_path,
                }
            ),
        )

        profile_run.main_for_profile(
            "btc_updown_15m_sandbox",
            ["--sandbox-starting-usdc", "15.5"],
        )

        assert seen["profile"].sandbox_starting_usdc == 15.5

    def test_main_for_profile_prints_resolved_profile(self, monkeypatch, capsys):
        profile = RunnerProfile(
            name="btc_updown_15m_live",
            strategy="btc_updown",
            slug_pattern="btc-updown-15m",
            hours_ahead=4,
            mode="live",
            binance_feed="global",
            outcome_side="yes",
            strategy_config={"trade_amount_usdc": 5.0},
        )
        monkeypatch.setattr(profile_run, "load_profile", lambda name: profile)

        profile_run.main_for_profile("btc_updown_15m_live", ["--print-profile"])

        rendered = json.loads(capsys.readouterr().out)
        assert rendered["name"] == "btc_updown_15m_live"
        assert rendered["outcome_side"] == "yes"
        assert rendered["strategy_config"] == {"trade_amount_usdc": 5.0}

    @pytest.mark.parametrize(
        ("module", "expected_name"),
        [
            (btc_updown_15m_live_run, "btc_updown_15m_live"),
            (btc_updown_15m_live_no_run, "btc_updown_15m_live_no"),
            (btc_updown_15m_sandbox_run, "btc_updown_15m_sandbox"),
            (btc_updown_15m_sandbox_no_run, "btc_updown_15m_sandbox_no"),
            (random_signal_15m_resolution_sandbox_run, "random_signal_15m_resolution_sandbox"),
            (random_signal_15m_sandbox_run, "random_signal_15m_sandbox"),
            (random_signal_15m_sandbox_no_run, "random_signal_15m_sandbox_no"),
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
