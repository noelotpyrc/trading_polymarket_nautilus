#!/usr/bin/env python3
"""
Shared infrastructure for live trading nodes.

Importable helpers for use by any strategy runner:
  - resolve_upcoming_window_metadata(slug_pattern, hours_ahead, outcome_side)
  - resolve_upcoming_windows(slug_pattern, hours_ahead, outcome_side)
  - build_node(pm_instrument_ids, sandbox, binance_us, sandbox_starting_usdc)
  - make_arg_parser(description)
  - prepare_run_metadata(...)
  - prepare_run(...)
  - schedule_stop(stop_target, run_secs)

Run scripts live in live/runs/ — each runner assembles its own node
by importing these helpers plus the client configs from live/config.py.
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import requests

from live.env import add_env_file_arg, load_project_env, validate_required_env_vars
from live.market_metadata import ResolvedWindowMetadata

load_project_env()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

GAMMA = "https://gamma-api.polymarket.com"
_FIRST_WINDOW_WARN_NS = 120_000_000_000


def resolve_upcoming_windows(
    slug_pattern: str,
    hours_ahead: int = 4,
    outcome_side: str = "yes",
) -> list[tuple[str, int]]:
    """
    Query Gamma API for current + upcoming market windows matching slug_pattern.
    Returns list of (pm_instrument_id, window_end_ns) ordered by time.

    Nautilus Polymarket instrument ID format: {condition_id}-{token_id}.POLYMARKET
    Makes one API call per window (~17 calls for 4h of 15m windows).
    """
    interval_secs = _parse_interval_secs(slug_pattern)
    now = int(time.time())
    window_start = (now // interval_secs) * interval_secs
    n_windows = (hours_ahead * 3600) // interval_secs + 1

    _validate_outcome_side(outcome_side)

    print(
        f"Resolving up to {n_windows} windows ({hours_ahead}h ahead) "
        f"for '{slug_pattern}' outcome={outcome_side.upper()}..."
    )

    metadata = resolve_upcoming_window_metadata(
        slug_pattern,
        hours_ahead=hours_ahead,
        outcome_side=outcome_side,
    )
    return [(window.instrument_id, window.window_end_ns) for window in metadata]


def resolve_upcoming_window_metadata(
    slug_pattern: str,
    hours_ahead: int = 4,
    outcome_side: str = "yes",
) -> list[ResolvedWindowMetadata]:
    """
    Query Gamma API for current + upcoming market windows matching slug_pattern.
    Returns rich metadata ordered by time.
    """
    interval_secs = _parse_interval_secs(slug_pattern)
    now = int(time.time())
    window_start = (now // interval_secs) * interval_secs
    n_windows = (hours_ahead * 3600) // interval_secs + 1

    _validate_outcome_side(outcome_side)

    print(
        f"Resolving up to {n_windows} windows ({hours_ahead}h ahead) "
        f"for '{slug_pattern}' outcome={outcome_side.upper()}..."
    )

    windows: list[ResolvedWindowMetadata] = []
    for i in range(n_windows):
        ts = window_start + i * interval_secs
        slug = f"{slug_pattern}-{ts}"
        try:
            resp = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10)
            resp.raise_for_status()
            markets = resp.json()
        except Exception as exc:
            print(f"  [{i:2d}] {slug} — request error: {exc}")
            continue

        if not markets:
            print(f"  [{i:2d}] {slug} — not found (market may not exist yet)")
            continue

        metadata = _window_metadata_from_market(
            slug=slug,
            market=markets[0],
            outcome_side=outcome_side,
            window_end_ns=(ts + interval_secs) * 1_000_000_000,
        )
        if metadata is None:
            print(f"  [{i:2d}] {slug} — missing conditionId or {outcome_side.upper()} token ID")
            continue

        windows.append(metadata)
        print(f"  [{i:2d}] {slug} [{metadata.selected_outcome_label}] → {metadata.instrument_id}")

    return windows


def build_node(
    pm_instrument_ids: list[str],
    sandbox: bool = False,
    binance_us: bool = False,
    sandbox_starting_usdc: float | None = None,
    log_path: str | None = None,
):
    """Build a TradingNode with Binance + Polymarket clients attached."""
    from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
    from nautilus_trader.adapters.polymarket import (
        PolymarketLiveDataClientFactory,
        PolymarketLiveExecClientFactory,
    )
    from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
    from nautilus_trader.config import LoggingConfig, TradingNodeConfig
    from nautilus_trader.live.config import LiveExecEngineConfig
    from nautilus_trader.live.node import TradingNode

    from live.config import (
        binance_data_config,
        polymarket_data_config,
        polymarket_exec_config,
        sandbox_exec_config,
    )

    logging_config = None
    if log_path is not None:
        log_file = os.path.abspath(log_path)
        logging_config = LoggingConfig(
            log_level="INFO",
            log_level_file="INFO",
            log_directory=os.path.dirname(log_file),
            log_file_name=os.path.basename(log_file),
            log_colors=False,
            clear_log_file=False,
        )

    node = TradingNode(config=TradingNodeConfig(
        exec_engine=LiveExecEngineConfig(
            reconciliation=not sandbox,
            convert_quote_qty_to_base=sandbox,
        ),
        data_clients={
            "BINANCE": binance_data_config(us=binance_us),
            "POLYMARKET": polymarket_data_config(pm_instrument_ids, sandbox=sandbox),
        },
        exec_clients={
            "POLYMARKET": (
                sandbox_exec_config(
                    starting_usdc=500.0 if sandbox_starting_usdc is None else sandbox_starting_usdc,
                )
                if sandbox
                else polymarket_exec_config()
            ),
        },
        logging=logging_config,
    ))
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_data_client_factory("POLYMARKET", PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(
        "POLYMARKET",
        SandboxLiveExecClientFactory if sandbox else PolymarketLiveExecClientFactory,
    )
    return node


def make_arg_parser(description: str) -> argparse.ArgumentParser:
    """Standard CLI args shared by all strategy runners."""
    parser = argparse.ArgumentParser(description=description)
    add_env_file_arg(parser)
    parser.add_argument("--slug-pattern", required=True,
                        help="Market slug pattern, e.g. btc-updown-15m")
    parser.add_argument("--hours-ahead", type=int, default=4,
                        help="Hours of windows to pre-load at startup (default: 4)")
    parser.add_argument("--run-secs", type=int, default=None,
                        help="Auto-stop after N seconds for bounded sandbox/manual runs")
    parser.add_argument("--outcome-side", choices=("yes", "no"), default="yes",
                        help="Select the first (yes) or second (no) Polymarket outcome token")
    parser.add_argument("--sandbox", action="store_true",
                        help="Sandbox mode: real data feeds, simulated execution (no real orders)")
    parser.add_argument("--sandbox-wallet-state-path", default=None,
                        help="Optional shared sandbox wallet-state JSON file for resolution tests")
    parser.add_argument("--sandbox-starting-usdc", type=float, default=None,
                        help="Override sandbox starting USDC.e balance for simulated execution")
    parser.add_argument("--binance-us", action="store_true",
                        help="Use Binance US endpoint (required if geo-restricted)")
    return parser


def prepare_run(
    *,
    slug_pattern: str,
    hours_ahead: int,
    outcome_side: str,
    sandbox: bool,
    binance_us: bool,
    run_secs: int | None,
    sandbox_starting_usdc: float | None = None,
) -> list[tuple[str, int]]:
    """Run shared live-runner preflight and return validated windows."""
    metadata = prepare_run_metadata(
        slug_pattern=slug_pattern,
        hours_ahead=hours_ahead,
        outcome_side=outcome_side,
        sandbox=sandbox,
        binance_us=binance_us,
        run_secs=run_secs,
        sandbox_starting_usdc=sandbox_starting_usdc,
    )
    return [(window.instrument_id, window.window_end_ns) for window in metadata]


def prepare_run_metadata(
    *,
    slug_pattern: str,
    hours_ahead: int,
    outcome_side: str,
    sandbox: bool,
    binance_us: bool,
    run_secs: int | None,
    sandbox_starting_usdc: float | None = None,
) -> list[ResolvedWindowMetadata]:
    """Run shared live-runner preflight and return validated metadata."""
    _validate_run_secs(run_secs)
    _validate_outcome_side(outcome_side)
    _validate_sandbox_starting_usdc(
        sandbox=sandbox,
        sandbox_starting_usdc=sandbox_starting_usdc,
    )
    _validate_required_env_vars(sandbox=sandbox)

    window_metadata = resolve_upcoming_window_metadata(
        slug_pattern,
        hours_ahead=hours_ahead,
        outcome_side=outcome_side,
    )
    windows = [(window.instrument_id, window.window_end_ns) for window in window_metadata]
    _validate_resolved_windows(windows)
    _print_run_summary(
        windows=windows,
        slug_pattern=slug_pattern,
        outcome_side=outcome_side,
        sandbox=sandbox,
        binance_us=binance_us,
        run_secs=run_secs,
        sandbox_starting_usdc=sandbox_starting_usdc,
    )
    return window_metadata


def schedule_stop(stop_target, run_secs: int | None):
    """Schedule a bounded stop for manual/sandbox runs."""
    if run_secs is None:
        return None

    callback = stop_target.stop if hasattr(stop_target, "stop") else stop_target
    timer = threading.Timer(run_secs, callback)
    timer.daemon = True
    timer.start()
    return timer


def _parse_interval_secs(slug_pattern: str) -> int:
    for part in slug_pattern.split("-"):
        if part.endswith("m") and part[:-1].isdigit():
            return int(part[:-1]) * 60
        if part.endswith("h") and part[:-1].isdigit():
            return int(part[:-1]) * 3600
    raise ValueError(f"Cannot parse interval from slug pattern: {slug_pattern!r}")


def _validate_run_secs(run_secs: int | None) -> None:
    if run_secs is not None and run_secs <= 0:
        raise SystemExit("--run-secs must be a positive integer")


def _validate_outcome_side(outcome_side: str) -> None:
    if outcome_side not in {"yes", "no"}:
        raise SystemExit("--outcome-side must be one of: yes, no")


def _validate_required_env_vars(*, sandbox: bool) -> None:
    validate_required_env_vars(sandbox=sandbox)


def _validate_sandbox_starting_usdc(*, sandbox: bool, sandbox_starting_usdc: float | None) -> None:
    if sandbox_starting_usdc is None:
        return
    if not sandbox:
        raise SystemExit("--sandbox-starting-usdc is only valid in sandbox mode")
    if sandbox_starting_usdc <= 0:
        raise SystemExit("--sandbox-starting-usdc must be a positive number")


def _validate_resolved_windows(windows: list[tuple[str, int]]) -> None:
    if not windows:
        raise SystemExit("No windows resolved. Check slug pattern or Gamma API connectivity.")

    instrument_ids = [instrument_id for instrument_id, _ in windows]
    if len(set(instrument_ids)) != len(instrument_ids):
        raise SystemExit("Resolved duplicate Polymarket instruments; aborting startup.")

    end_times = [window_end_ns for _, window_end_ns in windows]
    if any(current <= previous for previous, current in zip(end_times, end_times[1:])):
        raise SystemExit("Resolved windows are not strictly increasing by end time; aborting startup.")


def _window_metadata_from_market(
    *,
    slug: str,
    market: dict,
    outcome_side: str,
    window_end_ns: int,
) -> ResolvedWindowMetadata | None:
    condition_id = str(market.get("conditionId", "") or "")
    if not condition_id:
        return None

    yes_token_id, yes_label = _select_outcome_token(market, "yes")
    no_token_id, no_label = _select_outcome_token(market, "no")
    if not yes_token_id or not no_token_id:
        return None

    return ResolvedWindowMetadata(
        slug=slug,
        condition_id=condition_id,
        window_end_ns=window_end_ns,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        yes_outcome_label=yes_label,
        no_outcome_label=no_label,
        selected_outcome_side=outcome_side,
    )


def _print_run_summary(
    *,
    windows: list[tuple[str, int]],
    slug_pattern: str,
    outcome_side: str,
    sandbox: bool,
    binance_us: bool,
    run_secs: int | None,
    sandbox_starting_usdc: float | None,
) -> None:
    mode = "SANDBOX" if sandbox else "LIVE"
    binance_label = "Binance USDT futures (US)" if binance_us else "Binance USDT futures"
    first_end_ns = windows[0][1]
    last_end_ns = windows[-1][1]

    print()
    print(
        f"{mode} run | slug={slug_pattern} | feed={binance_label} | "
        f"outcome={outcome_side.upper()}"
    )
    print(f"Resolved {len(windows)} window(s)")
    print(f"First window ends : {_fmt_abs_ns(first_end_ns)} UTC")
    print(f"Last window ends  : {_fmt_abs_ns(last_end_ns)} UTC")
    if sandbox and sandbox_starting_usdc is not None:
        print(f"Sandbox balance   : {sandbox_starting_usdc:.6f} USDC.e")
    if run_secs is not None:
        print(f"Auto-stop after   : {run_secs}s")

    remaining_first_ns = first_end_ns - time.time_ns()
    if remaining_first_ns < _FIRST_WINDOW_WARN_NS:
        secs = max(0, remaining_first_ns // 1_000_000_000)
        print(f"WARNING: First window ends in {secs}s; startup may miss this window.")
    print()


def _fmt_abs_ns(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _select_outcome_token(market: dict[str, object], outcome_side: str) -> tuple[str | None, str]:
    token_ids = _parse_json_list(market.get("clobTokenIds"))
    outcome_labels = _parse_json_list(market.get("outcomes"))
    index = 0 if outcome_side == "yes" else 1

    if len(token_ids) <= index:
        return None, outcome_side.upper()

    label = outcome_labels[index] if len(outcome_labels) > index else outcome_side.upper()
    return str(token_ids[index]), str(label)


def _parse_json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []
