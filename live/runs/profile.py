#!/usr/bin/env python3
"""Run a checked-in live runner profile."""
import argparse
import json
import os
import sys

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from live.profiles import ProfileError, RunnerProfile, available_profile_names, load_profile
from live.runs.common import run_strategy, validate_strategy_config

load_dotenv()


def run_profile(profile: RunnerProfile) -> None:
    validate_strategy_config(profile.strategy, profile.strategy_config)
    run_strategy(
        profile.strategy,
        slug_pattern=profile.slug_pattern,
        hours_ahead=profile.hours_ahead,
        sandbox=profile.sandbox,
        binance_us=profile.binance_us,
        run_secs=profile.run_secs,
        strategy_config=profile.strategy_config,
    )


def main(argv: list[str] | None = None) -> None:
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
    parser = _make_fixed_profile_arg_parser(profile_name)
    args = parser.parse_args(argv)

    try:
        profile = load_profile(profile_name)
        if args.run_secs is not None:
            profile = profile.with_run_secs(args.run_secs)
        validate_strategy_config(profile.strategy, profile.strategy_config)
        if args.print_profile:
            print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
            return
        run_profile(profile)
    except (ProfileError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _make_profile_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a checked-in live runner profile")
    parser.add_argument("profile", nargs="?", help="Profile name or path to a TOML file")
    parser.add_argument("--list", action="store_true", help="List available checked-in profiles and exit")
    parser.add_argument("--run-secs", type=int, default=None,
                        help="Override profile runtime for a bounded manual run")
    parser.add_argument("--print-profile", action="store_true",
                        help="Print the resolved profile JSON and exit")
    return parser


def _make_fixed_profile_arg_parser(profile_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Run live profile {profile_name}")
    parser.add_argument("--run-secs", type=int, default=None,
                        help="Override profile runtime for a bounded manual run")
    parser.add_argument("--print-profile", action="store_true",
                        help="Print the resolved profile JSON and exit")
    return parser


def _fixed_profile_argv(args) -> list[str]:
    argv: list[str] = []
    if args.run_secs is not None:
        argv.extend(["--run-secs", str(args.run_secs)])
    if args.print_profile:
        argv.append("--print-profile")
    return argv


if __name__ == "__main__":
    main()
