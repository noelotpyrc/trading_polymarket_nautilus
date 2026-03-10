#!/usr/bin/env python3
"""Benchmark Binance historical warmup through Nautilus `request_bars(...)`.

Measures how long a live Nautilus actor takes to request and process a warmup
window of Binance 1-minute bars through the Binance live data client. It can
also validate the warmup/live handoff by waiting for the first live bar after
the historical request completes and computing the boundary signal window.

Usage:
    python tests/live/benchmark_binance_warmup.py
    python tests/live/benchmark_binance_warmup.py --days 14
    python tests/live/benchmark_binance_warmup.py --days 30 --timeout-secs 300
    python tests/live/benchmark_binance_warmup.py --days 14 --verify-handoff
"""
import argparse
import os
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, InstrumentProviderConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import Bar, BarType

from live.strategies.btc_updown import compute_signal

INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
BAR_TYPE = "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"
DONE_EVENT = threading.Event()


class BinanceWarmupBenchmarkConfig(ActorConfig, frozen=True):
    start_iso: str
    end_iso: str
    bar_type: str = BAR_TYPE
    progress_every: int = 5000
    verify_handoff: bool = False
    signal_lookback: int = 5


class BinanceWarmupBenchmark(Actor):
    def __init__(self, config: BinanceWarmupBenchmarkConfig):
        super().__init__(config)
        self._bar_type = BarType.from_str(config.bar_type)
        self._range_start = datetime.fromisoformat(config.start_iso)
        self._range_end = datetime.fromisoformat(config.end_iso)
        self._progress_every = config.progress_every
        self._verify_handoff = config.verify_handoff
        self._signal_lookback = config.signal_lookback
        self._signal_window: deque[tuple[int, float, str]] = deque(maxlen=config.signal_lookback + 1)
        self._historical_closes: dict[int, float] = {}
        self._live_buffer: dict[int, float] = {}

        self.request_started_monotonic: float | None = None
        self.first_bar_monotonic: float | None = None
        self.completed_monotonic: float | None = None
        self.bar_count = 0
        self.first_bar_ts_ns: int | None = None
        self.last_bar_ts_ns: int | None = None
        self.request_complete = False
        self.last_merged_ts_ns: int | None = None
        self.first_live_after_warmup_ts_ns: int | None = None
        self.boundary_signal: int | None = None
        self.boundary_window: list[tuple[int, float, str]] = []
        self.handoff_verified = False

    def on_start(self):
        if self._verify_handoff:
            self.subscribe_bars(self._bar_type)
        self.request_started_monotonic = time.monotonic()
        self.log.info(
            f"Requesting historical warmup: {self._bar_type} "
            f"from {self._range_start.isoformat()} to {self._range_end.isoformat()}"
        )
        self.request_bars(
            self._bar_type,
            start=self._range_start,
            end=self._range_end,
            callback=self._on_request_complete,
        )

    def on_historical_data(self, data):
        if not isinstance(data, Bar):
            return

        if data.bar_type != self._bar_type:
            return

        self.bar_count += 1
        self._historical_closes[data.ts_event] = float(data.close)
        if self.first_bar_monotonic is None:
            self.first_bar_monotonic = time.monotonic()
            self.first_bar_ts_ns = data.ts_event
        self.last_bar_ts_ns = data.ts_event

        if self._progress_every and self.bar_count % self._progress_every == 0:
            elapsed = time.monotonic() - self.request_started_monotonic
            self.log.info(f"Historical bars received: {self.bar_count} ({elapsed:.2f}s)")

    def on_bar(self, bar: Bar):
        if bar.bar_type != self._bar_type:
            return

        close = float(bar.close)
        if not self.request_complete:
            self._live_buffer[bar.ts_event] = close
            return

        if self.last_merged_ts_ns is None or bar.ts_event <= self.last_merged_ts_ns:
            return

        self.first_live_after_warmup_ts_ns = bar.ts_event
        self._signal_window.append((bar.ts_event, close, "live"))
        self.boundary_window = list(self._signal_window)
        self.boundary_signal = compute_signal([entry[1] for entry in self.boundary_window])
        self.handoff_verified = (
            len(self.boundary_window) == self._signal_lookback + 1
            and any(source != "live" for _, _, source in self.boundary_window[:-1])
            and self.boundary_window[-1][2] == "live"
        )
        self.log.info(
            f"Warmup/live handoff: ts={bar.ts_event} "
            f"signal={self.boundary_signal} "
            f"window={[(ts, close, source) for ts, close, source in self.boundary_window]}"
        )
        DONE_EVENT.set()

    def _on_request_complete(self, request_id) -> None:
        self.completed_monotonic = time.monotonic()
        self.request_complete = True
        self.log.info(
            f"Historical request complete: bars={self.bar_count} request_id={request_id}"
        )
        if not self._verify_handoff:
            DONE_EVENT.set()
            return

        merged = dict(self._historical_closes)
        merged.update(self._live_buffer)

        for ts_ns, close in sorted(merged.items()):
            source = "buffered_live" if ts_ns in self._live_buffer else "historical"
            self._signal_window.append((ts_ns, close, source))
            self.last_merged_ts_ns = ts_ns

        self.log.info(
            f"Prepared handoff window: last_merged_ts={self.last_merged_ts_ns} "
            f"window={[(ts, close, source) for ts, close, source in self._signal_window]}"
        )

    def on_stop(self):
        if self._verify_handoff:
            self.unsubscribe_bars(self._bar_type)


