#!/usr/bin/env python3
"""Stage 11 live order lifecycle rehearsal: rest one tiny live order, then cancel it."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OpenOrderParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)
from py_clob_client.exceptions import PolyApiException

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from live.env import add_env_file_arg, bootstrap_env_file, load_project_env

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()

HOST = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CHAIN_ID = 137
MIN_NOTIONAL_USDC = Decimal("5.00")
DEFAULT_REHEARSAL_NOTIONAL = Decimal("5.10")
DEFAULT_FLOOR_GUARD_TICKS = 10
DEFAULT_POLL_INTERVAL_SECS = 2.0
DEFAULT_OPEN_TIMEOUT_SECS = 15.0
DEFAULT_CANCEL_TIMEOUT_SECS = 15.0


@dataclass(frozen=True)
class RehearsalOrderPlan:
    token_id: str
    market_slug: str
    outcome_label: str
    price: Decimal
    size: Decimal
    notional_usdc: Decimal
    tick_size: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None


def lookup_event(event_slug: str) -> dict:
    response = requests.get(f"{GAMMA}/events", params={"slug": event_slug}, timeout=10)
    response.raise_for_status()
    events = response.json()
    if not events:
        raise SystemExit(f"No event found for slug: {event_slug}")
    return events[0]


def make_client() -> ClobClient:
    client = ClobClient(
        host=HOST,
        key=os.environ["PRIVATE_KEY"],
        chain_id=CHAIN_ID,
        signature_type=0,
    )
    client.set_api_creds(
        ApiCreds(
            api_key=os.environ["POLYMARKET_API_KEY"],
            api_secret=os.environ["POLYMARKET_API_SECRET"],
            api_passphrase=os.getenv("POLYMARKET_PASSPHRASE") or os.environ["POLYMARKET_API_PASSPHRASE"],
        )
    )
    return client


def lookup_event_markets(event_slug: str) -> list[dict]:
    return lookup_event(event_slug)["markets"]


def search_events(query: str, *, fetch_limit: int = 100, result_limit: int = 10) -> list[dict]:
    response = requests.get(
        f"{GAMMA}/events",
        params={"closed": "false", "limit": str(fetch_limit)},
        timeout=10,
    )
    response.raise_for_status()
    events = response.json()
    if not isinstance(events, list):
        raise SystemExit("Unexpected Gamma events response while searching")

    ranked: list[tuple[int, dict]] = []
    for event in events:
        score = event_match_score(event, query)
        if score > 0:
            ranked.append((score, event))

    ranked.sort(key=lambda item: (-item[0], str(item[1].get("slug", ""))))
    return [event for _, event in ranked[:result_limit]]


def event_match_score(event: dict, query: str) -> int:
    query_text = query.casefold().strip()
    if not query_text:
        return 0

    slug = str(event.get("slug", "")).casefold()
    title = str(event.get("title", "")).casefold()
    question = str(event.get("question", "")).casefold()
    tokens = [token for token in query_text.split() if token]

    if query_text == slug:
        return 100
    if query_text in slug:
        return 80
    if query_text in title:
        return 70
    if query_text in question:
        return 60

    haystacks = [slug, title, question]
    score = 0
    for token in tokens:
        if token in slug:
            score += 20
        elif token in title:
            score += 15
        elif token in question:
            score += 10
    if tokens and all(any(token in haystack for haystack in haystacks) for token in tokens):
        score += 10
    return score


def choose_event(events: list[dict], index: int | None) -> dict:
    if not events:
        raise SystemExit("No candidate events found for the supplied search")

    if index is not None:
        try:
            return events[index]
        except IndexError as exc:
            raise SystemExit(f"Event index out of range: {index}") from exc

    if len(events) == 1:
        event = events[0]
        print(f"Matched event: {event.get('title', event.get('slug', '(untitled)'))}")
        print(f"  slug={event.get('slug', '?')}")
        return event

    print("\nCandidate events:")
    for idx, event in enumerate(events):
        title = event.get("title") or event.get("slug", "(untitled)")
        slug = event.get("slug", "?")
        market_count = len(event.get("markets", []))
        print(f"  [{idx}] {title}")
        print(f"      slug={slug} markets={market_count}")

    try:
        selected = int(input("\nPick event index: "))
    except ValueError as exc:
        raise SystemExit("Invalid event index input") from exc
    try:
        return events[selected]
    except IndexError as exc:
        raise SystemExit(f"Event index out of range: {selected}") from exc


def choose_market(markets: list[dict], index: int | None) -> dict:
    if index is not None:
        try:
            return markets[index]
        except IndexError as exc:
            raise SystemExit(f"Market index out of range: {index}") from exc

    print("\nSub-markets:")
    for idx, market in enumerate(markets):
        prices = json.loads(market.get("outcomePrices", "[]"))
        print(f"  [{idx}] {market['slug']}")
        if prices:
            yes_price = prices[0]
            no_price = prices[1] if len(prices) > 1 else "?"
            print(f"      YES={yes_price} NO={no_price}")

    try:
        selected = int(input("\nPick market index: "))
    except ValueError as exc:
        raise SystemExit("Invalid market index input") from exc
    try:
        return markets[selected]
    except IndexError as exc:
        raise SystemExit(f"Market index out of range: {selected}") from exc


def fetch_book_plan(
    *,
    client: ClobClient,
    market: dict,
    outcome_side: str,
    notional_usdc: Decimal,
    floor_guard_ticks: int,
) -> tuple[RehearsalOrderPlan, object]:
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    outcomes = json.loads(market.get("outcomes", "[]"))
    token_index = 0 if outcome_side == "yes" else 1
    try:
        token_id = str(token_ids[token_index])
    except IndexError as exc:
        raise SystemExit(f"Selected outcome_side={outcome_side} not available for {market['slug']}") from exc

    outcome_label = outcomes[token_index] if len(outcomes) > token_index else outcome_side.upper()
    try:
        book = client.get_order_book(token_id)
    except PolyApiException as exc:
        if exc.status_code == 404:
            raise SystemExit(
                "Selected market has no live CLOB order book for the requested token: "
                f"market={market['slug']} outcome={outcome_label} token_id={token_id}. "
                "Try a different --market-index or use --search to choose another event."
            ) from exc
        raise SystemExit(
            "Failed to fetch live CLOB order book for "
            f"market={market['slug']} outcome={outcome_label}: {exc}"
        ) from exc
    tick_size = Decimal(str(book.tick_size))
    bids = _sorted_book_side(book.bids, reverse=True)
    asks = _sorted_book_side(book.asks, reverse=False)
    best_bid = _book_price(bids[0].price) if bids else None
    best_ask = _book_price(asks[0].price) if asks else None
    if best_ask is None:
        raise SystemExit("No ask side in live order book; refusing to rehearse on this market")

    price = tick_size
    floor_guard = tick_size * (floor_guard_ticks + 1)
    if best_ask <= floor_guard:
        raise SystemExit(
            f"Best ask {best_ask} is too close to the price floor {price}; "
            "choose another market for a no-fill rehearsal"
        )

    effective_notional = max(notional_usdc, MIN_NOTIONAL_USDC)
    size = (effective_notional / price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    actual_notional = (size * price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    if actual_notional < MIN_NOTIONAL_USDC:
        raise SystemExit(
            f"Computed resting order notional {actual_notional} USDC is below the platform minimum"
        )

    return (
        RehearsalOrderPlan(
            token_id=token_id,
            market_slug=market["slug"],
            outcome_label=outcome_label,
            price=price,
            size=size,
            notional_usdc=actual_notional,
            tick_size=tick_size,
            best_bid=best_bid,
            best_ask=best_ask,
        ),
        book,
    )


def submit_resting_order(client: ClobClient, plan: RehearsalOrderPlan) -> tuple[str, dict]:
    order = client.create_order(
        OrderArgs(
            token_id=plan.token_id,
            price=float(plan.price),
            size=float(plan.size),
            side="BUY",
        ),
        options=PartialCreateOrderOptions(tick_size=_tick_size_literal(plan.tick_size)),
    )
    response = client.post_order(order, OrderType.GTC, post_only=True)
    order_id = extract_order_id(response)
    if not response.get("success"):
        raise SystemExit(f"Order was not accepted: {response}")
    if not order_id:
        raise SystemExit(f"Could not extract order id from response: {response}")
    return order_id, response


def wait_for_open_order(
    client: ClobClient,
    *,
    order_id: str,
    token_id: str,
    timeout_secs: float,
    poll_interval_secs: float,
) -> dict:
    deadline = time.monotonic() + timeout_secs
    params = OpenOrderParams(asset_id=token_id)
    while time.monotonic() < deadline:
        open_orders = client.get_orders(params)
        match = find_order_by_id(open_orders, order_id)
        if match is not None:
            return match
        time.sleep(poll_interval_secs)
    raise SystemExit(f"Timed out waiting for live order to appear as open: {order_id}")


def cancel_and_wait(
    client: ClobClient,
    *,
    order_id: str,
    token_id: str,
    timeout_secs: float,
    poll_interval_secs: float,
) -> dict | None:
    cancel_response = client.cancel(order_id)
    print(f"Cancel response: {cancel_response}")
    deadline = time.monotonic() + timeout_secs
    params = OpenOrderParams(asset_id=token_id)
    while time.monotonic() < deadline:
        open_orders = client.get_orders(params)
        if find_order_by_id(open_orders, order_id) is None:
            try:
                return client.get_order(order_id)
            except Exception:
                return None
        time.sleep(poll_interval_secs)
    raise SystemExit(f"Timed out waiting for order cancel confirmation: {order_id}")


def sync_conditional_balance(client: ClobClient, token_id: str) -> float:
    params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
    client.update_balance_allowance(params)
    balance = client.get_balance_allowance(params)
    return int(balance.get("balance", 0)) / 1e6


def extract_order_id(response: dict) -> str | None:
    for key in ("orderID", "orderId", "id"):
        value = response.get(key)
        if value:
            return str(value)
    order = response.get("order")
    if isinstance(order, dict):
        for key in ("id", "orderID", "orderId"):
            value = order.get(key)
            if value:
                return str(value)
    return None


def find_order_by_id(orders: list[dict], order_id: str) -> dict | None:
    for order in orders:
        for key in ("id", "orderID", "orderId"):
            value = order.get(key)
            if value is not None and str(value) == order_id:
                return order
    return None


def print_book(market: dict, plan: RehearsalOrderPlan, book, levels: int) -> None:
    bids = _sorted_book_side(book.bids, reverse=True)
    asks = _sorted_book_side(book.asks, reverse=False)
    print(f"\nMarket       : {market['slug']}")
    print(f"Outcome      : {plan.outcome_label}")
    print(f"Token        : {plan.token_id}")
    print(f"Tick size    : {plan.tick_size}")
    print(f"Best bid     : {plan.best_bid if plan.best_bid is not None else 'n/a'}")
    print(f"Best ask     : {plan.best_ask if plan.best_ask is not None else 'n/a'}")
    print(f"Rehearsal BUY: price={plan.price} size={plan.size} notional={plan.notional_usdc} USDC")
    print("\nTop book levels:")
    print("  Asks:")
    for level in asks[:levels]:
        print(f"    px={level.price} size={level.size}")
    print("  Bids:")
    for level in bids[:levels]:
        print(f"    px={level.price} size={level.size}")


def _book_price(value: str | float) -> Decimal:
    return Decimal(str(value))


def _sorted_book_side(levels, *, reverse: bool):
    return sorted(levels, key=lambda level: _book_price(level.price), reverse=reverse)


def _tick_size_literal(tick_size: Decimal) -> str:
    return format(tick_size.normalize(), "f")


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Stage 11 live order lifecycle rehearsal")
    add_env_file_arg(parser)
    selector_group = parser.add_mutually_exclusive_group(required=True)
    selector_group.add_argument("--event", help="Polymarket event slug")
    selector_group.add_argument("--search", help="Search active Polymarket events by title or slug")
    parser.add_argument("--event-index", type=int, default=None, help="Search-result event index")
    parser.add_argument("--market-index", type=int, default=None, help="Sub-market index within the event")
    parser.add_argument("--outcome-side", choices=("yes", "no"), default="yes")
    parser.add_argument(
        "--amount-usdc",
        type=Decimal,
        default=DEFAULT_REHEARSAL_NOTIONAL,
        help="Target rehearsal notional in USDC (default: 5.10)",
    )
    parser.add_argument(
        "--floor-guard-ticks",
        type=int,
        default=DEFAULT_FLOOR_GUARD_TICKS,
        help="Require best ask to sit at least this many ticks above the price floor (default: 10)",
    )
    parser.add_argument("--show-levels", type=int, default=5, help="How many bid/ask levels to print")
    parser.add_argument("--open-timeout-secs", type=float, default=DEFAULT_OPEN_TIMEOUT_SECS)
    parser.add_argument("--cancel-timeout-secs", type=float, default=DEFAULT_CANCEL_TIMEOUT_SECS)
    parser.add_argument("--poll-interval-secs", type=float, default=DEFAULT_POLL_INTERVAL_SECS)
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt and execute immediately")
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_parser()
    args = parser.parse_args(argv)

    client = make_client()
    if args.event is not None:
        event = lookup_event(args.event)
    else:
        event = choose_event(search_events(args.search), args.event_index)
    markets = event["markets"]
    market = choose_market(markets, args.market_index)
    plan, book = fetch_book_plan(
        client=client,
        market=market,
        outcome_side=args.outcome_side,
        notional_usdc=args.amount_usdc,
        floor_guard_ticks=args.floor_guard_ticks,
    )
    print_book(market, plan, book, args.show_levels)

    if not args.yes:
        confirm = input("\nSubmit this resting BUY rehearsal and then cancel it? [y/N]: ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    order_id, submit_response = submit_resting_order(client, plan)
    print(f"\nSubmit response: {submit_response}")
    print(f"Order id: {order_id}")

    open_order = wait_for_open_order(
        client,
        order_id=order_id,
        token_id=plan.token_id,
        timeout_secs=args.open_timeout_secs,
        poll_interval_secs=args.poll_interval_secs,
    )
    print(f"Open confirmation: {open_order}")

    final_order = cancel_and_wait(
        client,
        order_id=order_id,
        token_id=plan.token_id,
        timeout_secs=args.cancel_timeout_secs,
        poll_interval_secs=args.poll_interval_secs,
    )
    if final_order is not None:
        print(f"Final order state: {final_order}")

    conditional_balance = sync_conditional_balance(client, plan.token_id)
    print(f"Conditional balance after cancel: {conditional_balance:.6f} shares")
    if conditional_balance != 0.0:
        raise SystemExit(
            f"Unexpected non-zero conditional balance after cancel: {conditional_balance:.6f} shares"
        )

    print("\nStage 11 rehearsal passed: order opened, canceled, and left no token balance.")


if __name__ == "__main__":
    main()
