#!/usr/bin/env python3
"""Run the external wallet-based resolution worker for a live profile."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(PROJECT_ROOT))

from live.env import add_env_file_arg, bootstrap_env_file, load_project_env
from live.market_metadata import WindowMetadataRegistry
from live.node import resolve_upcoming_window_metadata
from live.profiles import ProfileError, available_profile_names, load_profile
from live.redemption import DEFAULT_POLYGON_RPC_URL, ProdRedemptionExecutor
from live.resolution_worker import ResolutionWorker, SandboxResolutionExecutor
from live.status_artifacts import StatusArtifactWriter
from live.sandbox_wallet import SandboxWalletStore, SandboxWalletTruthProvider
from live.wallet_truth import ProdWalletTruthProvider, make_polymarket_balance_client

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass


def main(argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_arg_parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in available_profile_names():
            print(name, flush=True)
        return

    if not args.profile:
        parser.error("profile is required unless --list is used")
    if not args.once and args.interval_secs <= 0:
        parser.error("--interval-secs must be positive")

    try:
        profile = load_profile(args.profile)
        if args.hours_ahead is not None:
            profile = profile.with_hours_ahead(args.hours_ahead)
    except ProfileError as exc:
        raise SystemExit(str(exc)) from exc

    metadata = resolve_upcoming_window_metadata(
        profile.slug_pattern,
        hours_ahead=profile.hours_ahead,
        outcome_side=profile.outcome_side,
    )
    if not metadata:
        raise SystemExit("No window metadata resolved for the requested profile")

    registry = WindowMetadataRegistry(metadata)
    _print_scope_note(
        sandbox=profile.sandbox,
        slug_pattern=profile.slug_pattern,
        hours_ahead=profile.hours_ahead,
    )
    effective_sandbox_starting_usdc = (
        profile.sandbox_starting_usdc
        if args.sandbox_starting_usdc is None
        else args.sandbox_starting_usdc
    )
    worker = _build_worker(
        registry=registry,
        sandbox=profile.sandbox,
        sandbox_wallet_state_path=args.sandbox_wallet_state_path,
        sandbox_starting_usdc=effective_sandbox_starting_usdc,
        execute_redemptions=args.execute_redemptions,
        rpc_url=args.rpc_url,
    )
    status_writer = _build_status_writer(
        status_path=args.status_path,
        status_history_path=args.status_history_path,
    )

    while True:
        scanned_at = datetime.now(tz=timezone.utc)
        results = worker.scan_once()
        _print_scan(results, sandbox=profile.sandbox)
        _write_status_snapshot(
            writer=status_writer,
            profile=profile,
            scanned_at=scanned_at,
            execute_redemptions=args.execute_redemptions,
            results=results,
        )
        if args.once:
            return
        time.sleep(args.interval_secs)


def _make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the external Polymarket resolution worker")
    add_env_file_arg(parser)
    parser.add_argument("profile", nargs="?", help="Profile name or path to a TOML file")
    parser.add_argument("--list", action="store_true", help="List available checked-in profiles and exit")
    parser.add_argument("--hours-ahead", type=int, default=None,
                        help="Override profile window preload horizon in hours")
    parser.add_argument("--sandbox-wallet-state-path", default=None,
                        help="Shared sandbox wallet-state JSON file (required for sandbox mode)")
    parser.add_argument("--sandbox-starting-usdc", type=float, default=None,
                        help="Seed sandbox wallet truth with this starting USDC.e balance when creating the shared state file")
    parser.add_argument("--once", action="store_true",
                        help="Run one resolution scan and exit")
    parser.add_argument("--interval-secs", type=int, default=30,
                        help="Polling interval when running continuously (default: 30)")
    parser.add_argument("--execute-redemptions", action="store_true",
                        help="In live mode, actually submit redemption transactions instead of dry-run summaries")
    parser.add_argument("--rpc-url", default=DEFAULT_POLYGON_RPC_URL,
                        help=f"Polygon RPC URL for live redemptions (default: {DEFAULT_POLYGON_RPC_URL})")
    parser.add_argument("--status-path", default=None,
                        help="Optional path for the latest machine-readable worker status JSON")
    parser.add_argument("--status-history-path", default=None,
                        help="Optional path for append-only machine-readable worker status history JSONL")
    return parser


def _build_worker(
    *,
    registry: WindowMetadataRegistry,
    sandbox: bool,
    sandbox_wallet_state_path: str | None,
    sandbox_starting_usdc: float | None,
    execute_redemptions: bool,
    rpc_url: str,
) -> ResolutionWorker:
    if sandbox:
        if not sandbox_wallet_state_path:
            raise SystemExit("--sandbox-wallet-state-path is required for sandbox resolution runs")
        wallet_store = SandboxWalletStore(
            wallet_address=os.environ["POLYMARKET_TEST_WALLET_ADDRESS"],
            collateral_balance=0.0 if sandbox_starting_usdc is None else sandbox_starting_usdc,
            state_path=sandbox_wallet_state_path,
        )
        provider = SandboxWalletTruthProvider(wallet_store=wallet_store, registry=registry)
        executor = SandboxResolutionExecutor(wallet_store)
        return ResolutionWorker(
            registry=registry,
            wallet_truth_provider=provider,
            executor=executor,
        )

    balance_client, wallet_address = make_polymarket_balance_client(sandbox=False)
    provider = ProdWalletTruthProvider(
        wallet_address=wallet_address,
        balance_client=balance_client,
        registry=registry,
        restrict_to_registry=False,
    )
    executor = ProdRedemptionExecutor(
        private_key=os.environ["PRIVATE_KEY"],
        wallet_address=wallet_address,
        rpc_url=rpc_url,
        dry_run=not execute_redemptions,
    )
    return ResolutionWorker(
        registry=registry,
        wallet_truth_provider=provider,
        executor=executor,
        restrict_to_registry=False,
    )


def _print_scope_note(*, sandbox: bool, slug_pattern: str, hours_ahead: int) -> None:
    if sandbox:
        print(
            "Sandbox mode: startup window metadata is authoritative. "
            "Synthetic sandbox wallet positions must map through this registry "
            f"for '{slug_pattern}' ({hours_ahead}h horizon).",
            flush=True,
        )
        return

    print(
        "Live mode: startup window metadata is reference-only. "
        "Resolution scans actual Polymarket wallet positions and market truth, "
        "not just the preloaded window horizon.",
        flush=True,
    )


def _print_scan(results, *, sandbox: bool) -> None:
    if not results:
        if sandbox:
            print("No allowlisted wallet positions found.", flush=True)
        else:
            print("No Polymarket wallet positions found.", flush=True)
        return

    for result in results:
        settlement = "n/a" if result.settlement_price is None else f"{result.settlement_price:.2f}"
        tx = "" if result.transaction_hash is None else f" tx={result.transaction_hash}"
        print(
            f"{result.instrument_id} size={result.position_size:.6f} "
            f"status={result.status} settled={settlement}{tx}",
            flush=True,
        )


def _build_status_writer(
    *,
    status_path: str | None,
    status_history_path: str | None,
) -> StatusArtifactWriter | None:
    if status_path is None and status_history_path is None:
        return None
    latest_path = (
        Path(status_path).expanduser().resolve()
        if status_path is not None
        else Path(status_history_path).expanduser().resolve().with_suffix(".json")
    )
    history_path = (
        None
        if status_history_path is None
        else Path(status_history_path).expanduser().resolve()
    )
    return StatusArtifactWriter(latest_path=latest_path, history_path=history_path)


def _write_status_snapshot(
    *,
    writer: StatusArtifactWriter | None,
    profile,
    scanned_at: datetime,
    execute_redemptions: bool,
    results: list,
) -> None:
    if writer is None:
        return

    counts = Counter(result.status for result in results)
    payload = {
        "recorded_at": scanned_at,
        "component": "resolution_worker",
        "mode": "sandbox" if profile.sandbox else "live",
        "profile_name": profile.name,
        "slug_pattern": profile.slug_pattern,
        "hours_ahead": profile.hours_ahead,
        "execute_redemptions": execute_redemptions,
        "status": "idle" if not results else "tracking_positions",
        "position_count": len(results),
        "status_counts": dict(sorted(counts.items())),
        "results": [
            {
                "condition_id": result.condition_id,
                "instrument_id": result.instrument_id,
                "token_id": result.token_id,
                "position_size": result.position_size,
                "resolved": result.resolved,
                "settlement_price": result.settlement_price,
                "token_won": result.token_won,
                "status": result.status,
                "transaction_hash": result.transaction_hash,
            }
            for result in results
        ],
    }
    writer.write(payload)


if __name__ == "__main__":
    main()
