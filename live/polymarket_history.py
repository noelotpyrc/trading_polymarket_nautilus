#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests


DATA_API = "https://data-api.polymarket.com"
RAW_ACTIVITY_FILENAME = "pm_activity.csv"
AGGREGATED_HISTORY_FILENAME = "pm_aggregated_history.csv"
_LEGACY_ARTIFACTS = {
    "pm_trades.json",
    "pm_activity.json",
    "pm_closed_positions.json",
    "pm_history_summary.json",
    "pm_history_timeline.jsonl",
    "pm_history_timeline.csv",
    "pm_settlement_summary.json",
    "pm_settlement_history.jsonl",
    "pm_settlement_history.csv",
}


@dataclass
class PublicHistoryBundle:
    user: str
    activity: list[dict[str, Any]]
    raw_activity_rows: list[dict[str, Any]]
    aggregated_history_rows: list[dict[str, Any]]

    def summary(self) -> dict[str, Any]:
        trade_rows = [row for row in self.raw_activity_rows if row["type"] == "TRADE"]
        redeem_rows = [row for row in self.raw_activity_rows if row["type"] == "REDEEM"]
        return {
            "user": self.user,
            "activity_count": len(self.raw_activity_rows),
            "trade_count": len(trade_rows),
            "redeem_count": len(redeem_rows),
            "aggregated_market_count": len(self.aggregated_history_rows),
        }


def _make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch public Polymarket activity and build a simple aggregated market history",
    )
    parser.add_argument("--user", required=True, help="0x-prefixed user/profile address")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write the raw activity CSV and aggregated history CSV",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=500,
        help="Items per paginated request (default: 500)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional safety cap on paginated requests",
    )
    parser.add_argument(
        "--timeout-secs",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds (default: 20)",
    )
    return parser


def _coerce_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    raise TypeError(f"Unsupported payload shape: {type(payload)!r}")


def _fetch_paginated_activity(
    *,
    user: str,
    page_size: int,
    timeout_secs: float,
    max_pages: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    page = 0
    while True:
        page += 1
        if max_pages is not None and page > max_pages:
            break
        response = requests.get(
            f"{DATA_API}/activity",
            params={"user": user, "limit": page_size, "offset": offset},
            timeout=timeout_secs,
        )
        response.raise_for_status()
        batch = _coerce_rows(response.json())
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += len(batch)
    return rows


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat()
        except ValueError:
            pass
        try:
            numeric = float(value)
        except ValueError:
            return value
        value = numeric
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=UTC).isoformat()
    return str(value)


def _normalize_activity_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": _timestamp_to_iso(
            row.get("timestamp")
            or row.get("createdAt")
            or row.get("created_at")
            or row.get("lastUpdate")
            or row.get("last_update")
        ),
        "type": str(row.get("type") or row.get("activityType") or row.get("eventType") or row.get("action") or "").upper(),
        "condition_id": row.get("conditionId") or row.get("condition_id") or row.get("market"),
        "asset": row.get("asset") or row.get("asset_id"),
        "outcome": row.get("outcome"),
        "side": str(row.get("side") or "").upper(),
        "price": _safe_float(row.get("price")),
        "size": _safe_float(row.get("size") or row.get("amount")),
        "usdc_size": _safe_float(row.get("usdcSize") or row.get("usdc_size")),
        "transaction_hash": row.get("transactionHash") or row.get("transaction_hash"),
        "event_slug": row.get("eventSlug") or row.get("event_slug") or row.get("slug"),
        "title": row.get("title"),
    }


