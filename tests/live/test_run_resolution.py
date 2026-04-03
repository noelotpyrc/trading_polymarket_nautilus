"""Tests for the external resolution worker CLI and wiring."""
import json
from types import SimpleNamespace

import pytest

from live.market_metadata import ResolvedWindowMetadata, WindowMetadataRegistry
from live.profiles import RunnerProfile
from live.resolution_worker import ResolutionScanResult
from live import run_resolution


def _profile(*, mode: str = "sandbox") -> RunnerProfile:
    return RunnerProfile(
        name=f"btc_updown_15m_{mode}",
        strategy="btc_updown",
        slug_pattern="btc-updown-15m",
        hours_ahead=4,
        mode=mode,
        binance_feed="global",
        outcome_side="yes",
        run_secs=600 if mode == "sandbox" else None,
    )


def _metadata() -> list[ResolvedWindowMetadata]:
    return [
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
    ]


def test_main_lists_profiles(monkeypatch, capsys):
    monkeypatch.setattr(run_resolution, "available_profile_names", lambda: ["one", "two"])

    run_resolution.main(["--list"])

    assert capsys.readouterr().out == "one\ntwo\n"


def test_main_once_runs_single_scan(monkeypatch, capsys, tmp_path):
    class FakeWorker:
        def scan_once(self):
            return [
                ResolutionScanResult(
                    condition_id="cond-1",
                    instrument_id="cond-1-yes-1.POLYMARKET",
                    token_id="yes-1",
                    position_size=2.5,
                    resolved=True,
                    settlement_price=1.0,
                    token_won=True,
                    status="settled",
                )
            ]

    monkeypatch.setattr(run_resolution, "load_profile", lambda name: _profile(mode="sandbox"))
    monkeypatch.setattr(
        run_resolution,
        "resolve_upcoming_window_metadata",
        lambda slug_pattern, **kwargs: _metadata(),
    )
    monkeypatch.setattr(run_resolution, "_build_worker", lambda **kwargs: FakeWorker())

    status_path = tmp_path / "status.json"
    status_history_path = tmp_path / "status_history.jsonl"

    run_resolution.main([
        "btc_updown_15m_sandbox",
        "--once",
        "--sandbox-wallet-state-path",
        "/tmp/wallet.json",
        "--status-path",
        str(status_path),
        "--status-history-path",
        str(status_history_path),
    ])

    out = capsys.readouterr().out
    assert "Sandbox mode: startup window metadata is authoritative." in out
    assert "cond-1-yes-1.POLYMARKET size=2.500000 status=settled settled=1.00" in out
    latest = json.loads(status_path.read_text(encoding="utf-8"))
    history = [
        json.loads(line)
        for line in status_history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert latest["component"] == "resolution_worker"
    assert latest["mode"] == "sandbox"
    assert latest["status"] == "tracking_positions"
    assert latest["status_counts"] == {"settled": 1}
    assert len(history) == 1


def test_main_once_live_prints_reference_only_note(monkeypatch, capsys, tmp_path):
    class FakeWorker:
        def scan_once(self):
            return []

    monkeypatch.setattr(run_resolution, "load_profile", lambda name: _profile(mode="live"))
    monkeypatch.setattr(
        run_resolution,
        "resolve_upcoming_window_metadata",
        lambda slug_pattern, **kwargs: _metadata(),
    )
    monkeypatch.setattr(run_resolution, "_build_worker", lambda **kwargs: FakeWorker())

    status_path = tmp_path / "status.json"

    run_resolution.main([
        "btc_updown_15m_live",
        "--once",
        "--status-path",
        str(status_path),
    ])

    out = capsys.readouterr().out
    assert "Live mode: startup window metadata is reference-only." in out
    assert "No Polymarket wallet positions found." in out
    latest = json.loads(status_path.read_text(encoding="utf-8"))
    assert latest["mode"] == "live"
    assert latest["status"] == "idle"
    assert latest["position_count"] == 0


def test_main_once_artifacts_dir_writes_default_worker_artifacts(monkeypatch, tmp_path):
    class FakeWorker:
        def scan_once(self):
            return [
                ResolutionScanResult(
                    condition_id="cond-1",
                    instrument_id="cond-1-yes-1.POLYMARKET",
                    token_id="yes-1",
                    position_size=2.5,
                    resolved=True,
                    settlement_price=1.0,
                    token_won=True,
                    status="settled",
                )
            ]

    monkeypatch.setattr(run_resolution, "load_profile", lambda name: _profile(mode="sandbox"))
    monkeypatch.setattr(
        run_resolution,
        "resolve_upcoming_window_metadata",
        lambda slug_pattern, **kwargs: _metadata(),
    )
    monkeypatch.setattr(run_resolution, "_build_worker", lambda **kwargs: FakeWorker())

    artifacts_dir = tmp_path / "worker_artifacts"

    run_resolution.main([
        "btc_updown_15m_sandbox",
        "--once",
        "--sandbox-wallet-state-path",
        "/tmp/wallet.json",
        "--artifacts-dir",
        str(artifacts_dir),
    ])

    log_text = (artifacts_dir / "worker.log").read_text(encoding="utf-8")
    status = json.loads((artifacts_dir / "worker_status.json").read_text(encoding="utf-8"))
    history = [
        json.loads(line)
        for line in (artifacts_dir / "worker_status_history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert "Sandbox mode: startup window metadata is authoritative." in log_text
    assert "cond-1-yes-1.POLYMARKET size=2.500000 status=settled settled=1.00" in log_text
    assert status["component"] == "resolution_worker"
    assert status["status_counts"] == {"settled": 1}
    assert len(history) == 1


def test_main_live_uses_polygon_rpc_url_from_env_when_cli_omitted(monkeypatch, tmp_path):
    class FakeWorker:
        def scan_once(self):
            return []

    calls = {}

    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.example")
    monkeypatch.setattr(run_resolution, "load_profile", lambda name: _profile(mode="live"))
    monkeypatch.setattr(
        run_resolution,
        "resolve_upcoming_window_metadata",
        lambda slug_pattern, **kwargs: _metadata(),
    )

    def fake_build_worker(**kwargs):
        calls.update(kwargs)
        return FakeWorker()

    monkeypatch.setattr(run_resolution, "_build_worker", fake_build_worker)

    run_resolution.main([
        "btc_updown_15m_live",
        "--once",
        "--status-path",
        str(tmp_path / "status.json"),
    ])

    assert calls["rpc_url"] == "https://rpc.example"


def test_main_continuous_survives_scan_exception_and_writes_error_status(
    monkeypatch,
    capsys,
    tmp_path,
):
    class StopLoop(Exception):
        pass

    class FakeWorker:
        def scan_once(self):
            raise RuntimeError("scan exploded")

    monkeypatch.setattr(run_resolution, "load_profile", lambda name: _profile(mode="live"))
    monkeypatch.setattr(
        run_resolution,
        "resolve_upcoming_window_metadata",
        lambda slug_pattern, **kwargs: _metadata(),
    )
    monkeypatch.setattr(run_resolution, "_build_worker", lambda **kwargs: FakeWorker())
    monkeypatch.setattr(run_resolution.time, "sleep", lambda secs: (_ for _ in ()).throw(StopLoop()))

    status_path = tmp_path / "status.json"

    with pytest.raises(StopLoop):
        run_resolution.main([
            "btc_updown_15m_live",
            "--status-path",
            str(status_path),
            "--interval-secs",
            "30",
        ])

    out = capsys.readouterr().out
    assert "Resolution worker scan error: RuntimeError: scan exploded" in out
    latest = json.loads(status_path.read_text(encoding="utf-8"))
    assert latest["status"] == "scan_error"
    assert latest["last_error"] == "RuntimeError: scan exploded"
    assert latest["position_count"] == 0


def test_build_worker_requires_sandbox_state_path():
    registry = WindowMetadataRegistry(_metadata())

    with pytest.raises(SystemExit, match="--sandbox-wallet-state-path is required"):
        run_resolution._build_worker(
            registry=registry,
            sandbox=True,
            sandbox_wallet_state_path=None,
            sandbox_starting_usdc=None,
            execute_redemptions=False,
            rpc_url="http://localhost:8545",
        )


def test_build_worker_sandbox_seeds_wallet_store_with_starting_balance(monkeypatch):
    registry = WindowMetadataRegistry(_metadata())
    seen = {}

    class FakeWalletStore:
        def __init__(self, *, wallet_address, collateral_balance, state_path):
            seen["wallet_address"] = wallet_address
            seen["collateral_balance"] = collateral_balance
            seen["state_path"] = state_path

    monkeypatch.setenv("POLYMARKET_TEST_WALLET_ADDRESS", "0xtest")
    monkeypatch.setattr(run_resolution, "SandboxWalletStore", FakeWalletStore)
    monkeypatch.setattr(
        run_resolution,
        "SandboxWalletTruthProvider",
        lambda wallet_store, registry: ("provider", wallet_store, registry),
    )
    monkeypatch.setattr(
        run_resolution,
        "SandboxResolutionExecutor",
        lambda wallet_store: ("executor", wallet_store),
    )
    monkeypatch.setattr(
        run_resolution,
        "ResolutionWorker",
        lambda registry, wallet_truth_provider, executor: ("worker", registry, wallet_truth_provider, executor),
    )

    worker = run_resolution._build_worker(
        registry=registry,
        sandbox=True,
        sandbox_wallet_state_path="/tmp/wallet.json",
        sandbox_starting_usdc=10.0,
        execute_redemptions=False,
        rpc_url="http://localhost:8545",
    )

    assert worker[0] == "worker"
    assert seen == {
        "wallet_address": "0xtest",
        "collateral_balance": 10.0,
        "state_path": "/tmp/wallet.json",
    }


def test_build_worker_live_uses_dry_run_until_execute_requested(monkeypatch):
    registry = WindowMetadataRegistry(_metadata())
    calls = {}
    fake_provider = object()
    fake_executor = object()
    fake_worker = object()

    monkeypatch.setattr(
        run_resolution,
        "make_polymarket_balance_client",
        lambda sandbox: (SimpleNamespace(), "0x0000000000000000000000000000000000000001"),
    )
    monkeypatch.setenv("PRIVATE_KEY", "0x" + ("11" * 32))
    monkeypatch.setattr(
        run_resolution,
        "ProdWalletTruthProvider",
        lambda wallet_address, balance_client, registry, restrict_to_registry: _capture_provider(
            calls, fake_provider, wallet_address, balance_client, registry, restrict_to_registry
        ),
    )

    def fake_executor_ctor(**kwargs):
        calls["executor"] = kwargs
        return fake_executor

    monkeypatch.setattr(run_resolution, "ProdRedemptionExecutor", fake_executor_ctor)
    monkeypatch.setattr(
        run_resolution,
        "ResolutionWorker",
        lambda registry, wallet_truth_provider, executor, restrict_to_registry: _capture_worker(
            calls, fake_worker, registry, wallet_truth_provider, executor, restrict_to_registry
        ),
    )

    worker = run_resolution._build_worker(
        registry=registry,
        sandbox=False,
        sandbox_wallet_state_path=None,
        sandbox_starting_usdc=None,
        execute_redemptions=False,
        rpc_url="http://localhost:8545",
    )

    assert calls["executor"]["dry_run"] is True
    assert calls["executor"]["wallet_address"] == "0x0000000000000000000000000000000000000001"
    assert calls["provider"][3] is False
    assert calls["worker_args"][3] is False
    assert worker is fake_worker


def _capture_provider(calls, fake_provider, wallet_address, balance_client, registry, restrict_to_registry):
    calls["provider"] = (wallet_address, balance_client, registry, restrict_to_registry)
    return fake_provider


def _capture_worker(calls, fake_worker, registry, wallet_truth_provider, executor, restrict_to_registry):
    calls["worker_args"] = (registry, wallet_truth_provider, executor, restrict_to_registry)
    return fake_worker
