from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from live.polymarket_history import (
    AGGREGATED_HISTORY_FILENAME,
    RAW_ACTIVITY_FILENAME,
    build_public_history,
    build_aggregated_history,
    write_public_history,
)


def _mock_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@patch("live.polymarket_history.requests.get")
def test_build_public_history_paginates_activity(mock_get):
    mock_get.side_effect = [
        _mock_response(
            [
                {"type": "TRADE", "conditionId": "c1", "timestamp": 1000},
                {"type": "REDEEM", "conditionId": "c1", "timestamp": 1001},
            ]
        ),
        _mock_response(
            [
                {"type": "TRADE", "conditionId": "c2", "timestamp": 1002},
            ]
        ),
    ]

    bundle = build_public_history(user="0xabc", page_size=2)

    assert len(bundle.activity) == 3
    assert len(bundle.raw_activity_rows) == 3
    call_params = [call.kwargs["params"] for call in mock_get.call_args_list]
    assert call_params == [
        {"user": "0xabc", "limit": 2, "offset": 0},
        {"user": "0xabc", "limit": 2, "offset": 2},
    ]


def test_build_aggregated_history_uses_trade_and_redeem_usdc_cashflows():
    rows = [
        {
            "timestamp": "2026-03-29T12:00:00+00:00",
            "type": "TRADE",
            "condition_id": "condition-1",
            "asset": "asset-1",
            "outcome": "Up",
            "side": "BUY",
            "price": 0.91,
            "size": 5.6,
            "usdc_size": 5.096,
            "transaction_hash": "0xbuy",
            "event_slug": "btc-updown-15m-1",
            "title": "BTC Up or Down",
        },
        {
            "timestamp": "2026-03-29T12:10:00+00:00",
            "type": "REDEEM",
            "condition_id": "condition-1",
            "asset": "",
            "outcome": "",
            "side": "",
            "price": 0.0,
            "size": 5.6,
            "usdc_size": 5.6,
            "transaction_hash": "0xredeem",
            "event_slug": "btc-updown-15m-1",
            "title": "BTC Up or Down",
        },
    ]

    aggregated = build_aggregated_history(rows)

    assert len(aggregated) == 1
    row = aggregated[0]
    assert row["condition_id"] == "condition-1"
    assert row["buy_count"] == 1
    assert row["sell_count"] == 0
    assert row["redeem_count"] == 1
    assert row["buy_usdc"] == pytest.approx(5.096)
    assert row["redeem_usdc"] == pytest.approx(5.6)
    assert row["pnl_usdc"] == pytest.approx(0.504)
    assert row["close_type"] == "redeemed"


def test_build_aggregated_history_handles_loss_when_redeem_usdc_is_zero():
    rows = [
        {
            "timestamp": "2026-03-29T12:00:00+00:00",
            "type": "TRADE",
            "condition_id": "condition-1",
            "asset": "asset-1",
            "outcome": "Up",
            "side": "BUY",
            "price": 0.94,
            "size": 5.42,
            "usdc_size": 5.0948,
            "transaction_hash": "0xbuy",
            "event_slug": "btc-updown-15m-1",
            "title": "BTC Up or Down",
        },
        {
            "timestamp": "2026-03-29T12:10:00+00:00",
            "type": "REDEEM",
            "condition_id": "condition-1",
            "asset": "",
            "outcome": "",
            "side": "",
            "price": 0.0,
            "size": 0.0,
            "usdc_size": 0.0,
            "transaction_hash": "0xredeem",
            "event_slug": "btc-updown-15m-1",
            "title": "BTC Up or Down",
        },
    ]

    aggregated = build_aggregated_history(rows)

    assert len(aggregated) == 1
    assert aggregated[0]["pnl_usdc"] == pytest.approx(-5.0948)
    assert aggregated[0]["close_type"] == "redeemed"


@patch("live.polymarket_history.requests.get")
def test_write_public_history_creates_only_two_csv_outputs(mock_get, tmp_path: Path):
    mock_get.side_effect = [
        _mock_response(
            [
                {
                    "proxyWallet": "0xabc",
                    "type": "TRADE",
                    "conditionId": "condition-1",
                    "asset": "asset-1",
                    "outcome": "Up",
                    "side": "BUY",
                    "price": 0.91,
                    "size": 5.6,
                    "usdcSize": 5.096,
                    "timestamp": 1_700_000_001,
                    "transactionHash": "0xbuy",
                    "eventSlug": "btc-updown-15m-1",
                    "title": "BTC Up or Down",
                },
            ]
        ),
    ]

    # Simulate stale artifacts from the old version.
    for legacy_name in (
        "pm_trades.json",
        "pm_history_timeline.csv",
        "pm_settlement_history.csv",
    ):
        (tmp_path / legacy_name).write_text("stale", encoding="utf-8")

    bundle = build_public_history(user="0xabc", page_size=50)
    write_public_history(bundle, tmp_path)

    assert {path.name for path in tmp_path.iterdir()} == {
        RAW_ACTIVITY_FILENAME,
        AGGREGATED_HISTORY_FILENAME,
    }

    with (tmp_path / RAW_ACTIVITY_FILENAME).open("r", encoding="utf-8", newline="") as handle:
        raw_rows = list(csv.reader(handle))
    with (tmp_path / AGGREGATED_HISTORY_FILENAME).open("r", encoding="utf-8", newline="") as handle:
        aggregated_rows = list(csv.reader(handle))

    assert raw_rows[0][0] == "timestamp"
    assert aggregated_rows[0][0] == "condition_id"
