#!/usr/bin/env python3
"""Run the BTC up/down infrastructure test strategy with ad hoc CLI settings."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from live.env import bootstrap_env_file, load_project_env
from live.node import make_arg_parser
from live.runs.common import run_strategy

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()


def run(args) -> None:
    kwargs = {
        "slug_pattern": args.slug_pattern,
        "hours_ahead": args.hours_ahead,
        "outcome_side": args.outcome_side,
        "sandbox": args.sandbox,
        "binance_us": args.binance_us,
        "run_secs": args.run_secs,
        "sandbox_starting_usdc": args.sandbox_starting_usdc,
    }
    sandbox_wallet_state_path = getattr(args, "sandbox_wallet_state_path", None)
    if sandbox_wallet_state_path is not None:
        kwargs["sandbox_wallet_state_path"] = sandbox_wallet_state_path
    run_strategy("btc_updown", **kwargs)


def main(argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = make_arg_parser("BTC up/down momentum strategy")
    args = parser.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
