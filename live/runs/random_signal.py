#!/usr/bin/env python3
"""Run the random infrastructure test strategy with ad hoc CLI settings."""
import os
import sys

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from live.node import make_arg_parser
from live.runs.common import run_strategy

load_dotenv()


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
    strategy_config = {}
    if args.entry_threshold is not None:
        strategy_config["entry_threshold"] = args.entry_threshold
    if args.exit_threshold is not None:
        strategy_config["exit_threshold"] = args.exit_threshold
    if args.trade_amount_usdc is not None:
        strategy_config["trade_amount_usdc"] = args.trade_amount_usdc
    if args.disable_signal_exit:
        strategy_config["disable_signal_exit"] = True
    if args.carry_window_end_position:
        strategy_config["carry_window_end_position"] = True
    if strategy_config:
        kwargs["strategy_config"] = strategy_config
    sandbox_wallet_state_path = getattr(args, "sandbox_wallet_state_path", None)
    if sandbox_wallet_state_path is not None:
        kwargs["sandbox_wallet_state_path"] = sandbox_wallet_state_path
    run_strategy("random_signal", **kwargs)


def main(argv: list[str] | None = None) -> None:
    parser = make_arg_parser("Random signal test strategy")
    parser.add_argument("--entry-threshold", type=float, default=None,
                        help="Override random entry threshold for ad hoc runs")
    parser.add_argument("--exit-threshold", type=float, default=None,
                        help="Override random exit threshold for ad hoc runs")
    parser.add_argument("--trade-amount-usdc", type=float, default=None,
                        help="Override random-signal trade amount in USDC for ad hoc runs")
    parser.add_argument("--disable-signal-exit", action="store_true",
                        help="Disable signal-driven exits (sandbox residual testing)")
    parser.add_argument("--carry-window-end-position", action="store_true",
                        help="Carry the current window position to resolution at window end")
    args = parser.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
