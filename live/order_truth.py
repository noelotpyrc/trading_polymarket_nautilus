"""Order-truth types and providers for stale IOC reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OpenOrderParams


class OrderTruthStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"

    @property
    def is_open(self) -> bool:
        return self is OrderTruthStatus.OPEN

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderTruthStatus.CLOSED,
            OrderTruthStatus.CANCELED,
            OrderTruthStatus.EXPIRED,
            OrderTruthStatus.NOT_FOUND,
        }


@dataclass(frozen=True)
class OrderTruthRecord:
    client_order_id: str | None
    venue_order_id: str | None
    status: OrderTruthStatus
    reason: str | None = None
    remaining_qty: float | None = None


class OrderTruthProvider(Protocol):
    def order_status(
        self,
        *,
        client_order_id: str | None,
        venue_order_id: str | None,
    ) -> OrderTruthRecord:
        """Return external order truth for the given order identifiers."""


class ProdOrderTruthProvider:
    """Production order-truth provider backed by Polymarket CLOB order endpoints."""

    def __init__(self, *, clob_client: ClobClient) -> None:
        self._client = clob_client

    def order_status(
        self,
        *,
        client_order_id: str | None,
        venue_order_id: str | None,
    ) -> OrderTruthRecord:
        if venue_order_id is None:
            return OrderTruthRecord(
                client_order_id=client_order_id,
                venue_order_id=None,
                status=OrderTruthStatus.UNKNOWN,
                reason="missing venue order id",
            )

        try:
            open_orders = self._client.get_orders(OpenOrderParams(id=str(venue_order_id)))
        except Exception as exc:
            return OrderTruthRecord(
                client_order_id=client_order_id,
                venue_order_id=str(venue_order_id),
                status=OrderTruthStatus.UNKNOWN,
                reason=str(exc),
            )

        for payload in open_orders or ():
            payload_order_id = _payload_order_id(payload)
            if payload_order_id is None or payload_order_id != str(venue_order_id):
                continue
            return OrderTruthRecord(
                client_order_id=client_order_id,
                venue_order_id=str(venue_order_id),
                status=OrderTruthStatus.OPEN,
                remaining_qty=_payload_remaining_qty(payload),
            )

        try:
            payload = self._client.get_order(str(venue_order_id))
        except Exception as exc:
            if _looks_like_missing_order(exc):
                return OrderTruthRecord(
                    client_order_id=client_order_id,
                    venue_order_id=str(venue_order_id),
                    status=OrderTruthStatus.NOT_FOUND,
                    reason=str(exc),
                )
            return OrderTruthRecord(
                client_order_id=client_order_id,
                venue_order_id=str(venue_order_id),
                status=OrderTruthStatus.NOT_FOUND,
                reason="not present in open orders",
            )

        status = _normalize_payload_status(payload)
        if status is None:
            status = OrderTruthStatus.NOT_FOUND

        return OrderTruthRecord(
            client_order_id=client_order_id,
            venue_order_id=str(venue_order_id),
            status=status,
            reason=_payload_status_value(payload),
            remaining_qty=_payload_remaining_qty(payload),
        )


def _looks_like_missing_order(exc: Exception) -> bool:
    message = str(exc).lower()
    return "404" in message or "not found" in message


def _payload_order_id(payload: dict) -> str | None:
    for key in ("id", "order_id", "orderID"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _payload_status_value(payload: dict) -> str | None:
    for key in ("status", "state", "order_status", "orderStatus"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _normalize_payload_status(payload: dict) -> OrderTruthStatus | None:
    raw_status = _payload_status_value(payload)
    if raw_status is None:
        return None

    normalized = raw_status.strip().lower()
    if normalized in {"open", "live", "placed", "placement", "resting", "active"}:
        return OrderTruthStatus.OPEN
    if normalized in {"filled", "matched", "executed", "closed", "done"}:
        return OrderTruthStatus.CLOSED
    if normalized in {"canceled", "cancelled"}:
        return OrderTruthStatus.CANCELED
    if normalized == "expired":
        return OrderTruthStatus.EXPIRED
    if normalized in {"not_found", "missing"}:
        return OrderTruthStatus.NOT_FOUND
    return OrderTruthStatus.UNKNOWN


def _payload_remaining_qty(payload: dict) -> float | None:
    for key in ("remaining_size", "remainingSize", "unfilled_size", "unfilledSize"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    size = payload.get("size")
    matched = payload.get("size_matched") or payload.get("matched_size")
    if size is None or matched is None:
        return None

    try:
        return max(0.0, float(size) - float(matched))
    except (TypeError, ValueError):
        return None
