#!/usr/bin/env python3
"""Run standalone market-data capture for PM YES/NO and Binance quotes."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(PROJECT_ROOT))

from live.market_data_capture import (
    MarketDataCaptureRecorder,
    default_binance_stream_url,
)
from live.node import resolve_upcoming_window_metadata

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass


class _TeeTextIO:
    def __init__(self, primary, secondary) -> None:
        self._primary = primary
        self._secondary = secondary
        self.encoding = getattr(primary, "encoding", "utf-8")
        self.errors = getattr(primary, "errors", "strict")

    def write(self, data: str) -> int:
        self._primary.write(data)
        self._secondary.write(data)
        return len(data)

    def flush(self) -> None:
        self._primary.flush()
        self._secondary.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._primary, "isatty", lambda: False)())

    def writable(self) -> bool:
        return True

    def __getattr__(self, name: str):
        return getattr(self._primary, name)


def main(argv: list[str] | None = None) -> None:
    parser = _make_arg_parser()
    args = parser.parse_args(argv)

    if args.sample_interval_secs <= 0:
        parser.error("--sample-interval-secs must be positive")

    artifact_paths = _resolve_artifact_paths(
        artifacts_dir=args.artifacts_dir,
        samples_path=args.samples_path,
    )
    restore_streams = _install_capture_log(artifact_paths["log_path"])

    try:
        metadata = _resolve_windows(
            slug_pattern=args.slug_pattern,
            hours_ahead=args.hours_ahead,
        )
        if not metadata:
            raise SystemExit("No window metadata resolved for the requested market scope")

        binance_symbol = args.binance_symbol
        try:
            binance_ws_url = (
                args.binance_stream_url
                or default_binance_stream_url(binance_symbol, us=args.binance_us)
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

        print(
            "Market-data capture starting | "
            f"slug={args.slug_pattern} "
            f"hours_ahead={args.hours_ahead} "
            f"sample_interval={args.sample_interval_secs}s "
            f"binance_symbol={binance_symbol}",
            flush=True,
        )
        print(
            f"Artifacts | samples={artifact_paths['samples_path']} "
            f"log={artifact_paths['log_path'] or 'stdout only'}",
            flush=True,
        )

        recorder = MarketDataCaptureRecorder(
            windows=metadata,
            binance_symbol=binance_symbol,
            sample_interval_secs=args.sample_interval_secs,
            samples_path=Path(artifact_paths["samples_path"]).expanduser().resolve(),
            binance_ws_url=binance_ws_url,
            refresh_windows=lambda: _resolve_windows(
                slug_pattern=args.slug_pattern,
                hours_ahead=args.hours_ahead,
            ),
        )
        asyncio.run(recorder.run(once=args.once))
    finally:
        _restore_capture_log(restore_streams)


def _make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture PM YES/NO quotes plus Binance real-time quotes in a separate process"
    )
    parser.add_argument("--slug-pattern", required=True,
                        help="Market slug pattern, e.g. btc-updown-15m")
    parser.add_argument("--hours-ahead", type=int, required=True,
                        help="Hours of windows to resolve ahead for capture")
    parser.add_argument("--binance-symbol", default="BTCUSDT",
                        help="Binance symbol to sample, default: BTCUSDT")
    parser.add_argument("--binance-us", action="store_true",
                        help="Use Binance US routing; requires --binance-stream-url for now")
    parser.add_argument("--once", action="store_true",
                        help="Write one sample once an active window is available, then exit")
    parser.add_argument("--sample-interval-secs", type=float, default=5.0,
                        help="Sampling cadence in seconds (default: 5)")
    parser.add_argument("--samples-path", default=None,
                        help="Optional output path for append-only capture samples JSONL")
    parser.add_argument("--artifacts-dir", default=None,
                        help="Optional directory for market_data.log and market_data_samples.jsonl")
    parser.add_argument("--binance-stream-url", default=None,
                        help="Optional full Binance websocket URL override")
    return parser


def _resolve_windows(*, slug_pattern: str, hours_ahead: int) -> list:
    return resolve_upcoming_window_metadata(
        slug_pattern,
        hours_ahead=hours_ahead,
        outcome_side="yes",
    )


def _resolve_artifact_paths(
    *,
    artifacts_dir: str | None,
    samples_path: str | None,
) -> dict[str, str | None]:
    if artifacts_dir is None:
        if samples_path is None:
            raise SystemExit("Provide --artifacts-dir or --samples-path for market-data capture output")
        return {
            "log_path": None,
            "samples_path": str(Path(samples_path).expanduser().resolve()),
        }

    artifacts_path = Path(artifacts_dir).expanduser().resolve()
    artifacts_path.mkdir(parents=True, exist_ok=True)
    return {
        "log_path": str(artifacts_path / "market_data.log"),
        "samples_path": samples_path or str(artifacts_path / "market_data_samples.jsonl"),
    }


def _install_capture_log(log_path: str | None) -> tuple[object, object, object] | None:
    if log_path is None:
        return None
    log_file = Path(log_path).expanduser().resolve().open("a", encoding="utf-8", buffering=1)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _TeeTextIO(original_stdout, log_file)
    sys.stderr = _TeeTextIO(original_stderr, log_file)
    return log_file, original_stdout, original_stderr


def _restore_capture_log(state: tuple[object, object, object] | None) -> None:
    if state is None:
        return
    log_file, original_stdout, original_stderr = state
    sys.stdout.flush()
    sys.stderr.flush()
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    log_file.close()


if __name__ == "__main__":
    main()