def build_aggregated_history(raw_activity_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in raw_activity_rows:
        activity_type = row["type"]
        if activity_type not in {"TRADE", "REDEEM"}:
            continue
        condition_id = str(row.get("condition_id") or "")
        if not condition_id:
            continue
        grouped_row = grouped.setdefault(
            condition_id,
            {
                "condition_id": condition_id,
                "event_slug": row.get("event_slug"),
                "title": row.get("title"),
                "outcomes": set(),
                "asset_ids": set(),
                "first_activity_at": row.get("timestamp"),
                "last_activity_at": row.get("timestamp"),
                "buy_count": 0,
                "sell_count": 0,
                "redeem_count": 0,
                "buy_size": 0.0,
                "sell_size": 0.0,
                "redeem_size": 0.0,
                "buy_usdc": 0.0,
                "sell_usdc": 0.0,
                "redeem_usdc": 0.0,
                "trade_tx_hashes": [],
                "redeem_tx_hashes": [],
            },
        )
        grouped_row["event_slug"] = grouped_row["event_slug"] or row.get("event_slug")
        grouped_row["title"] = grouped_row["title"] or row.get("title")
        timestamp = row.get("timestamp") or ""
        if grouped_row["first_activity_at"] is None or timestamp < grouped_row["first_activity_at"]:
            grouped_row["first_activity_at"] = row.get("timestamp")
        if grouped_row["last_activity_at"] is None or timestamp > grouped_row["last_activity_at"]:
            grouped_row["last_activity_at"] = row.get("timestamp")
        outcome = row.get("outcome")
        if outcome:
            grouped_row["outcomes"].add(str(outcome))
        asset = row.get("asset")
        if asset:
            grouped_row["asset_ids"].add(str(asset))
        size = float(row.get("size") or 0.0)
        usdc_size = float(row.get("usdc_size") or 0.0)
        tx_hash = row.get("transaction_hash")

        if activity_type == "TRADE":
            side = row.get("side")
            if side == "BUY":
                grouped_row["buy_count"] += 1
                grouped_row["buy_size"] += size
                grouped_row["buy_usdc"] += usdc_size
            elif side == "SELL":
                grouped_row["sell_count"] += 1
                grouped_row["sell_size"] += size
                grouped_row["sell_usdc"] += usdc_size
            if tx_hash and tx_hash not in grouped_row["trade_tx_hashes"]:
                grouped_row["trade_tx_hashes"].append(tx_hash)
        elif activity_type == "REDEEM":
            grouped_row["redeem_count"] += 1
            grouped_row["redeem_size"] += size
            grouped_row["redeem_usdc"] += usdc_size
            if tx_hash and tx_hash not in grouped_row["redeem_tx_hashes"]:
                grouped_row["redeem_tx_hashes"].append(tx_hash)

    aggregated_rows: list[dict[str, Any]] = []
    for row in grouped.values():
        net_pnl = row["sell_usdc"] + row["redeem_usdc"] - row["buy_usdc"]
        if row["redeem_count"] > 0:
            close_type = "redeemed"
        elif abs(row["buy_size"] - row["sell_size"]) <= 1e-9 and row["sell_count"] > 0:
            close_type = "sold_flat"
        else:
            close_type = "open_or_unknown"
        outcomes = sorted(row["outcomes"])
        aggregated_rows.append(
            {
                "condition_id": row["condition_id"],
                "event_slug": row["event_slug"],
                "title": row["title"],
                "outcome": outcomes[0] if len(outcomes) == 1 else ";".join(outcomes),
                "asset_ids": ";".join(sorted(row["asset_ids"])),
                "first_activity_at": row["first_activity_at"],
                "last_activity_at": row["last_activity_at"],
                "buy_count": row["buy_count"],
                "sell_count": row["sell_count"],
                "redeem_count": row["redeem_count"],
                "buy_size": row["buy_size"],
                "sell_size": row["sell_size"],
                "redeem_size": row["redeem_size"],
                "buy_usdc": row["buy_usdc"],
                "sell_usdc": row["sell_usdc"],
                "redeem_usdc": row["redeem_usdc"],
                "pnl_usdc": net_pnl,
                "close_type": close_type,
                "trade_tx_hashes": ";".join(row["trade_tx_hashes"]),
                "redeem_tx_hashes": ";".join(row["redeem_tx_hashes"]),
            }
        )
    aggregated_rows.sort(key=lambda row: (row["first_activity_at"] or "", row["condition_id"]))
    return aggregated_rows


def build_public_history(
    *,
    user: str,
    page_size: int = 500,
    timeout_secs: float = 20.0,
    max_pages: int | None = None,
) -> PublicHistoryBundle:
    activity = _fetch_paginated_activity(
        user=user,
        page_size=page_size,
        timeout_secs=timeout_secs,
        max_pages=max_pages,
    )
    raw_activity_rows = [_normalize_activity_row(row) for row in activity]
    raw_activity_rows.sort(key=lambda row: (row["timestamp"] or "", row["transaction_hash"] or ""))
    aggregated_history_rows = build_aggregated_history(raw_activity_rows)
    return PublicHistoryBundle(
        user=user,
        activity=activity,
        raw_activity_rows=raw_activity_rows,
        aggregated_history_rows=aggregated_history_rows,
    )


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in fieldnames})


def _cleanup_old_artifacts(output_dir: Path) -> None:
    for filename in _LEGACY_ARTIFACTS:
        path = output_dir / filename
        if path.exists():
            path.unlink()


def write_public_history(bundle: PublicHistoryBundle, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_artifacts(output_dir)
    _write_csv(
        output_dir / RAW_ACTIVITY_FILENAME,
        bundle.raw_activity_rows,
        fieldnames=[
            "timestamp",
            "type",
            "condition_id",
            "asset",
            "outcome",
            "side",
            "price",
            "size",
            "usdc_size",
            "transaction_hash",
            "event_slug",
            "title",
        ],
    )
    _write_csv(
        output_dir / AGGREGATED_HISTORY_FILENAME,
        bundle.aggregated_history_rows,
        fieldnames=[
            "condition_id",
            "event_slug",
            "title",
            "outcome",
            "asset_ids",
            "first_activity_at",
            "last_activity_at",
            "buy_count",
            "sell_count",
            "redeem_count",
            "buy_size",
            "sell_size",
            "redeem_size",
            "buy_usdc",
            "sell_usdc",
            "redeem_usdc",
            "pnl_usdc",
            "close_type",
            "trade_tx_hashes",
            "redeem_tx_hashes",
        ],
    )


def main(argv: list[str] | None = None) -> None:
    parser = _make_arg_parser()
    args = parser.parse_args(argv)

    bundle = build_public_history(
        user=args.user,
        page_size=args.page_size,
        timeout_secs=args.timeout_secs,
        max_pages=args.max_pages,
    )
    output_dir = Path(args.output_dir)
    write_public_history(bundle, output_dir)

    summary = bundle.summary()
    print(f"User             : {summary['user']}")
    print(f"Activity rows    : {summary['activity_count']}")
    print(f"Trade rows       : {summary['trade_count']}")
    print(f"Redeem rows      : {summary['redeem_count']}")
    print(f"Aggregated rows  : {summary['aggregated_market_count']}")
    print(f"Artifacts        : {output_dir}")


if __name__ == "__main__":
    main()