def _fmt_bar_ts(ts_ns: int | None) -> str:
    if ts_ns is None:
        return "(none)"
    return datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).isoformat()


def _fmt_signal(signal: int | None) -> str:
    return {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL", None: "(none)"}.get(signal, "(none)")


def main():
    parser = argparse.ArgumentParser(description="Benchmark Binance historical warmup")
    parser.add_argument("--days", type=int, default=14, help="Warmup lookback in days")
    parser.add_argument(
        "--timeout-secs",
        type=int,
        default=180,
        help="Hard timeout for the benchmark run",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=5000,
        help="Log progress every N bars (default: 5000)",
    )
    parser.add_argument(
        "--verify-handoff",
        action="store_true",
        help="Wait for the first post-warmup live bar and report the merged signal window",
    )
    parser.add_argument(
        "--signal-lookback",
        type=int,
        default=5,
        help="Signal lookback to use for handoff verification (default: 5)",
    )
    args = parser.parse_args()

    if args.days <= 0:
        raise SystemExit("--days must be a positive integer")
    if args.timeout_secs <= 0:
        raise SystemExit("--timeout-secs must be a positive integer")
    if args.signal_lookback <= 0:
        raise SystemExit("--signal-lookback must be a positive integer")

    DONE_EVENT.clear()

    end = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)

    print("=== Binance Warmup Benchmark ===")
    print(f"Instrument      : {INSTRUMENT_ID}")
    print(f"Bar type        : {BAR_TYPE}")
    print(f"Warmup days     : {args.days}")
    print(f"Range start     : {start.isoformat()}")
    print(f"Range end       : {end.isoformat()}")
    print(f"Timeout         : {args.timeout_secs}s")
    print(f"Verify handoff  : {'yes' if args.verify_handoff else 'no'}")
    if args.verify_handoff:
        print(f"Signal lookback : {args.signal_lookback}")
    print()

    node = TradingNode(
        config=TradingNodeConfig(
            data_clients={
                "BINANCE": BinanceDataClientConfig(
                    account_type=BinanceAccountType.USDT_FUTURES,
                    instrument_provider=InstrumentProviderConfig(
                        load_ids=frozenset([INSTRUMENT_ID]),
                    ),
                ),
            },
            exec_clients={},
        )
    )
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)

    actor = BinanceWarmupBenchmark(
        BinanceWarmupBenchmarkConfig(
            start_iso=start.isoformat(),
            end_iso=end.isoformat(),
            progress_every=args.progress_every,
            verify_handoff=args.verify_handoff,
            signal_lookback=args.signal_lookback,
        )
    )
    node.trader.add_actor(actor)
    node.build()

    def _stop_when_done() -> None:
        DONE_EVENT.wait()
        node.stop()

    threading.Thread(target=_stop_when_done, daemon=True).start()
    threading.Timer(args.timeout_secs, node.stop).start()

    wall_started = time.monotonic()
    node.run()
    wall_elapsed = time.monotonic() - wall_started

    request_elapsed = None
    first_bar_elapsed = None
    if actor.request_started_monotonic is not None and actor.completed_monotonic is not None:
        request_elapsed = actor.completed_monotonic - actor.request_started_monotonic
    if actor.request_started_monotonic is not None and actor.first_bar_monotonic is not None:
        first_bar_elapsed = actor.first_bar_monotonic - actor.request_started_monotonic

    print()
    print("=== Benchmark Result ===")
    print(f"Bars received    : {actor.bar_count}")
    print(f"First bar ts     : {_fmt_bar_ts(actor.first_bar_ts_ns)}")
    print(f"Last bar ts      : {_fmt_bar_ts(actor.last_bar_ts_ns)}")
    print(f"Time to first bar: {first_bar_elapsed:.2f}s" if first_bar_elapsed is not None else "Time to first bar: (none)")
    print(f"Request complete : {request_elapsed:.2f}s" if request_elapsed is not None else "Request complete : (timed out / incomplete)")
    print(f"Total wall time  : {wall_elapsed:.2f}s")
    if args.verify_handoff:
        print(f"First live after : {_fmt_bar_ts(actor.first_live_after_warmup_ts_ns)}")
        print(f"Handoff verified : {'yes' if actor.handoff_verified else 'no'}")
        print(f"Boundary signal  : {_fmt_signal(actor.boundary_signal)}")
        print(f"Boundary window  : {actor.boundary_window}")

        if not actor.handoff_verified:
            raise SystemExit("Warmup/live handoff was not verified before timeout")


if __name__ == "__main__":
    main()
