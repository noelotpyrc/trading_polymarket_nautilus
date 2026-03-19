from decimal import Decimal
import logging

from live.fill_rehearsal import (
    BookSnapshot,
    FilledEntry,
    MinuteMidRecorder,
    manage_entry,
    manage_exit,
    choose_entry_price,
    choose_exit_price,
    midpoint_from_snapshot,
    profitable_exit_impossible,
    round_up_to_tick,
    size_from_notional,
)


def test_choose_entry_price_requires_strictly_above_threshold():
    snapshot = BookSnapshot(
        tick_size=Decimal("0.01"),
        best_bid=Decimal("0.90"),
        best_ask=Decimal("0.91"),
    )

    assert choose_entry_price(snapshot=snapshot, threshold=Decimal("0.90")) is None


def test_choose_entry_price_uses_best_bid_when_threshold_is_met():
    snapshot = BookSnapshot(
        tick_size=Decimal("0.01"),
        best_bid=Decimal("0.91"),
        best_ask=Decimal("0.92"),
    )

    assert choose_entry_price(snapshot=snapshot, threshold=Decimal("0.90")) == Decimal("0.91")


def test_choose_exit_price_never_goes_below_profitable_floor():
    snapshot = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.918"),
        best_ask=Decimal("0.919"),
    )

    assert choose_exit_price(snapshot=snapshot, profitable_floor=Decimal("0.921")) == Decimal("0.921")


def test_choose_exit_price_can_join_higher_best_ask():
    snapshot = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.925"),
        best_ask=Decimal("0.928"),
    )

    assert choose_exit_price(snapshot=snapshot, profitable_floor=Decimal("0.921")) == Decimal("0.928")


def test_profitable_exit_impossible_when_floor_exceeds_one():
    assert profitable_exit_impossible(Decimal("1.008")) is True
    assert profitable_exit_impossible(Decimal("1.000")) is False


def test_round_up_to_tick_uses_live_tick_increment():
    assert round_up_to_tick(Decimal("0.9211"), Decimal("0.001")) == Decimal("0.922")
    assert round_up_to_tick(Decimal("0.9211"), Decimal("0.01")) == Decimal("0.93")


def test_size_from_notional_rounds_down_to_share_precision():
    assert size_from_notional(Decimal("5.10"), Decimal("0.91")) == Decimal("5.604395")


def test_midpoint_from_snapshot_uses_best_bid_and_best_ask():
    snapshot = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.995"),
        best_ask=Decimal("0.996"),
    )

    assert midpoint_from_snapshot(snapshot) == Decimal("0.995500")


def test_minute_mid_recorder_keeps_latest_snapshot_within_each_minute(caplog):
    recorder = MinuteMidRecorder()
    logger = logging.getLogger("test_fill_rehearsal")
    snapshot_a = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.985"),
        best_ask=Decimal("0.987"),
    )
    snapshot_b = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.995"),
        best_ask=Decimal("0.996"),
    )

    with caplog.at_level(logging.INFO):
        recorder.observe(
            window_slug="btc-updown-15m-1",
            observed_at_ns=1773865745 * 1_000_000_000,
            snapshot=snapshot_a,
            logger=logger,
        )
        recorder.observe(
            window_slug="btc-updown-15m-1",
            observed_at_ns=1773865766 * 1_000_000_000,
            snapshot=snapshot_b,
            logger=logger,
        )
        recorder.flush_window(window_slug="btc-updown-15m-1", logger=logger)

    rows = recorder.serialized_rows()
    assert rows == [
        {
            "window_slug": "btc-updown-15m-1",
            "minute_start": "2026-03-18 20:29:00 UTC",
            "minute_start_ns": 1773865740 * 1_000_000_000,
            "best_bid": "0.995",
            "best_ask": "0.996",
            "midpoint": "0.995500",
        }
    ]
    assert "Minute midpoint" in caplog.text


def test_minute_mid_recorder_emits_previous_minute_on_rollover(caplog):
    recorder = MinuteMidRecorder()
    logger = logging.getLogger("test_fill_rehearsal_rollover")
    snapshot_a = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.975"),
        best_ask=Decimal("0.977"),
    )
    snapshot_b = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.995"),
        best_ask=Decimal("0.996"),
    )

    with caplog.at_level(logging.INFO):
        recorder.observe(
            window_slug="btc-updown-15m-2",
            observed_at_ns=1773865690 * 1_000_000_000,
            snapshot=snapshot_a,
            logger=logger,
        )
        recorder.observe(
            window_slug="btc-updown-15m-2",
            observed_at_ns=1773865766 * 1_000_000_000,
            snapshot=snapshot_b,
            logger=logger,
        )
        recorder.flush_window(window_slug="btc-updown-15m-2", logger=logger)

    rows = recorder.serialized_rows()
    assert rows == [
        {
            "window_slug": "btc-updown-15m-2",
            "minute_start": "2026-03-18 20:28:00 UTC",
            "minute_start_ns": 1773865680 * 1_000_000_000,
            "best_bid": "0.975",
            "best_ask": "0.977",
            "midpoint": "0.976000",
        },
        {
            "window_slug": "btc-updown-15m-2",
            "minute_start": "2026-03-18 20:29:00 UTC",
            "minute_start_ns": 1773865740 * 1_000_000_000,
            "best_bid": "0.995",
            "best_ask": "0.996",
            "midpoint": "0.995500",
        },
    ]


