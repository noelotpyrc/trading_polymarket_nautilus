"""Standalone market-data capture for post-run analysis."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable

import websockets

from live.market_metadata import ResolvedWindowMetadata

PM_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_FUTURES_WS_BASE_URL = "wss://fstream.binance.com/ws"


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def ns_to_utc(ts_ns: int | None) -> datetime | None:
    if ts_ns is None:
        return None
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)


def window_start_ns(window: ResolvedWindowMetadata) -> int:
    suffix = window.slug.rsplit("-", 1)[-1]
    if not suffix.isdigit():
        raise ValueError(f"Could not parse window start from slug: {window.slug}")
    return int(suffix) * 1_000_000_000


def select_active_window(
    windows: list[ResolvedWindowMetadata] | tuple[ResolvedWindowMetadata, ...],
    now_ns: int,
) -> ResolvedWindowMetadata | None:
    for window in windows:
        if now_ns < window.window_end_ns:
            return window
    return None


def default_binance_stream_url(symbol: str, *, us: bool) -> str:
    if us:
        raise ValueError(
            "Binance US websocket routing is not implemented for market-data capture yet; "
            "use --binance-stream-url to override explicitly."
        )
    return f"{BINANCE_FUTURES_WS_BASE_URL}/{symbol.lower()}@bookTicker"


def _coerce_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_source_ts(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        if value > 1_000_000_000_000_000:
            return datetime.fromtimestamp(float(value) / 1_000_000_000, tz=timezone.utc)
        if value > 1_000_000_000_000:
            return datetime.fromtimestamp(float(value) / 1_000, tz=timezone.utc)
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_source_ts(int(text))
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass
class QuoteState:
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    source_ts: datetime | None = None
    updated_at: datetime | None = None

    def update(
        self,
        *,
        bid: object = None,
        ask: object = None,
        bid_size: object = None,
        ask_size: object = None,
        source_ts: object = None,
    ) -> None:
        self.bid = _coerce_float(bid)
        self.ask = _coerce_float(ask)
        self.bid_size = _coerce_float(bid_size)
        self.ask_size = _coerce_float(ask_size)
        self.source_ts = _parse_source_ts(source_ts)
        self.updated_at = utc_now()

    def payload(self, *, recorded_at: datetime) -> dict[str, object]:
        mid = None
        spread = None
        if self.bid is not None and self.ask is not None:
            mid = (self.bid + self.ask) / 2
            spread = self.ask - self.bid
        source_age_secs = None
        if self.source_ts is not None:
            source_age_secs = max(0.0, (recorded_at - self.source_ts).total_seconds())
        return {
            "source_ts": self.source_ts,
            "source_age_secs": source_age_secs,
            "bid": self.bid,
            "bid_size": self.bid_size,
            "ask": self.ask,
            "ask_size": self.ask_size,
            "mid": mid,
            "spread": spread,
        }


def apply_pm_quote_message(
    *,
    yes_token_id: str,
    no_token_id: str,
    yes_quote: QuoteState,
    no_quote: QuoteState,
    message: dict[str, object],
) -> bool:
    token_id = str(message.get("asset_id") or "")
    if token_id == str(yes_token_id):
        target = yes_quote
    elif token_id == str(no_token_id):
        target = no_quote
    else:
        return False

    event_type = str(message.get("event_type") or message.get("type") or "")
    source_ts = message.get("timestamp") or message.get("ts")

    if event_type == "best_bid_ask":
        target.update(
            bid=message.get("best_bid"),
            ask=message.get("best_ask"),
            bid_size=message.get("best_bid_size"),
            ask_size=message.get("best_ask_size"),
            source_ts=source_ts,
        )
        return True

    if event_type == "book":
        # PM book payload ordering is not stable enough to treat the first level
        # as authoritative top-of-book. Only best_bid_ask should drive samples.
        return True

    return False


def apply_binance_book_ticker(
    *,
    quote: QuoteState,
    message: dict[str, object],
) -> bool:
    payload = message.get("data") if isinstance(message.get("data"), dict) else message
    if not isinstance(payload, dict):
        return False
    if "b" not in payload or "a" not in payload:
        return False

    quote.update(
        bid=payload.get("b"),
        ask=payload.get("a"),
        bid_size=payload.get("B"),
        ask_size=payload.get("A"),
        source_ts=payload.get("E") or payload.get("T"),
    )
    return True


def build_sample_payload(
    *,
    window: ResolvedWindowMetadata,
    recorded_at: datetime,
    yes_quote: QuoteState,
    no_quote: QuoteState,
    binance_quote: QuoteState,
    sample_interval_secs: float,
    binance_symbol: str,
) -> dict[str, object]:
    start_ns = window_start_ns(window)
    end_ns = window.window_end_ns
    ttl_secs = max(0.0, (ns_to_utc(end_ns) - recorded_at).total_seconds())
    return {
        "event_type": "sample",
        "recorded_at": recorded_at,
        "market_slug": window.slug,
        "condition_id": window.condition_id,
        "window_start_ns": start_ns,
        "window_start_utc": ns_to_utc(start_ns),
        "window_end_ns": end_ns,
        "window_end_utc": ns_to_utc(end_ns),
        "ttl_secs": ttl_secs,
        "sample_interval_secs": sample_interval_secs,
        "pm_yes": {
            "token_id": window.yes_token_id,
            "outcome_label": window.yes_outcome_label,
            **yes_quote.payload(recorded_at=recorded_at),
        },
        "pm_no": {
            "token_id": window.no_token_id,
            "outcome_label": window.no_outcome_label,
            **no_quote.payload(recorded_at=recorded_at),
        },
        "binance": {
            "symbol": binance_symbol,
            **binance_quote.payload(recorded_at=recorded_at),
        },
    }


class MarketDataCaptureRecorder:
    def __init__(
        self,
        *,
        windows: list[ResolvedWindowMetadata],
        binance_symbol: str,
        sample_interval_secs: float,
        samples_path: Path,
        pm_ws_url: str = PM_MARKET_WS_URL,
        binance_ws_url: str,
        refresh_windows: Callable[[], list[ResolvedWindowMetadata]] | None = None,
    ) -> None:
        self._windows = list(windows)
        self._binance_symbol = binance_symbol
        self._sample_interval_secs = sample_interval_secs
        self._samples_path = samples_path.expanduser().resolve()
        self._pm_ws_url = pm_ws_url
        self._binance_ws_url = binance_ws_url
        self._refresh_windows = refresh_windows

        self._current_window: ResolvedWindowMetadata | None = None
        self._yes_quote = QuoteState()
        self._no_quote = QuoteState()
        self._binance_quote = QuoteState()

        self._handle = None
        self._pm_task: asyncio.Task[None] | None = None
        self._binance_task: asyncio.Task[None] | None = None
        self._last_sample_monotonic = 0.0
        self._last_window_refresh_monotonic = 0.0

    async def run(self, *, once: bool = False) -> None:
        self._open_writer()
        self._write_json({
            "event_type": "capture_started",
            "recorded_at": utc_now(),
            "binance_symbol": self._binance_symbol,
            "sample_interval_secs": self._sample_interval_secs,
        })

        self._binance_task = asyncio.create_task(self._run_binance_stream())
        try:
            while True:
                await self._maybe_refresh_windows()
                active_window = select_active_window(self._windows, time.time_ns())
                if active_window != self._current_window:
                    await self._switch_window(active_window)

                if self._current_window is not None:
                    now = time.monotonic()
                    if (
                        self._last_sample_monotonic == 0.0
                        or now - self._last_sample_monotonic >= self._sample_interval_secs
                    ):
                        self._emit_sample(self._current_window)
                        self._last_sample_monotonic = now
                        if once:
                            return
                await asyncio.sleep(min(1.0, max(0.5, self._sample_interval_secs / 5)))
        finally:
            await self._switch_window(None)
            await self._cancel_task(self._binance_task)
            self._write_json({
                "event_type": "capture_stopped",
                "recorded_at": utc_now(),
            })
            self._close_writer()

    async def _maybe_refresh_windows(self) -> None:
        if self._refresh_windows is None:
            return
        if select_active_window(self._windows, time.time_ns()) is not None:
            return
        now = time.monotonic()
        if now - self._last_window_refresh_monotonic < 15:
            return
        refreshed = self._refresh_windows()
        if refreshed:
            self._windows = refreshed
        self._last_window_refresh_monotonic = now

    async def _switch_window(self, window: ResolvedWindowMetadata | None) -> None:
        previous = self._current_window
        if previous is not None:
            self._write_window_event("window_observation_stopped", previous)
        await self._cancel_task(self._pm_task)
        self._pm_task = None
        self._current_window = window
        self._yes_quote = QuoteState()
        self._no_quote = QuoteState()
        if window is not None:
            self._write_window_event("window_observation_started", window)
            self._pm_task = asyncio.create_task(self._run_pm_stream(window))

    async def _run_pm_stream(self, window: ResolvedWindowMetadata) -> None:
        token_ids = [window.yes_token_id, window.no_token_id]
        subscribe_payload = {
            "type": "market",
            "assets_ids": token_ids,
            "custom_feature_enabled": True,
        }
        while True:
            try:
                async with websockets.connect(
                    self._pm_ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    await ws.send(json.dumps(subscribe_payload))
                    async for raw in ws:
                        payload = json.loads(raw)
                        messages = payload if isinstance(payload, list) else [payload]
                        for message in messages:
                            if not isinstance(message, dict):
                                continue
                            apply_pm_quote_message(
                                yes_token_id=window.yes_token_id,
                                no_token_id=window.no_token_id,
                                yes_quote=self._yes_quote,
                                no_quote=self._no_quote,
                                message=message,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"PM capture reconnect for {window.slug}: "
                    f"{exc.__class__.__name__}: {exc}",
                    flush=True,
                )
                await asyncio.sleep(3)

    async def _run_binance_stream(self) -> None:
        while True:
            try:
                async with websockets.connect(
                    self._binance_ws_url,
                    ping_interval=180,
                    ping_timeout=30,
                    close_timeout=5,
                ) as ws:
                    async for raw in ws:
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            apply_binance_book_ticker(
                                quote=self._binance_quote,
                                message=payload,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "Binance capture reconnect: "
                    f"{exc.__class__.__name__}: {exc}",
                    flush=True,
                )
                await asyncio.sleep(3)

    def _emit_sample(self, window: ResolvedWindowMetadata) -> None:
        self._write_json(
            build_sample_payload(
                window=window,
                recorded_at=utc_now(),
                yes_quote=self._yes_quote,
                no_quote=self._no_quote,
                binance_quote=self._binance_quote,
                sample_interval_secs=self._sample_interval_secs,
                binance_symbol=self._binance_symbol,
            )
        )

    def _write_window_event(self, event_type: str, window: ResolvedWindowMetadata) -> None:
        start_ns = window_start_ns(window)
        self._write_json({
            "event_type": event_type,
            "recorded_at": utc_now(),
            "market_slug": window.slug,
            "condition_id": window.condition_id,
            "window_start_ns": start_ns,
            "window_start_utc": ns_to_utc(start_ns),
            "window_end_ns": window.window_end_ns,
            "window_end_utc": ns_to_utc(window.window_end_ns),
            "pm_yes_token_id": window.yes_token_id,
            "pm_no_token_id": window.no_token_id,
        })

    def _open_writer(self) -> None:
        self._samples_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._samples_path.open("a", encoding="utf-8")

    def _close_writer(self) -> None:
        if self._handle is None:
            return
        self._handle.flush()
        self._handle.close()
        self._handle = None

    def _write_json(self, payload: dict[str, Any]) -> None:
        assert self._handle is not None
        self._handle.write(json.dumps(payload, default=self._json_default, sort_keys=True) + "\n")
        self._handle.flush()

    @staticmethod
    def _json_default(value: object) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
