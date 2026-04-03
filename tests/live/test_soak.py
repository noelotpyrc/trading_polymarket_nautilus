"""Tests for the soak-run harness."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from live import soak
from live.profiles import ProfileError, RunnerProfile


def _profile(
    *,
    name: str = "random_signal_15m_sandbox",
    mode: str = "sandbox",
    run_secs: int | None = 180,
) -> RunnerProfile:
    return RunnerProfile(
        name=name,
        strategy="random_signal",
        slug_pattern="btc-updown-15m",
        hours_ahead=1,
        mode=mode,
        binance_feed="global",
        run_secs=run_secs,
    )


def _allow_env_validation(monkeypatch) -> None:
    monkeypatch.setattr(soak, "validate_required_env_vars", lambda sandbox, env_file=None: None)


class TestPrepareProfile:
    def test_rejects_live_profile_by_default(self):
        with pytest.raises(ProfileError, match="sandbox profiles by default"):
            soak._prepare_profile(
                _profile(mode="live", run_secs=300),
                run_secs=None,
                hours_ahead=None,
                allow_live=False,
                allow_unbounded=False,
            )

    def test_rejects_unbounded_profile_by_default(self):
        with pytest.raises(ProfileError, match="requires a bounded runtime"):
            soak._prepare_profile(
                _profile(run_secs=None),
                run_secs=None,
                hours_ahead=None,
                allow_live=False,
                allow_unbounded=False,
            )

    def test_run_secs_override_is_applied(self):
        prepared = soak._prepare_profile(
            _profile(run_secs=180),
            run_secs=3600,
            hours_ahead=None,
            allow_live=False,
            allow_unbounded=False,
        )

        assert prepared.run_secs == 3600

    def test_hours_ahead_override_is_applied(self):
        prepared = soak._prepare_profile(
            _profile(run_secs=180),
            run_secs=None,
            hours_ahead=8,
            allow_live=False,
            allow_unbounded=False,
        )

        assert prepared.hours_ahead == 8


class TestRunSoakBatch:
    def test_batch_writes_logs_and_summaries(self, tmp_path, monkeypatch):
        profile = _profile()
        times = iter([
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 14, tzinfo=timezone.utc),
        ])

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profile)
        _allow_env_validation(monkeypatch)

        def fake_run(command, cwd, stdout, stderr, text, check):
            stdout.write("runner output\n")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(soak.subprocess, "run", fake_run)

        batch = soak.run_soak_batch(
            profile_refs=["random_signal_15m_sandbox"],
            run_secs=600,
            hours_ahead=8,
            output_root=tmp_path,
            label="stage7",
            keep_going=False,
            allow_live=False,
            allow_unbounded=False,
            sandbox_wallet_state_path=None,
        )

        batch_dir = Path(batch["batch_dir"])
        run_dir = batch_dir / "01_random_signal_15m_sandbox"

        assert batch["status"] == "passed"
        assert batch_dir.name == "20260312T150000Z_stage7"
        assert (run_dir / "runner.log").read_text(encoding="utf-8").startswith("# profile=random_signal_15m_sandbox")
        assert (run_dir / "profile.json").exists()
        assert (run_dir / "summary.json").exists()
        assert (batch_dir / "summary.json").exists()
        assert batch["hours_ahead_override"] == 8
        assert batch["results"][0]["run_secs"] == 600
        assert batch["results"][0]["hours_ahead"] == 8
        assert "--hours-ahead 8" in (run_dir / "command.txt").read_text(encoding="utf-8")
        assert batch["results"][0]["wallet_state_path"] == str(run_dir / "wallet_state.json")
        assert batch["results"][0]["events_path"] == str(run_dir / "events.jsonl")
        assert batch["results"][0]["status_path"] == str(run_dir / "status.json")
        assert batch["results"][0]["status_history_path"] == str(run_dir / "status_history.jsonl")
        command_text = (run_dir / "command.txt").read_text(encoding="utf-8")
        assert "--sandbox-wallet-state-path" in command_text
        assert "--events-path" in command_text
        assert "--status-path" in command_text
        assert "--status-history-path" in command_text
        assert str(run_dir / "events.jsonl") in command_text
        assert str(run_dir / "status.json") in command_text
        assert str(run_dir / "status_history.jsonl") in command_text
        assert str(run_dir / "wallet_state.json") in command_text

    def test_batch_stops_after_first_failure_without_keep_going(self, tmp_path, monkeypatch):
        profiles = {
            "first": _profile(name="first"),
            "second": _profile(name="second"),
        }
        times = iter([
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 3, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 4, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 5, tzinfo=timezone.utc),
        ])

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profiles[ref])
        _allow_env_validation(monkeypatch)

        seen_commands = []

        def fake_run(command, cwd, stdout, stderr, text, check):
            seen_commands.append(command)
            return SimpleNamespace(returncode=2)

        monkeypatch.setattr(soak.subprocess, "run", fake_run)

        batch = soak.run_soak_batch(
            profile_refs=["first", "second"],
            run_secs=None,
            hours_ahead=None,
            output_root=tmp_path,
            label=None,
            keep_going=False,
            allow_live=False,
            allow_unbounded=False,
            sandbox_wallet_state_path=None,
        )

        assert batch["status"] == "failed"
        assert len(batch["results"]) == 1
        assert len(seen_commands) == 1

    def test_batch_continues_with_keep_going(self, tmp_path, monkeypatch):
        profiles = {
            "first": _profile(name="first"),
            "second": _profile(name="second"),
        }
        times = iter([
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 3, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 4, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 6, tzinfo=timezone.utc),
        ])

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profiles[ref])
        _allow_env_validation(monkeypatch)

        returncodes = iter([3, 0])

        def fake_run(command, cwd, stdout, stderr, text, check):
            return SimpleNamespace(returncode=next(returncodes))

        monkeypatch.setattr(soak.subprocess, "run", fake_run)

        batch = soak.run_soak_batch(
            profile_refs=["first", "second"],
            run_secs=None,
            hours_ahead=None,
            output_root=tmp_path,
            label=None,
            keep_going=True,
            allow_live=False,
            allow_unbounded=False,
            sandbox_wallet_state_path=None,
        )

        assert batch["status"] == "failed"
        assert [result["exit_code"] for result in batch["results"]] == [3, 0]

    def test_batch_uses_explicit_sandbox_wallet_state_path(self, tmp_path, monkeypatch):
        profile = _profile()
        times = iter([
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 14, tzinfo=timezone.utc),
        ])

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profile)
        _allow_env_validation(monkeypatch)
        monkeypatch.setattr(
            soak.subprocess,
            "run",
            lambda command, cwd, stdout, stderr, text, check: SimpleNamespace(returncode=0),
        )

        wallet_state_path = tmp_path / "shared_wallet.json"
        batch = soak.run_soak_batch(
            profile_refs=["random_signal_15m_sandbox"],
            run_secs=600,
            hours_ahead=None,
            output_root=tmp_path,
            label=None,
            keep_going=False,
            allow_live=False,
            allow_unbounded=False,
            sandbox_wallet_state_path=str(wallet_state_path),
        )

        result = batch["results"][0]
        assert result["wallet_state_path"] == str(wallet_state_path)

    def test_batch_rejects_companion_resolution_worker(self, tmp_path):
        with pytest.raises(ValueError, match="no longer launches the resolution worker"):
            soak.run_soak_batch(
                profile_refs=["random_signal_15m_resolution_sandbox"],
                run_secs=None,
                hours_ahead=None,
                output_root=tmp_path,
                label="stage8",
                keep_going=False,
                allow_live=False,
                allow_unbounded=False,
                sandbox_wallet_state_path=None,
                sandbox_starting_usdc=None,
                with_resolution_worker=True,
                resolution_interval_secs=15,
            )

    def test_batch_can_run_companion_alert_monitor(self, tmp_path, monkeypatch):
        profile = _profile(name="random_signal_15m_alert_monitor_sandbox", run_secs=600)
        times = iter([
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 14, tzinfo=timezone.utc),
        ])

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profile)
        _allow_env_validation(monkeypatch)

        def fake_run(command, cwd, stdout, stderr, text, check):
            stdout.write("runner output\n")
            return SimpleNamespace(returncode=0)

        class FakeMonitorProcess:
            def __init__(self, command, cwd, stdout, stderr, text):
                self.command = command
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        monkeypatch.setattr(soak.subprocess, "run", fake_run)
        monkeypatch.setattr(soak.subprocess, "Popen", FakeMonitorProcess)

        batch = soak.run_soak_batch(
            profile_refs=["random_signal_15m_alert_monitor_sandbox"],
            run_secs=None,
            hours_ahead=None,
            output_root=tmp_path,
            label="stage13",
            keep_going=False,
            allow_live=False,
            allow_unbounded=False,
            sandbox_wallet_state_path=None,
            with_alert_monitor=True,
            alert_monitor_interval_secs=11,
        )

        run_dir = Path(batch["batch_dir"]) / "01_random_signal_15m_alert_monitor_sandbox"
        result = batch["results"][0]
        monitor_command_text = (run_dir / "alert_monitor_command.txt").read_text(encoding="utf-8")

        assert batch["status"] == "passed"
        assert batch["with_alert_monitor"] is True
        assert batch["alert_monitor_interval_secs"] == 11
        assert result["alert_monitor"] is True
        assert result["alert_monitor_terminated_by_harness"] is True
        assert result["alert_monitor_log_path"] == str(run_dir / "alert_monitor.log")
        assert result["alerts_path"] == str(run_dir / "alerts.jsonl")
        assert "--interval-secs 11" in monitor_command_text
        assert "--allow-missing-startup-status" in monitor_command_text
        assert str(run_dir) in monitor_command_text

    def test_batch_does_not_fail_when_alert_monitor_exits_nonzero(self, tmp_path, monkeypatch):
        profile = _profile(name="random_signal_15m_alert_monitor_nonfatal_sandbox", run_secs=600)
        times = iter([
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 14, tzinfo=timezone.utc),
        ])

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profile)
        _allow_env_validation(monkeypatch)

        def fake_run(command, cwd, stdout, stderr, text, check):
            stdout.write("runner output\n")
            return SimpleNamespace(returncode=0)

        class FakeMonitorProcess:
            def __init__(self, command, cwd, stdout, stderr, text):
                self.command = command
                self.returncode = 1

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        monkeypatch.setattr(soak.subprocess, "run", fake_run)
        monkeypatch.setattr(soak.subprocess, "Popen", FakeMonitorProcess)

        batch = soak.run_soak_batch(
            profile_refs=["random_signal_15m_alert_monitor_nonfatal_sandbox"],
            run_secs=None,
            hours_ahead=None,
            output_root=tmp_path,
            label="stage13",
            keep_going=False,
            allow_live=False,
            allow_unbounded=False,
            sandbox_wallet_state_path=None,
            with_alert_monitor=True,
            alert_monitor_interval_secs=11,
        )

        result = batch["results"][0]

        assert batch["status"] == "passed"
        assert result["status"] == "passed"
        assert result["alert_monitor_exit_code"] == 1
        assert result["alert_monitor_failed"] is True
        assert result["alert_monitor_terminated_by_harness"] is False

    def test_batch_forwards_env_file_to_runner(self, tmp_path, monkeypatch):
        profile = _profile(name="random_signal_15m_resolution_sandbox", run_secs=600)
        times = iter([
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 14, tzinfo=timezone.utc),
        ])

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profile)
        _allow_env_validation(monkeypatch)

        def fake_run(command, cwd, stdout, stderr, text, check):
            stdout.write("runner output\n")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(soak.subprocess, "run", fake_run)

        batch = soak.run_soak_batch(
            profile_refs=["random_signal_15m_resolution_sandbox"],
            run_secs=600,
            hours_ahead=None,
            output_root=tmp_path,
            label=None,
            keep_going=False,
            allow_live=False,
            allow_unbounded=False,
            sandbox_wallet_state_path=None,
            sandbox_starting_usdc=None,
            env_file="/tmp/live_wallet.env",
        )

        result = batch["results"][0]

        assert result["command"][3:5] == ["--env-file", "/tmp/live_wallet.env"]
        assert result["worker_command"] is None

    def test_batch_can_run_live_profile_without_resolution_worker(self, tmp_path, monkeypatch):
        profile = _profile(name="btc_updown_15m_live_worker", mode="live", run_secs=600)
        times = iter([
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 14, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 15, tzinfo=timezone.utc),
        ])

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profile)
        _allow_env_validation(monkeypatch)

        def fake_run(command, cwd, stdout, stderr, text, check):
            stdout.write("runner output\n")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(soak.subprocess, "run", fake_run)

        batch = soak.run_soak_batch(
            profile_refs=["btc_updown_15m_live_worker"],
            run_secs=600,
            hours_ahead=2,
            output_root=tmp_path,
            label="live_stage",
            keep_going=False,
            allow_live=True,
            allow_unbounded=False,
            sandbox_wallet_state_path=None,
            sandbox_starting_usdc=None,
            env_file="/tmp/live_wallet.env",
        )

        result = batch["results"][0]

        assert batch["status"] == "passed"
        assert result["sandbox"] is False
        assert result["wallet_state_path"] is None
        assert result["resolution_worker"] is False
        assert result["worker_command"] is None
        assert result["worker_execute_redemptions"] is None
        assert result["worker_rpc_url"] is None
        assert result["command"][3:5] == ["--env-file", "/tmp/live_wallet.env"]

    def test_batch_fails_fast_on_missing_live_env_before_spawning_alert_monitor(self, tmp_path, monkeypatch):
        profile = _profile(name="btc_updown_15m_live_missing_env", mode="live", run_secs=600)
        timestamps = [
            datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 3, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 15, 0, 4, tzinfo=timezone.utc),
        ]
        times = iter(timestamps)

        monkeypatch.setattr(soak, "_utc_now", lambda: next(times, timestamps[-1]))
        monkeypatch.setattr(soak, "load_profile", lambda ref: profile)

        popen_calls: list[list[str]] = []

        def fake_popen(*args, **kwargs):
            popen_calls.append(args[0])
            raise AssertionError("companion processes should not start on env preflight failure")

        monkeypatch.setattr(
            soak,
            "validate_required_env_vars",
            lambda sandbox, env_file=None: (_ for _ in ()).throw(
                SystemExit("Missing required live env vars: PRIVATE_KEY")
            ),
        )
        monkeypatch.setattr(soak.subprocess, "Popen", fake_popen)

        batch = soak.run_soak_batch(
            profile_refs=["btc_updown_15m_live_missing_env"],
            run_secs=600,
            hours_ahead=2,
            output_root=tmp_path,
            label="live_missing_env",
            keep_going=False,
            allow_live=True,
            allow_unbounded=False,
            sandbox_wallet_state_path=None,
            sandbox_starting_usdc=None,
            with_alert_monitor=True,
            alert_monitor_interval_secs=15,
            env_file="/tmp/live_wallet.env",
        )

        run_dir = Path(batch["batch_dir"]) / "01_btc_updown_15m_live_missing_env"
        result = batch["results"][0]

        assert batch["status"] == "failed"
        assert result["status"] == "failed"
        assert result["exit_code"] == 1
        assert result["worker_command"] is None
        assert result["alert_monitor_command"] is None
        assert result["worker_log_path"] is None
        assert result["alert_monitor_log_path"] is None
        assert popen_calls == []
        assert "Missing required live env vars: PRIVATE_KEY" in (
            run_dir / "runner.log"
        ).read_text(encoding="utf-8")


class TestMain:
    def test_main_lists_profiles(self, monkeypatch, capsys):
        monkeypatch.setattr(soak, "available_profile_names", lambda: ["a_profile", "b_profile"])

        soak.main(["--list"])

        assert capsys.readouterr().out == "a_profile\nb_profile\n"

    def test_main_rejects_resolution_worker_flag(self, capsys):
        with pytest.raises(SystemExit):
            soak.main(["demo_profile", "--with-resolution-worker"])

        assert "no longer supported by soak.py" in capsys.readouterr().err
