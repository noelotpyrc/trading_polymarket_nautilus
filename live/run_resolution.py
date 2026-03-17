#!/usr/bin/env python3
"""Run the external wallet-based resolution worker for a live profile."""
from __future__ import annotations

import argparse
import os
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
from live.sandbox_wallet import SandboxWalletStore, SandboxWalletTruthProvider
from live.wallet_truth import ProdWalletTruthProvider, make_polymarket_balance_client

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()


def main(argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_arg_parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in available_profile_names():
            print(name)
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

    while True:
        results = worker.scan_once()
        _print_scan(results)
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
    )


def _print_scan(results) -> None:
    if not results:
        print("No allowlisted wallet positions found.")
        return

    for result in results:
        settlement = "n/a" if result.settlement_price is None else f"{result.settlement_price:.2f}"
        tx = "" if result.transaction_hash is None else f" tx={result.transaction_hash}"
        print(
            f"{result.instrument_id} size={result.position_size:.6f} "
            f"status={result.status} settled={settlement}{tx}"
        )


if __name__ == "__main__":
    main()
