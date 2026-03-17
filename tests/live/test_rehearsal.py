"""Tests for the Stage 11 live order rehearsal helpers."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from live import rehearsal
from py_clob_client.exceptions import PolyApiException


def _book(*, tick_size: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]):
    return SimpleNamespace(
        tick_size=tick_size,
        bids=[SimpleNamespace(price=price, size=size) for price, size in bids],
        asks=[SimpleNamespace(price=price, size=size) for price, size in asks],
    )


class FakeClient:
    def __init__(self, book):
        self._book = book

    def get_order_book(self, token_id):
        return self._book


class MissingBookClient:
    def get_order_book(self, token_id):
        exc = PolyApiException(error_msg={"error": "No orderbook exists for the requested token id"})
        exc.status_code = 404
        raise exc


def test_event_match_score_prefers_exact_slug_match():
    event = {
        "slug": "bitcoin-above-100k-on-march-20",
        "title": "Bitcoin above 100k on March 20?",
        "question": "Will Bitcoin trade above 100k on March 20?",
    }

    assert rehearsal.event_match_score(event, "bitcoin-above-100k-on-march-20") == 100
    assert rehearsal.event_match_score(event, "bitcoin above 100k") > 0
    assert rehearsal.event_match_score(event, "ethereum 5k") == 0


def test_choose_event_returns_single_match_without_prompt(capsys):
    event = {
        "slug": "bitcoin-above-100k-on-march-20",
        "title": "Bitcoin above 100k on March 20?",
        "markets": [{}],
    }

    selected = rehearsal.choose_event([event], index=None)

    assert selected is event
    assert "Matched event" in capsys.readouterr().out


def test_fetch_book_plan_uses_floor_price_with_min_notional():
    market = {
        "slug": "demo-market",
        "clobTokenIds": '["yes-token", "no-token"]',
        "outcomes": '["Yes", "No"]',
    }
    client = FakeClient(
        _book(
            tick_size="0.01",
            bids=[("0.42", "100"), ("0.41", "100")],
            asks=[("0.43", "100"), ("0.44", "100")],
        )
    )

    plan, book = rehearsal.fetch_book_plan(
        client=client,
        market=market,
        outcome_side="yes",
        notional_usdc=Decimal("5.10"),
        floor_guard_ticks=10,
    )

    assert book.tick_size == "0.01"
    assert plan.token_id == "yes-token"
    assert plan.outcome_label == "Yes"
    assert plan.price == Decimal("0.01")
    assert plan.size == Decimal("510.000000")
    assert plan.notional_usdc == Decimal("5.100000")
    assert plan.best_bid == Decimal("0.42")
    assert plan.best_ask == Decimal("0.43")


def test_fetch_book_plan_normalizes_live_book_edges_before_reading_best_prices():
    market = {
        "slug": "btc-up-or-down",
        "clobTokenIds": '["yes-token", "no-token"]',
        "outcomes": '["Up", "Down"]',
    }
    client = FakeClient(
        _book(
            tick_size="0.01",
            bids=[
                ("0.01", "51864.97"),
                ("0.58", "5691"),
                ("0.59", "600"),
                ("0.57", "9501"),
            ],
            asks=[
                ("0.99", "51830.35"),
                ("0.62", "1660"),
                ("0.61", "10221"),
                ("0.60", "527"),
            ],
        )
    )

    plan, _ = rehearsal.fetch_book_plan(
        client=client,
        market=market,
        outcome_side="yes",
        notional_usdc=Decimal("5.10"),
        floor_guard_ticks=10,
    )

    assert plan.best_bid == Decimal("0.59")
    assert plan.best_ask == Decimal("0.60")
    assert plan.price == Decimal("0.01")


def test_fetch_book_plan_rejects_market_too_close_to_floor():
    market = {
        "slug": "floor-market",
        "clobTokenIds": '["yes-token", "no-token"]',
        "outcomes": '["Yes", "No"]',
    }
    client = FakeClient(
        _book(
            tick_size="0.01",
            bids=[("0.03", "100")],
            asks=[("0.05", "100")],
        )
    )

    with pytest.raises(SystemExit, match="too close to the price floor"):
        rehearsal.fetch_book_plan(
            client=client,
            market=market,
            outcome_side="yes",
            notional_usdc=Decimal("5.10"),
            floor_guard_ticks=10,
        )


def test_fetch_book_plan_reports_missing_live_order_book_cleanly():
    market = {
        "slug": "no-book-market",
        "clobTokenIds": '["yes-token", "no-token"]',
        "outcomes": '["Yes", "No"]',
    }

    with pytest.raises(SystemExit, match="has no live CLOB order book"):
        rehearsal.fetch_book_plan(
            client=MissingBookClient(),
            market=market,
            outcome_side="yes",
            notional_usdc=Decimal("5.10"),
            floor_guard_ticks=10,
        )


def test_extract_order_id_handles_common_response_shapes():
    assert rehearsal.extract_order_id({"orderID": "one"}) == "one"
    assert rehearsal.extract_order_id({"orderId": "two"}) == "two"
    assert rehearsal.extract_order_id({"id": "three"}) == "three"
    assert rehearsal.extract_order_id({"order": {"id": "four"}}) == "four"
    assert rehearsal.extract_order_id({"success": True}) is None


def test_find_order_by_id_matches_common_id_keys():
    orders = [
        {"id": "one"},
        {"orderId": "two"},
        {"orderID": "three"},
    ]

    assert rehearsal.find_order_by_id(orders, "one") == {"id": "one"}
    assert rehearsal.find_order_by_id(orders, "two") == {"orderId": "two"}
    assert rehearsal.find_order_by_id(orders, "three") == {"orderID": "three"}
    assert rehearsal.find_order_by_id(orders, "missing") is None
