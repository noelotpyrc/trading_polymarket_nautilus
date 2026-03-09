#!/usr/bin/env python3
"""Run the BTC up/down infrastructure test strategy with live data feeds."""
import os
import sys

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from live.node import build_node, make_arg_parser, prepare_run, schedule_stop
from live.strategies.btc_updown import BtcUpDownConfig, BtcUpDownStrategy

load_dotenv()


def run(args) -> None:
    windows = prepare_run(
        slug_pattern=args.slug_pattern,
        hours_ahead=args.hours_ahead,
        sandbox=args.sandbox,
        binance_us=args.binance_us,
        run_secs=args.run_secs,
    )
    pm_ids = [window[0] for window in windows]
    end_times = [window[1] for window in windows]

    node = build_node(pm_ids, sandbox=args.sandbox, binance_us=args.binance_us)
    node.trader.add_strategy(
        BtcUpDownStrategy(
            BtcUpDownConfig(
                pm_instrument_ids=tuple(pm_ids),
                window_end_times_ns=tuple(end_times),
            )
        )
    )
    node.build()
    schedule_stop(node, args.run_secs)
    node.run()


def main(argv: list[str] | None = None) -> None:
    parser = make_arg_parser("BTC up/down momentum strategy")
    args = parser.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
