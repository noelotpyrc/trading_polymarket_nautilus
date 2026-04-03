from __future__ import annotations

from datetime import datetime, timedelta, timezone

from live.market_data_capture import (
    QuoteState,
    apply_binance_book_ticker,
    apply_pm_quote_message,
    build_sample_payload,
    select_active_window,
    window_start_ns,
)
from live.market_metadata import ResolvedWindowMetadata


def _window(*, slug_ts: int, end_ts: int) -> ResolvedWindowMetadata:
    return ResolvedWindowMetadata(
        slug=f"btc-updown-15m-{slug_ts}",
        condition_id=f"cond-{slug_ts}",
        window_end_ns=end_ts * 1_000_000_000,
        yes_token_id=f"yes-{slug_ts}",
        no_token_id=f"no-{slug_ts}",
        yes_outcome_label="Up",
        no_outcome_label="Down",
        selected_outcome_side="yes",
    )


def test_window_start_ns_uses_slug_suffix() -> None:
    window = _window(slug_ts=1_775_051_100, end_ts=1_775_052_000)
    assert window_start_ns(window) == 1_775_051_100 * 1_000_000_000


def test_select_active_window_returns_first_not_ended() -> None:
    first = _window(slug_ts=100, end_ts=200)
    second = _window(slug_ts=200, end_ts=300)

    assert select_active_window([first, second], 150 * 1_000_000_000) == first
    assert select_active_window([first, second], 250 * 1_000_000_000) == second
    assert select_active_window([first, second], 300 * 1_000_000_000) is None


def test_apply_pm_quote_message_updates_best_bid_ask() -> None:
    yes = QuoteState()
    no = QuoteState()

    updated = apply_pm_quote_message(
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_quote=yes,
        no_quote=no,
        message={
            "event_type": "best_bid_ask",
            "asset_id": "yes-token",
            "best_bid": "0.91",
            "best_ask": "0.92",
            "timestamp": 1_775_052_000_000,
        },
    )

    assert updated is True
    assert yes.bid == 0.91
    assert yes.ask == 0.92
    assert no.bid is None


def test_apply_pm_quote_message_ignores_book_for_top_of_book_state() -> None:
    yes = QuoteState()
    no = QuoteState()

    updated = apply_pm_quote_message(
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_quote=yes,
        no_quote=no,
        message={
            "event_type": "book",
            "asset_id": "no-token",
            "bids": [{"price": "0.08", "size": "120"}],
            "asks": [{"price": "0.09", "size": "130"}],
            "timestamp": 1_775_052_001_000,
        },
    )

    assert updated is True
    assert no.bid is None
    assert no.ask is None
    assert no.bid_size is None
    assert no.ask_size is None


def test_book_message_does_not_clobber_best_bid_ask_quote() -> None:
    yes = QuoteState()
    no = QuoteState()

    first = apply_pm_quote_message(
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_quote=yes,
        no_quote=no,
        message={
            "event_type": "best_bid_ask",
            "asset_id": "yes-token",
            "best_bid": "0.95",
            "best_ask": "0.96",
            "best_bid_size": "110",
            "best_ask_size": "120",
            "timestamp": 1_775_052_000_000,
        },
    )
    second = apply_pm_quote_message(
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_quote=yes,
        no_quote=no,
        message={
            "event_type": "book",
            "asset_id": "yes-token",
            "bids": [{"price": "0.01", "size": "9000"}],
            "asks": [{"price": "0.99", "size": "8000"}],
            "timestamp": 1_775_052_001_000,
        },
    )

    assert first is True
    assert second is True
    assert yes.bid == 0.95
    assert yes.ask == 0.96
    assert yes.bid_size == 110.0
    assert yes.ask_size == 120.0


def test_apply_binance_book_ticker_updates_quote() -> None:
    quote = QuoteState()

    updated = apply_binance_book_ticker(
        quote=quote,
        message={
            "e": "bookTicker",
            "s": "BTCUSDT",
            "b": "68147.2",
            "B": "5.4",
            "a": "68147.3",
            "A": "6.1",
            "E": 1_775_052_002_000,
        },
    )

    assert updated is True
    assert quote.bid == 68147.2
    assert quote.ask == 68147.3
    assert quote.bid_size == 5.4
    assert quote.ask_size == 6.1


def test_build_sample_payload_includes_yes_no_and_binance() -> None:
    window = _window(slug_ts=1_775_051_100, end_ts=1_775_052_000)
    recorded_at = datetime(2026, 4, 1, 14, 0, 0, tzinfo=timezone.utc)

    yes = QuoteState(bid=0.91, ask=0.92, bid_size=10.0, ask_size=12.0, source_ts=recorded_at)
    no = QuoteState(bid=0.08, ask=0.09, bid_size=15.0, ask_size=11.0, source_ts=recorded_at)
    binance = QuoteState(
        bid=68147.2,
        ask=68147.3,
        bid_size=5.0,
        ask_size=6.0,
        source_ts=recorded_at - timedelta(seconds=1),
    )

    payload = build_sample_payload(
        window=window,
        recorded_at=recorded_at,
        yes_quote=yes,
        no_quote=no,
        binance_quote=binance,
        sample_interval_secs=5.0,
        binance_symbol="BTCUSDT",
    )

    assert payload["event_type"] == "sample"
    assert payload["market_slug"] == window.slug
    assert payload["pm_yes"]["bid"] == 0.91
    assert payload["pm_no"]["ask"] == 0.09
    assert payload["binance"]["mid"] == (68147.2 + 68147.3) / 2
    assert payload["sample_interval_secs"] == 5.0