def test_manage_entry_does_not_resubmit_after_fill_when_balance_sync_lags(monkeypatch):
    logger = logging.getLogger("test_manage_entry_fill_latch")
    recorder = MinuteMidRecorder()
    snapshot = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.998"),
        best_ask=Decimal("0.999"),
    )
    submit_calls = []
    cancel_calls = []
    sync_sizes = iter([Decimal("0"), Decimal("5.11")])
    monotonic_values = iter([100.0, 102.0, 103.0, 103.1, 103.2])
    now_values = iter([
        50_000_000_000,
        50_000_000_000,
        52_000_000_000,
        52_000_000_000,
    ])

    def fake_now_ns():
        try:
            return next(now_values)
        except StopIteration:
            return 52_000_000_000

    monkeypatch.setattr("live.fill_rehearsal._now_ns", fake_now_ns)
    monkeypatch.setattr("live.fill_rehearsal.fetch_book_snapshot", lambda client, token_id: snapshot)
    monkeypatch.setattr(
        "live.fill_rehearsal.submit_limit_order",
        lambda client, **kwargs: (submit_calls.append(kwargs) or ("order-1", {"success": True})),
    )
    monkeypatch.setattr(
        "live.fill_rehearsal.safe_get_order",
        lambda client, order_id: {
            "status": "MATCHED",
            "size_matched": "5.11",
            "price": "0.998",
            "associate_trades": [],
        },
    )
    monkeypatch.setattr(
        "live.fill_rehearsal.cancel_if_open",
        lambda client, order_id, token_id, **kwargs: cancel_calls.append(order_id),
    )
    monkeypatch.setattr("live.fill_rehearsal.sync_conditional_balance", lambda client, token_id: next(sync_sizes))
    monkeypatch.setattr("live.fill_rehearsal.resolve_average_fill_price", lambda client, payload: Decimal("0.998"))
    monkeypatch.setattr("live.fill_rehearsal.time.sleep", lambda secs: None)
    monkeypatch.setattr("live.fill_rehearsal.time.monotonic", lambda: next(monotonic_values))

    result = manage_entry(
        client=object(),
        token_id="yes-token",
        window_slug="btc-updown-15m-1",
        window_end_ns=100_000_000_000,
        amount_usdc=Decimal("5.10"),
        entry_threshold=Decimal("0.90"),
        entry_window_secs=60,
        entry_cancel_before_expiry_secs=10,
        reprice_interval_secs=10,
        poll_interval_secs=0.1,
        logger=logger,
        minute_mid_recorder=recorder,
    )

    assert result.size == Decimal("5.11")
    assert result.average_price == Decimal("0.998")
    assert result.source_order_id == "order-1"
    assert len(submit_calls) == 1
    assert cancel_calls == ["order-1"]


