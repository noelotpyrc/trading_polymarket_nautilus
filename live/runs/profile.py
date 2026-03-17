#!/usr/bin/env python3
"""Run a checked-in live runner profile."""
import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from live.env import add_env_file_arg, bootstrap_env_file, load_project_env
from live.profiles import ProfileError, RunnerProfile, available_profile_names, load_profile
from live.runs.common import run_strategy, validate_strategy_config

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()


def run_profile(
    profile: RunnerProfile,
    *,
    sandbox_wallet_state_path: str | None = None,
    sandbox_starting_usdc: float | None = None,
) -> None:
    validate_strategy_config(profile.strategy, profile.strategy_config)
    effective_sandbox_starting_usdc = (
        profile.sandbox_starting_usdc
        if sandbox_starting_usdc is None
        else sandbox_starting_usdc
    )
    run_kwargs = {
        "strategy_name": profile.strategy,
        "slug_pattern": profile.slug_pattern,
        "hours_ahead": profile.hours_ahead,
        "outcome_side": profile.outcome_side,
        "sandbox": profile.sandbox,
        "binance_us": profile.binance_us,
        "run_secs": profile.run_secs,
        "strategy_config": profile.strategy_config,
    }
    if effective_sandbox_starting_usdc is not None:
        run_kwargs["sandbox_starting_usdc"] = effective_sandbox_starting_usdc
    if sandbox_wallet_state_path is not None:
        run_kwargs["sandbox_wallet_state_path"] = sandbox_wallet_state_path
    run_strategy(**run_kwargs)


def main(argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_profile_arg_parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in available_profile_names():
            print(name)
        return

    if not args.profile:
        parser.error("profile is required unless --list is used")

    main_for_profile(args.profile, argv=_fixed_profile_argv(args))


def main_for_profile(profile_name: str, argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_fixed_profile_arg_parser(profile_name)
    args = parser.parse_args(argv)

    try:
        profile = load_profile(profile_name)
        if args.hours_ahead is not None:
            profile = profile.with_hours_ahead(args.hours_ahead)
        if args.run_secs is not None:
            profile = profile.with_run_secs(args.run_secs)
        if args.sandbox_starting_usdc is not None:
            profile = profile.with_sandbox_starting_usdc(args.sandbox_starting_usdc)
        validate_strategy_config(profile.strategy, profile.strategy_config)
        if args.print_profile:
            print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
            return
        run_profile(
            profile,
            sandbox_wallet_state_path=args.sandbox_wallet_state_path,
        )
    except (ProfileError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _make_profile_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a checked-in live runner profile")
    add_env_file_arg(parser)
    parser.add_argument("profile", nargs="?", help="Profile name or path to a TOML file")
    parser.add_argument("--list", action="store_true", help="List available checked-in profiles and exit")
    parser.add_argument("--hours-ahead", type=int, default=None,
                        help="Override profile window preload horizon in hours")
    parser.add_argument("--run-secs", type=int, default=None,
                        help="Override profile runtime for a bounded manual run")
    parser.add_argument("--sandbox-wallet-state-path", default=None,
                        help="Optional shared sandbox wallet-state JSON file for resolution tests")
    parser.add_argument("--sandbox-starting-usdc", type=float, default=None,
                        help="Override sandbox starting USDC.e balance for simulated execution")
    parser.add_argument("--print-profile", action="store_true",
                        help="Print the resolved profile JSON and exit")
    return parser


def _make_fixed_profile_arg_parser(profile_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Run live profile {profile_name}")
    add_env_file_arg(parser)
    parser.add_argument("--hours-ahead", type=int, default=None,
                        help="Override profile window preload horizon in hours")
    parser.add_argument("--run-secs", type=int, default=None,
                        help="Override profile runtime for a bounded manual run")
    parser.add_argument("--sandbox-wallet-state-path", default=None,
                        help="Optional shared sandbox wallet-state JSON file for resolution tests")
    parser.add_argument("--sandbox-starting-usdc", type=float, default=None,
                        help="Override sandbox starting USDC.e balance for simulated execution")
    parser.add_argument("--print-profile", action="store_true",
                        help="Print the resolved profile JSON and exit")
    return parser


def _fixed_profile_argv(args) -> list[str]:
    argv: list[str] = []
    if args.env_file is not None:
        argv.extend(["--env-file", args.env_file])
    if args.hours_ahead is not None:
        argv.extend(["--hours-ahead", str(args.hours_ahead)])
    if args.run_secs is not None:
        argv.extend(["--run-secs", str(args.run_secs)])
    if args.sandbox_wallet_state_path is not None:
        argv.extend(["--sandbox-wallet-state-path", args.sandbox_wallet_state_path])
    if args.sandbox_starting_usdc is not None:
        argv.extend(["--sandbox-starting-usdc", str(args.sandbox_starting_usdc)])
    if args.print_profile:
        argv.append("--print-profile")
    return argv


if __name__ == "__main__":
    main()
