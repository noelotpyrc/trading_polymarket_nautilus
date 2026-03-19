#!/usr/bin/env python3
"""Stage 12a live limit-fill rehearsal on btc-updown-15m windows."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path

from py_clob_client.clob_types import (
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    TradeParams,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from live.env import add_env_file_arg, bootstrap_env_file, load_project_env
from live.market_metadata import ResolvedWindowMetadata, WindowMetadataRegistry
from live.node import resolve_upcoming_window_metadata
from live.redemption import ProdRedemptionExecutor
from live.rehearsal import extract_order_id, make_client, sync_conditional_balance
from live.resolution import fetch_market_resolution
from live.wallet_truth import ProdWalletTruthProvider, make_polymarket_balance_client

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()

DEFAULT_SLUG_PATTERN = "btc-updown-15m"
MIN_NOTIONAL_USDC = Decimal("5.00")
DEFAULT_ENTRY_NOTIONAL_USDC = Decimal("5.10")
DEFAULT_ENTRY_THRESHOLD = Decimal("0.90")
DEFAULT_ENTRY_WINDOW_SECS = 60
DEFAULT_ENTRY_CANCEL_BEFORE_EXPIRY_SECS = 10
DEFAULT_REPRICE_INTERVAL_SECS = 10
DEFAULT_EXIT_ATTEMPT_WINDOW_SECS = 30
DEFAULT_EXIT_PROFIT_BUFFER_USD = Decimal("0.01")
DEFAULT_POLL_INTERVAL_SECS = 2.0
DEFAULT_HOURS_AHEAD = 2
DEFAULT_SETTLEMENT_POLL_INTERVAL_SECS = 15.0
DEFAULT_SETTLEMENT_TIMEOUT_SECS = 1200.0
DEFAULT_ENTRY_BALANCE_SYNC_TIMEOUT_SECS = 5.0
DEFAULT_ENTRY_BALANCE_SYNC_POLL_INTERVAL_SECS = 0.5
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "logs" / "fill_rehearsal"


@dataclass(frozen=True)
class BookSnapshot:
    tick_size: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    min_order_size: Decimal | None = None


@dataclass(frozen=True)
class FilledEntry:
    size: Decimal
    average_price: Decimal
    source_order_id: str | None


@dataclass(frozen=True)
class WindowOutcome:
    status: str
    window_slug: str
    message: str


@dataclass(frozen=True)
class RunArtifacts:
    run_dir: Path
    log_path: Path
    command_path: Path
    summary_path: Path
    minute_mid_prices_path: Path


@dataclass(frozen=True)
class MinuteMidPrice:
    window_slug: str
    minute_start_ns: int
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None


class MinuteMidRecorder:
    def __init__(self) -> None:
        self._open_rows: dict[str, MinuteMidPrice] = {}
        self._rows: list[MinuteMidPrice] = []

    def observe(
        self,
        *,
        window_slug: str,
        observed_at_ns: int,
        snapshot: BookSnapshot,
        logger: logging.Logger,
    ) -> None:
        minute_start_ns = (observed_at_ns // 60_000_000_000) * 60_000_000_000
        row = MinuteMidPrice(
            window_slug=window_slug,
            minute_start_ns=minute_start_ns,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            midpoint=midpoint_from_snapshot(snapshot),
        )
        current = self._open_rows.get(window_slug)
        if current is None:
            self._open_rows[window_slug] = row
            return
        if current.minute_start_ns == minute_start_ns:
            self._open_rows[window_slug] = row
            return
        self._emit(current, logger)
        self._rows.append(current)
        self._open_rows[window_slug] = row

    def flush_window(self, *, window_slug: str, logger: logging.Logger) -> None:
        current = self._open_rows.pop(window_slug, None)
        if current is None:
            return
        self._emit(current, logger)
        self._rows.append(current)

    def flush_all(self, *, logger: logging.Logger) -> None:
        for window_slug in list(self._open_rows):
            self.flush_window(window_slug=window_slug, logger=logger)

    def serialized_rows(self) -> list[dict[str, object]]:
        return [
            {
                "window_slug": row.window_slug,
                "minute_start": _fmt_utc(row.minute_start_ns),
                "minute_start_ns": row.minute_start_ns,
                "best_bid": _serialize_decimal(row.best_bid),
                "best_ask": _serialize_decimal(row.best_ask),
                "midpoint": _serialize_decimal(row.midpoint),
            }
            for row in self._rows
        ]

    def _emit(self, row: MinuteMidPrice, logger: logging.Logger) -> None:
        logger.info(
            "[%s] Minute midpoint | minute=%s best_bid=%s best_ask=%s mid=%s",
            row.window_slug,
            _fmt_utc(row.minute_start_ns),
            _fmt_decimal(row.best_bid),
            _fmt_decimal(row.best_ask),
            _fmt_decimal(row.midpoint),
        )


class TeeStream:
    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Stage 12a live limit-fill rehearsal")
    add_env_file_arg(parser)
    parser.add_argument("--slug-pattern", default=DEFAULT_SLUG_PATTERN)
    parser.add_argument("--hours-ahead", type=int, default=DEFAULT_HOURS_AHEAD)
    parser.add_argument("--outcome-side", choices=("yes", "no"), default="yes")
    parser.add_argument(
        "--amount-usdc",
        type=Decimal,
        default=DEFAULT_ENTRY_NOTIONAL_USDC,
        help="Target entry notional in USDC (default: 5.10)",
    )
    parser.add_argument(
        "--entry-threshold",
        type=Decimal,
        default=DEFAULT_ENTRY_THRESHOLD,
        help="Require chosen-side best bid to exceed this price/probability before entry (default: 0.90)",
    )
    parser.add_argument("--entry-window-secs", type=int, default=DEFAULT_ENTRY_WINDOW_SECS)
    parser.add_argument(
        "--entry-cancel-before-expiry-secs",
        type=int,
        default=DEFAULT_ENTRY_CANCEL_BEFORE_EXPIRY_SECS,
    )
    parser.add_argument(
        "--reprice-interval-secs",
        type=int,
        default=DEFAULT_REPRICE_INTERVAL_SECS,
        help="Minimum seconds between cancel/replace actions (default: 10)",
    )
    parser.add_argument(
        "--exit-attempt-window-secs",
        type=int,
        default=DEFAULT_EXIT_ATTEMPT_WINDOW_SECS,
        help="Bounded exit management window after fill (default: 30)",
    )
    parser.add_argument(
        "--profit-buffer-usd",
        type=Decimal,
        default=DEFAULT_EXIT_PROFIT_BUFFER_USD,
        help="Absolute profitable exit buffer above average entry price (default: 0.01)",
    )
    parser.add_argument("--poll-interval-secs", type=float, default=DEFAULT_POLL_INTERVAL_SECS)
    parser.add_argument(
        "--wait-for-settlement",
        action="store_true",
        help="If live limit exit does not complete, wait for market resolution instead of stopping immediately",
    )
    parser.add_argument(
        "--settlement-timeout-secs",
        type=float,
        default=DEFAULT_SETTLEMENT_TIMEOUT_SECS,
    )
    parser.add_argument(
        "--settlement-poll-interval-secs",
        type=float,
        default=DEFAULT_SETTLEMENT_POLL_INTERVAL_SECS,
    )
    parser.add_argument(
        "--redeem-on-settlement",
        action="store_true",
        help="After resolution, attempt real redemption for the held position",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"Directory for persisted logs and summaries (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Optional label appended to the timestamped output directory",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt and execute immediately")
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_parser()
    args = parser.parse_args(argv)
    artifacts = prepare_artifacts(output_root=Path(args.output_root), label=args.label)
    started_at = utc_now()
    command = [sys.executable, str(PROJECT_ROOT / "live" / "fill_rehearsal.py"), *argv]
    artifacts.command_path.write_text(" ".join(command) + "\n", encoding="utf-8")

    summary: dict[str, object] = {
        "run_dir": str(artifacts.run_dir),
        "log_path": str(artifacts.log_path),
        "minute_mid_prices_path": str(artifacts.minute_mid_prices_path),
        "command": command,
        "status": "failed",
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "duration_secs": None,
        "outcome": None,
        "window_slug": None,
        "message": None,
        "slug_pattern": args.slug_pattern,
        "hours_ahead": args.hours_ahead,
        "outcome_side": args.outcome_side,
        "amount_usdc": str(args.amount_usdc),
        "entry_threshold": str(args.entry_threshold),
        "entry_window_secs": args.entry_window_secs,
        "entry_cancel_before_expiry_secs": args.entry_cancel_before_expiry_secs,
        "reprice_interval_secs": args.reprice_interval_secs,
        "exit_attempt_window_secs": args.exit_attempt_window_secs,
        "profit_buffer_usd": str(args.profit_buffer_usd),
        "wait_for_settlement": args.wait_for_settlement,
        "redeem_on_settlement": args.redeem_on_settlement,
    }

    with artifacts.log_path.open("w", encoding="utf-8") as log_handle:
        tee_stdout = TeeStream(sys.stdout, log_handle)
        tee_stderr = TeeStream(sys.stderr, log_handle)
        with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
            logger = configure_logger()
            minute_mid_recorder = MinuteMidRecorder()
            logger.info("Artifacts : %s", artifacts.run_dir)
            logger.info("Command   : %s", " ".join(command))
            try:
                if args.amount_usdc < MIN_NOTIONAL_USDC:
                    raise SystemExit(f"--amount-usdc must be at least {MIN_NOTIONAL_USDC}")
                if args.entry_cancel_before_expiry_secs >= args.entry_window_secs:
                    raise SystemExit("--entry-cancel-before-expiry-secs must be smaller than --entry-window-secs")

                client = make_client()
                windows = resolve_upcoming_window_metadata(
                    args.slug_pattern,
                    hours_ahead=args.hours_ahead,
                    outcome_side=args.outcome_side,
                )
                if not windows:
                    raise SystemExit("No upcoming windows resolved for the requested rehearsal horizon")

                logger.info("Stage 12a live fill rehearsal")
                logger.info("  slug_pattern        : %s", args.slug_pattern)
                logger.info("  outcome_side        : %s", args.outcome_side)
                logger.info("  watched windows     : %s", len(windows))
                logger.info("  entry threshold     : > %s", args.entry_threshold)
                logger.info("  entry notional      : %s USDC", args.amount_usdc)
                logger.info("  entry window        : last %ss", args.entry_window_secs)
                logger.info("  entry cutoff        : T-%ss", args.entry_cancel_before_expiry_secs)
                logger.info("  reprice cadence     : %ss", args.reprice_interval_secs)
                logger.info("  exit attempt window : %ss", args.exit_attempt_window_secs)
                logger.info("  profit buffer       : %s USD", args.profit_buffer_usd)
                logger.info("  settlement fallback : %s", "enabled" if args.wait_for_settlement else "disabled")
                if args.redeem_on_settlement:
                    logger.info("  settlement redeem   : enabled")

                summary["watched_windows"] = [
                    {
                        "slug": window.slug,
                        "window_end": _fmt_utc(window.window_end_ns),
                        "outcome_side": window.selected_outcome_side,
                    }
                    for window in windows
                ]
                for idx, window in enumerate(windows):
                    logger.info(
                        "  [%s] %s ends=%s outcome=%s",
                        idx,
                        window.slug,
                        _fmt_utc(window.window_end_ns),
                        window.selected_outcome_side.upper(),
                    )

                if not args.yes:
                    confirm = input("\nRun this Stage 12a live fill rehearsal? [y/N]: ")
                    if confirm.lower() != "y":
                        logger.warning("Operator cancelled Stage 12a rehearsal before any live order submission")
                        summary["status"] = "cancelled"
                        summary["message"] = "Operator cancelled before execution"
                        return

                for window in windows:
                    if _now_ns() >= window.window_end_ns:
                        logger.info("Skipping %s because the window already expired before monitoring began", window.slug)
                        continue
                    outcome = run_window_rehearsal(
                        client=client,
                        window=window,
                        args=args,
                        logger=logger,
                        minute_mid_recorder=minute_mid_recorder,
                    )
                    logger.info("Window outcome: %s — %s", outcome.status, outcome.window_slug)
                    logger.info("  %s", outcome.message)
                    summary["outcome"] = outcome.status
                    summary["window_slug"] = outcome.window_slug
                    summary["message"] = outcome.message
                    if outcome.status == "skipped":
                        continue
                    if outcome.status == "settlement_pending":
                        raise SystemExit(outcome.message)
                    summary["status"] = "passed"
                    return

                raise SystemExit("No qualifying live entry filled across the watched windows")
            except SystemExit as exc:
                if summary["status"] != "cancelled":
                    summary["status"] = "failed"
                    summary["message"] = str(exc)
                logger.error("%s", exc)
                raise
            finally:
                minute_mid_recorder.flush_all(logger=logger)
                artifacts.minute_mid_prices_path.write_text(
                    json.dumps(minute_mid_recorder.serialized_rows(), indent=2) + "\n",
                    encoding="utf-8",
                )
                finished_at = utc_now()
                summary["finished_at"] = finished_at.isoformat()
                summary["duration_secs"] = round((finished_at - started_at).total_seconds(), 3)
                artifacts.summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                logger.info("Summary   : %s", artifacts.summary_path)
                logger.info("Minute mids: %s", artifacts.minute_mid_prices_path)


def run_window_rehearsal(
    *,
    client,
    window: ResolvedWindowMetadata,
    args,
    logger: logging.Logger,
    minute_mid_recorder: MinuteMidRecorder,
) -> WindowOutcome:
    token = window.token(args.outcome_side)
    logger.info("")
    logger.info("Watching %s (%s) until %s", window.slug, token.outcome_label, _fmt_utc(window.window_end_ns))

    filled_entry = manage_entry(
        client=client,
        token_id=token.token_id,
        window_slug=window.slug,
        window_end_ns=window.window_end_ns,
        amount_usdc=args.amount_usdc,
        entry_threshold=args.entry_threshold,
        entry_window_secs=args.entry_window_secs,
        entry_cancel_before_expiry_secs=args.entry_cancel_before_expiry_secs,
        reprice_interval_secs=args.reprice_interval_secs,
        poll_interval_secs=args.poll_interval_secs,
        logger=logger,
        minute_mid_recorder=minute_mid_recorder,
    )
    if filled_entry is None:
        minute_mid_recorder.flush_window(window_slug=window.slug, logger=logger)
        return WindowOutcome(
            status="skipped",
            window_slug=window.slug,
            message="No filled entry before cutoff; canceled any passive entry order and skipped this window.",
        )

    exit_flat = manage_exit(
        client=client,
        token_id=token.token_id,
        window_slug=window.slug,
        filled_entry=filled_entry,
        profit_buffer_usd=args.profit_buffer_usd,
        exit_attempt_window_secs=args.exit_attempt_window_secs,
        reprice_interval_secs=args.reprice_interval_secs,
        poll_interval_secs=args.poll_interval_secs,
        logger=logger,
        minute_mid_recorder=minute_mid_recorder,
    )
    if exit_flat:
        minute_mid_recorder.flush_window(window_slug=window.slug, logger=logger)
        return WindowOutcome(
            status="limit_exit",
            window_slug=window.slug,
            message=(
                f"Filled entry at avg {filled_entry.average_price} and flattened via live limit exit "
                f"with no remaining token balance."
            ),
        )

    if not args.wait_for_settlement:
        remaining = sync_conditional_balance(client, token.token_id)
        minute_mid_recorder.flush_window(window_slug=window.slug, logger=logger)
        return WindowOutcome(
            status="settlement_pending",
            window_slug=window.slug,
            message=(
                "Limit exit window expired; remaining position left for later settlement handling "
                f"({remaining:.6f} shares)."
            ),
        )

    settlement_message = wait_for_settlement(
        client=client,
        window=window,
        token_id=token.token_id,
        timeout_secs=args.settlement_timeout_secs,
        poll_interval_secs=args.settlement_poll_interval_secs,
        redeem_on_settlement=args.redeem_on_settlement,
        logger=logger,
    )
    minute_mid_recorder.flush_window(window_slug=window.slug, logger=logger)
    return WindowOutcome(
        status="settled",
        window_slug=window.slug,
        message=settlement_message,
    )


def manage_entry(
    *,
    client,
    token_id: str,
    window_slug: str,
    window_end_ns: int,
    amount_usdc: Decimal,
    entry_threshold: Decimal,
    entry_window_secs: int,
    entry_cancel_before_expiry_secs: int,
    reprice_interval_secs: int,
    poll_interval_secs: float,
    logger: logging.Logger,
    minute_mid_recorder: MinuteMidRecorder,
) -> FilledEntry | None:
    entry_start_ns = window_end_ns - entry_window_secs * 1_000_000_000
    entry_cutoff_ns = window_end_ns - entry_cancel_before_expiry_secs * 1_000_000_000
    order_id: str | None = None
    current_price: Decimal | None = None
    last_action_mono = 0.0
    last_payload: dict | None = None

    while _now_ns() < entry_cutoff_ns:
        now_ns = _now_ns()
        if now_ns < entry_start_ns:
            sleep_secs = min(poll_interval_secs, (entry_start_ns - now_ns) / 1_000_000_000)
            time.sleep(max(0.0, sleep_secs))
            continue

        book = fetch_book_snapshot(client, token_id)
        minute_mid_recorder.observe(
            window_slug=window_slug,
            observed_at_ns=now_ns,
            snapshot=book,
            logger=logger,
        )
        target_entry_price = choose_entry_price(snapshot=book, threshold=entry_threshold)
        now_mono = time.monotonic()
        seconds_to_expiry = max(0.0, (window_end_ns - now_ns) / 1_000_000_000)
        current_status = "none"
        current_matched = Decimal("0")

        if order_id is not None:
            payload = safe_get_order(client, order_id)
            last_payload = payload or last_payload
            matched_size = order_matched_size(payload)
            status = order_status(payload)
            current_status = status.value
            current_matched = matched_size
            if matched_size > 0:
                logger.info(
                    "[%s] Entry fill observed | order_id=%s status=%s matched_size=%s",
                    window_slug,
                    order_id,
                    status.value,
                    matched_size,
                )
                cancel_if_open(
                    client,
                    order_id,
                    token_id,
                    poll_interval_secs=poll_interval_secs,
                    logger=logger,
                    reason="entry remainder cleanup after fill",
                )
                avg_price = resolve_average_fill_price(client, payload)
                size = await_entry_balance_sync(
                    client=client,
                    token_id=token_id,
                    window_slug=window_slug,
                    logger=logger,
                )
                logger.info(
                    "[%s] Entry position ready | size=%s avg_fill=%s",
                    window_slug,
                    size,
                    avg_price,
                )
                return FilledEntry(size=size, average_price=avg_price, source_order_id=order_id)

            if status.is_open:
                if target_entry_price is None:
                    if matched_size <= 0:
                        logger.info(
                            "[%s] Entry no longer qualifies; cancelling open entry order %s",
                            window_slug,
                            order_id,
                        )
                        cancel_if_open(
                            client,
                            order_id,
                            token_id,
                            poll_interval_secs=poll_interval_secs,
                            logger=logger,
                            reason="entry no longer qualified",
                        )
                        order_id = None
                        current_price = None
                        last_action_mono = now_mono
                        continue
                elif matched_size <= 0 and now_mono - last_action_mono >= reprice_interval_secs:
                    if current_price != target_entry_price:
                        logger.info(
                            "[%s] Repricing passive entry | old=%s new=%s order_id=%s",
                            window_slug,
                            current_price,
                            target_entry_price,
                            order_id,
                        )
                        terminal_payload = cancel_if_open(
                            client,
                            order_id,
                            token_id,
                            poll_interval_secs=poll_interval_secs,
                            logger=logger,
                            reason="entry reprice",
                        )
                        terminal_matched = order_matched_size(terminal_payload)
                        if terminal_matched > 0:
                            logger.info(
                                "[%s] Entry partial fill detected while repricing | order_id=%s matched_size=%s",
                                window_slug,
                                order_id,
                                terminal_matched,
                            )
                            avg_price = resolve_average_fill_price(client, terminal_payload)
                            size = await_entry_balance_sync(
                                client=client,
                                token_id=token_id,
                                window_slug=window_slug,
                                logger=logger,
                            )
                            logger.info(
                                "[%s] Entry position ready after reprice cancel | size=%s avg_fill=%s",
                                window_slug,
                                size,
                                avg_price,
                            )
                            return FilledEntry(size=size, average_price=avg_price, source_order_id=order_id)
                        order_id = None
                        current_price = None
                        last_action_mono = now_mono
                        continue

        logger.info(
            "[%s] Entry checkpoint | tte=%.1fs best_bid=%s best_ask=%s tick=%s threshold=%s "
            "target=%s order_id=%s status=%s matched=%s current_price=%s",
            window_slug,
            seconds_to_expiry,
            _fmt_decimal(book.best_bid),
            _fmt_decimal(book.best_ask),
            book.tick_size,
            entry_threshold,
            _fmt_decimal(target_entry_price),
            order_id or "none",
            current_status,
            current_matched,
            _fmt_decimal(current_price),
        )

        if order_id is None and target_entry_price is not None and now_mono - last_action_mono >= reprice_interval_secs:
            size = size_from_notional(amount_usdc, target_entry_price)
            if size <= 0:
                raise SystemExit("Computed non-positive entry size")
            order_id, response = submit_limit_order(
                client,
                token_id=token_id,
                side="BUY",
                price=target_entry_price,
                size=size,
                tick_size=book.tick_size,
            )
            logger.info(
                "[%s] Submitted passive entry BUY | order_id=%s price=%s size=%s notional=%s response=%s",
                window_slug,
                order_id,
                target_entry_price,
                size,
                (size * target_entry_price).quantize(Decimal("0.000001")),
                response,
            )
            current_price = target_entry_price
            last_action_mono = now_mono

        time.sleep(poll_interval_secs)

    if order_id is not None:
        payload = safe_get_order(client, order_id)
        last_payload = payload or last_payload
        logger.info("[%s] Entry cutoff reached; cancelling outstanding entry order %s", window_slug, order_id)
        terminal_payload = cancel_if_open(
            client,
            order_id,
            token_id,
            poll_interval_secs=poll_interval_secs,
            logger=logger,
            reason="entry cutoff",
        )
        if order_matched_size(terminal_payload) > 0:
            size = Decimal(str(sync_conditional_balance(client, token_id)))
            average_price = resolve_average_fill_price(client, terminal_payload)
            logger.info(
                "[%s] Partial entry preserved at cutoff | size=%s avg_fill=%s",
                window_slug,
                size,
                average_price,
            )
            return FilledEntry(size=size, average_price=average_price, source_order_id=order_id)

    size = Decimal(str(sync_conditional_balance(client, token_id)))
    if size <= 0:
        logger.info("[%s] Entry window ended with no filled position", window_slug)
        return None

    average_price = resolve_average_fill_price(client, last_payload)
    logger.info(
        "[%s] Partial entry preserved at cutoff | size=%s avg_fill=%s",
        window_slug,
        size,
        average_price,
    )
    return FilledEntry(size=size, average_price=average_price, source_order_id=order_id)


def manage_exit(
    *,
    client,
    token_id: str,
    window_slug: str,
    filled_entry: FilledEntry,
    profit_buffer_usd: Decimal,
    exit_attempt_window_secs: int,
    reprice_interval_secs: int,
    poll_interval_secs: float,
    logger: logging.Logger,
    minute_mid_recorder: MinuteMidRecorder,
) -> bool:
    exit_deadline_mono = time.monotonic() + exit_attempt_window_secs
    order_id: str | None = None
    current_price: Decimal | None = None
    last_action_mono = 0.0

    while time.monotonic() < exit_deadline_mono:
        balance = Decimal(str(sync_conditional_balance(client, token_id)))
        if balance <= 0:
            logger.info("[%s] Exit complete; conditional balance is flat", window_slug)
            return True

        book = fetch_book_snapshot(client, token_id)
        minute_mid_recorder.observe(
            window_slug=window_slug,
            observed_at_ns=_now_ns(),
            snapshot=book,
            logger=logger,
        )
        if book.min_order_size is not None and balance < book.min_order_size:
            logger.warning(
                "[%s] Exit impossible below PM minimum size | remaining=%s min_order_size=%s; falling back to settlement",
                window_slug,
                balance,
                book.min_order_size,
            )
            return False
        raw_profit_floor = filled_entry.average_price + profit_buffer_usd
        if profitable_exit_impossible(raw_profit_floor):
            logger.warning(
                "[%s] Profitable live exit impossible | entry_avg=%s raw_profit_floor=%s exceeds max price 1.0; "
                "falling back to settlement",
                window_slug,
                filled_entry.average_price,
                raw_profit_floor,
            )
            return False
        profitable_floor = round_up_to_tick(raw_profit_floor, book.tick_size)
        target_exit_price = choose_exit_price(snapshot=book, profitable_floor=profitable_floor)
        now_mono = time.monotonic()
        current_status = "none"

        if order_id is not None:
            payload = safe_get_order(client, order_id)
            status = order_status(payload)
            current_status = status.value
            if status.is_terminal:
                logger.info("[%s] Exit order terminalized | order_id=%s status=%s", window_slug, order_id, status.value)
                order_id = None
                current_price = None
            elif now_mono - last_action_mono >= reprice_interval_secs and target_exit_price != current_price:
                logger.info(
                    "[%s] Repricing passive exit | old=%s new=%s floor=%s order_id=%s",
                    window_slug,
                    current_price,
                    target_exit_price,
                    profitable_floor,
                    order_id,
                )
                cancel_if_open(
                    client,
                    order_id,
                    token_id,
                    poll_interval_secs=poll_interval_secs,
                    logger=logger,
                    reason="exit reprice",
                )
                order_id = None
                current_price = None
                last_action_mono = now_mono

        logger.info(
            "[%s] Exit checkpoint | remaining=%s best_bid=%s best_ask=%s tick=%s "
            "entry_avg=%s profit_floor=%s target=%s order_id=%s status=%s current_price=%s",
            window_slug,
            balance,
            _fmt_decimal(book.best_bid),
            _fmt_decimal(book.best_ask),
            book.tick_size,
            filled_entry.average_price,
            profitable_floor,
            target_exit_price,
            order_id or "none",
            current_status,
            _fmt_decimal(current_price),
        )

        if order_id is None and now_mono - last_action_mono >= reprice_interval_secs:
            order_id, response = submit_limit_order(
                client,
                token_id=token_id,
                side="SELL",
                price=target_exit_price,
                size=balance,
                tick_size=book.tick_size,
            )
            logger.info(
                "[%s] Submitted passive exit SELL | order_id=%s price=%s size=%s response=%s",
                window_slug,
                order_id,
                target_exit_price,
                balance,
                response,
            )
            current_price = target_exit_price
            last_action_mono = now_mono

        time.sleep(poll_interval_secs)

    if order_id is not None:
        logger.warning("[%s] Exit attempt window expired; cancelling outstanding exit order %s", window_slug, order_id)
        cancel_if_open(
            client,
            order_id,
            token_id,
            poll_interval_secs=poll_interval_secs,
            logger=logger,
            reason="exit attempt window expired",
        )

    remaining = Decimal(str(sync_conditional_balance(client, token_id)))
    logger.warning("[%s] Exit branch ended with remaining balance=%s; settlement fallback required", window_slug, remaining)
    return Decimal(str(sync_conditional_balance(client, token_id))) <= 0


def await_entry_balance_sync(
    *,
    client,
    token_id: str,
    window_slug: str,
    logger: logging.Logger,
    timeout_secs: float = DEFAULT_ENTRY_BALANCE_SYNC_TIMEOUT_SECS,
    poll_interval_secs: float = DEFAULT_ENTRY_BALANCE_SYNC_POLL_INTERVAL_SECS,
) -> Decimal:
    deadline = time.monotonic() + timeout_secs
    while True:
        size = Decimal(str(sync_conditional_balance(client, token_id)))
        if size > 0:
            return size
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"[{window_slug}] Entry fill observed but conditional balance did not sync within {timeout_secs}s"
            )
        logger.info(
            "[%s] Entry fill synced in order state, waiting for conditional balance visibility...",
            window_slug,
        )
        time.sleep(poll_interval_secs)


def wait_for_settlement(
    *,
    client,
    window: ResolvedWindowMetadata,
    token_id: str,
    timeout_secs: float,
    poll_interval_secs: float,
    redeem_on_settlement: bool,
    logger: logging.Logger,
) -> str:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        resolution = fetch_market_resolution(window.condition_id, token_id)
        logger.info(
            "[%s] Settlement checkpoint | resolved=%s token_won=%s settlement_price=%s",
            window.slug,
            resolution.resolved,
            resolution.token_won,
            resolution.settlement_price,
        )
        if resolution.resolved:
            won = resolution.token_won
            message = f"Market resolved with token_won={won} settlement_price={resolution.settlement_price}"
            if redeem_on_settlement:
                redemption_status = redeem_resolved_position(window)
                message = f"{message}; {redemption_status}"
            remaining = sync_conditional_balance(client, token_id)
            message = f"{message}; remaining conditional balance={remaining:.6f} shares"
            return message
        time.sleep(poll_interval_secs)

    raise SystemExit(
        f"Timed out waiting for settlement on {window.slug} after {timeout_secs}s"
    )


def redeem_resolved_position(window: ResolvedWindowMetadata) -> str:
    registry = WindowMetadataRegistry([window])
    balance_client, funder = make_polymarket_balance_client(sandbox=False)
    wallet_address = os.getenv("POLYMARKET_FUNDER") or os.environ["WALLET_ADDRESS"]
    provider = ProdWalletTruthProvider(
        wallet_address=funder,
        balance_client=balance_client,
        registry=registry,
    )
    snapshot = provider.snapshot()
    positions = tuple(position for position in snapshot.positions if position.condition_id == window.condition_id)
    if not positions:
        return "no redeemable position found in wallet truth snapshot"

    resolution = fetch_market_resolution(window.condition_id, window.selected_token_id)
    executor = ProdRedemptionExecutor(
        private_key=os.environ["PRIVATE_KEY"],
        wallet_address=wallet_address,
        dry_run=False,
    )
    results = executor.settle(positions=positions, resolution=resolution)
    statuses = ", ".join(
        f"{result.token_id[:10]}...:{result.status}:{result.transaction_hash or 'n/a'}"
        for result in results
    )
    return f"redemption attempted ({statuses})"


def fetch_book_snapshot(client, token_id: str) -> BookSnapshot:
    book = client.get_order_book(token_id)
    bids = sorted(book.bids, key=lambda level: _book_price(level.price), reverse=True)
    asks = sorted(book.asks, key=lambda level: _book_price(level.price))
    return BookSnapshot(
        tick_size=Decimal(str(book.tick_size)),
        best_bid=_book_price(bids[0].price) if bids else None,
        best_ask=_book_price(asks[0].price) if asks else None,
        min_order_size=None if book.min_order_size is None else Decimal(str(book.min_order_size)),
    )


def choose_entry_price(*, snapshot: BookSnapshot, threshold: Decimal) -> Decimal | None:
    if snapshot.best_bid is None or snapshot.best_bid <= threshold:
        return None
    return snapshot.best_bid


def choose_exit_price(*, snapshot: BookSnapshot, profitable_floor: Decimal) -> Decimal:
    if snapshot.best_ask is None:
        return profitable_floor
    return max(snapshot.best_ask, profitable_floor)


def size_from_notional(notional_usdc: Decimal, price: Decimal) -> Decimal:
    if price <= 0:
        raise ValueError("price must be positive")
    effective_notional = max(notional_usdc, MIN_NOTIONAL_USDC)
    return (effective_notional / price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def round_up_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    steps = (value / tick_size).to_integral_value(rounding=ROUND_UP)
    return (steps * tick_size).quantize(tick_size)


def profitable_exit_impossible(raw_profit_floor: Decimal) -> bool:
    return raw_profit_floor > Decimal("1")


def submit_limit_order(client, *, token_id: str, side: str, price: Decimal, size: Decimal, tick_size: Decimal) -> tuple[str, dict]:
    order = client.create_order(
        OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=side,
        ),
        options=PartialCreateOrderOptions(tick_size=_tick_size_literal(tick_size)),
    )
    response = client.post_order(order, OrderType.GTC, post_only=True)
    if not response.get("success"):
        raise SystemExit(f"{side} order was not accepted: {response}")
    order_id = extract_order_id(response)
    if not order_id:
        raise SystemExit(f"Could not extract order id from response: {response}")
    return order_id, response


def cancel_if_open(
    client,
    order_id: str,
    token_id: str,
    *,
    poll_interval_secs: float,
    logger: logging.Logger,
    reason: str,
) -> dict | None:
    payload = safe_get_order(client, order_id)
    if not order_status(payload).is_open:
        return payload
    response = client.cancel(order_id)
    logger.info("Cancel response for %s (%s): %s", order_id, reason, response)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        payload = safe_get_order(client, order_id)
        if not order_status(payload).is_open:
            logger.info("Cancel confirmed for %s (%s)", order_id, reason)
            return payload
        time.sleep(poll_interval_secs)
    raise SystemExit(f"Timed out waiting for cancel confirmation: {order_id}")


def safe_get_order(client, order_id: str | None) -> dict | None:
    if order_id is None:
        return None
    try:
        return client.get_order(order_id)
    except Exception:
        return None


@dataclass(frozen=True)
class _OrderStatus:
    value: str

    @property
    def is_open(self) -> bool:
        return self.value in {"open", "live", "placed", "active", "partially_filled"}

    @property
    def is_terminal(self) -> bool:
        return not self.is_open


def order_status(payload: dict | None) -> _OrderStatus:
    if not payload:
        return _OrderStatus("not_found")
    raw = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if raw in {"partiallyfilled", "partially_filled", "partial"}:
        raw = "partially_filled"
    return _OrderStatus(raw or "unknown")


def order_matched_size(payload: dict | None) -> Decimal:
    if not payload:
        return Decimal("0")
    value = payload.get("size_matched") or payload.get("matched_size") or 0
    return Decimal(str(value))


def resolve_average_fill_price(client, payload: dict | None) -> Decimal:
    if not payload:
        raise SystemExit("Cannot resolve average fill price without an order payload")

    trade_ids = {str(value) for value in payload.get("associate_trades") or () if value}
    fallback_price = Decimal(str(payload.get("price") or "0"))
    if not trade_ids:
        return fallback_price

    created_at = int(payload.get("created_at") or 0)
    trades = client.get_trades(
        TradeParams(
            market=payload.get("market"),
            asset_id=payload.get("asset_id"),
            after=max(0, created_at - 300),
        )
    )
    matched = [trade for trade in trades if str(trade.get("id")) in trade_ids]
    if not matched:
        return fallback_price

    total_size = Decimal("0")
    total_notional = Decimal("0")
    for trade in matched:
        size = Decimal(str(trade.get("size") or "0"))
        price = Decimal(str(trade.get("price") or "0"))
        total_size += size
        total_notional += size * price

    if total_size <= 0:
        return fallback_price
    return (total_notional / total_size).quantize(Decimal("0.000001"))


def _book_price(value: str | float) -> Decimal:
    return Decimal(str(value))


def _tick_size_literal(tick_size: Decimal) -> str:
    return format(tick_size.normalize(), "f")


def midpoint_from_snapshot(snapshot: BookSnapshot) -> Decimal | None:
    if snapshot.best_bid is None or snapshot.best_ask is None:
        return None
    return ((snapshot.best_bid + snapshot.best_ask) / Decimal("2")).quantize(Decimal("0.000001"))


def _now_ns() -> int:
    return time.time_ns()


def _fmt_utc(ts_ns: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts_ns / 1_000_000_000))


def _fmt_decimal(value: Decimal | None) -> str:
    return "n/a" if value is None else str(value)


def _serialize_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def prepare_artifacts(*, output_root: Path, label: str | None) -> RunArtifacts:
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = _make_run_dir(output_root=output_root, label=label)
    run_dir.mkdir(parents=True, exist_ok=False)
    return RunArtifacts(
        run_dir=run_dir,
        log_path=run_dir / "runner.log",
        command_path=run_dir / "command.txt",
        summary_path=run_dir / "summary.json",
        minute_mid_prices_path=run_dir / "minute_mid_prices.json",
    )


def configure_logger() -> logging.Logger:
    logger = logging.getLogger("fill_rehearsal")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)sZ [%(levelname)s] %(message)s")
    formatter.converter = time.gmtime

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.handlers.clear()
    logger.addHandler(stream_handler)
    return logger


def _make_run_dir(*, output_root: Path, label: str | None) -> Path:
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    if not label:
        return output_root / timestamp
    return output_root / f"{timestamp}_{_safe_name(label)}"


def _safe_name(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in {"-", "_"}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or "run"


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


if __name__ == "__main__":
    main()
