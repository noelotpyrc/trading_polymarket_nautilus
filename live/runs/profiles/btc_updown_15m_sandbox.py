#!/usr/bin/env python3
"""Run the fixed sandbox BTC up/down 15m profile."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from live.runs.profile import main_for_profile


def main(argv: list[str] | None = None) -> None:
    main_for_profile("btc_updown_15m_sandbox", argv=argv)


if __name__ == "__main__":
    main()
