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
    run_strategy(
        "random_signal",
        slug_pattern=args.slug_pattern,
        hours_ahead=args.hours_ahead,
        outcome_side=args.outcome_side,
        sandbox=args.sandbox,
        binance_us=args.binance_us,
        run_secs=args.run_secs,
    )


def main(argv: list[str] | None = None) -> None:
    parser = make_arg_parser("Random signal test strategy")
    args = parser.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
