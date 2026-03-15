#!/usr/bin/env python3
"""Run the fixed sandbox random-signal Stage 8 resolution profile."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from live.runs.profile import main_for_profile


def main(argv: list[str] | None = None) -> None:
    main_for_profile("random_signal_15m_resolution_sandbox", argv=argv)


if __name__ == "__main__":
    main()