def test_manage_entry_latches_partial_fill_during_reprice_cancel(monkeypatch):
    logger = logging.getLogger("test_manage_entry_reprice_partial")
    recorder = MinuteMidRecorder()
    snapshots = iter(
        [
            BookSnapshot(
                tick_size=Decimal("0.01"),
                best_bid=Decimal("0.98"),
                best_ask=Decimal("0.99"),
            ),
            BookSnapshot(
                tick_size=Decimal("0.01"),
                best_bid=Decimal("0.99"),
                best_ask=None,
            ),
        ]
    )
    submit_calls = []
    cancel_calls = []
    monotonic_values = iter([100.0, 111.0, 111.1])
    now_values = iter([
        50_000_000_000,
        50_000_000_000,
        52_000_000_000,
    ])

    def fake_now_ns():
        try:
            return next(now_values)
        except StopIteration:
            return 52_000_000_000

    monkeypatch.setattr("live.fill_rehearsal._now_ns", fake_now_ns)
    monkeypatch.setattr("live.fill_rehearsal.fetch_book_snapshot", lambda client, token_id: next(snapshots))
    monkeypatch.setattr(
        "live.fill_rehearsal.submit_limit_order",
        lambda client, **kwargs: (submit_calls.append(kwargs) or ("order-1", {"success": True})),
    )
    monkeypatch.setattr(
        "live.fill_rehearsal.safe_get_order",
        lambda client, order_id: {
            "status": "LIVE",
            "size_matched": "0",
            "price": "0.98",
            "associate_trades": [],
        },
    )
    monkeypatch.setattr(
        "live.fill_rehearsal.cancel_if_open",
        lambda client, order_id, token_id, **kwargs: (
            cancel_calls.append(order_id)
            or {
                "status": "CANCELED",
                "size_matched": "1.98",
                "price": "0.98",
                "associate_trades": [],
            }
        ),
    )
    monkeypatch.setattr("live.fill_rehearsal.sync_conditional_balance", lambda client, token_id: Decimal("1.98"))
    monkeypatch.setattr("live.fill_rehearsal.resolve_average_fill_price", lambda client, payload: Decimal("0.98"))
    monkeypatch.setattr("live.fill_rehearsal.time.sleep", lambda secs: None)
    def fake_monotonic():
        try:
            return next(monotonic_values)
        except StopIteration:
            return 111.1

    monkeypatch.setattr("live.fill_rehearsal.time.monotonic", fake_monotonic)

    result = manage_entry(
        client=object(),
        token_id="yes-token",
        window_slug="btc-updown-15m-2",
        window_end_ns=100_000_000_000,
        amount_usdc=Decimal("5.10"),
        entry_threshold=Decimal("0.90"),
        entry_window_secs=60,
        entry_cancel_before_expiry_secs=10,
        reprice_interval_secs=10,
        poll_interval_secs=0.1,
        logger=logger,
        minute_mid_recorder=recorder,
    )

    assert result.size == Decimal("1.98")
    assert result.average_price == Decimal("0.98")
    assert result.source_order_id == "order-1"
    assert len(submit_calls) == 1
    assert cancel_calls == ["order-1"]


def test_manage_exit_falls_back_immediately_when_profitable_exit_is_impossible(monkeypatch):
    logger = logging.getLogger("test_manage_exit_profit_cap")
    recorder = MinuteMidRecorder()
    snapshot = BookSnapshot(
        tick_size=Decimal("0.001"),
        best_bid=Decimal("0.999"),
        best_ask=None,
    )
    submit_calls = []

    monkeypatch.setattr("live.fill_rehearsal.sync_conditional_balance", lambda client, token_id: Decimal("5.11"))
    monkeypatch.setattr("live.fill_rehearsal.fetch_book_snapshot", lambda client, token_id: snapshot)
    monkeypatch.setattr("live.fill_rehearsal.submit_limit_order", lambda client, **kwargs: submit_calls.append(kwargs))
    monkeypatch.setattr("live.fill_rehearsal.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("live.fill_rehearsal._now_ns", lambda: 1773870291000000000)

    result = manage_exit(
        client=object(),
        token_id="yes-token",
        window_slug="btc-updown-15m-1",
        filled_entry=FilledEntry(
            size=Decimal("5.11"),
            average_price=Decimal("0.998"),
            source_order_id="order-1",
        ),
        profit_buffer_usd=Decimal("0.01"),
        exit_attempt_window_secs=30,
        reprice_interval_secs=10,
        poll_interval_secs=0.1,
        logger=logger,
        minute_mid_recorder=recorder,
    )

    assert result is False
    assert submit_calls == []


def test_manage_exit_falls_back_when_remaining_size_is_below_market_minimum(monkeypatch):
    logger = logging.getLogger("test_manage_exit_min_size")
    recorder = MinuteMidRecorder()
    snapshot = BookSnapshot(
        tick_size=Decimal("0.01"),
        best_bid=Decimal("0.94"),
        best_ask=Decimal("0.95"),
        min_order_size=Decimal("5"),
    )
    submit_calls = []

    monkeypatch.setattr("live.fill_rehearsal.sync_conditional_balance", lambda client, token_id: Decimal("2.50"))
    monkeypatch.setattr("live.fill_rehearsal.fetch_book_snapshot", lambda client, token_id: snapshot)
    monkeypatch.setattr("live.fill_rehearsal.submit_limit_order", lambda client, **kwargs: submit_calls.append(kwargs))
    monkeypatch.setattr("live.fill_rehearsal.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("live.fill_rehearsal._now_ns", lambda: 1773870291000000000)

    result = manage_exit(
        client=object(),
        token_id="yes-token",
        window_slug="btc-updown-15m-3",
        filled_entry=FilledEntry(
            size=Decimal("2.50"),
            average_price=Decimal("0.91"),
            source_order_id="order-1",
        ),
        profit_buffer_usd=Decimal("0.01"),
        exit_attempt_window_secs=30,
        reprice_interval_secs=10,
        poll_interval_secs=0.1,
        logger=logger,
        minute_mid_recorder=recorder,
    )

    assert result is False
    assert submit_calls == []
