"""Synthetic order truth for sandbox IOC reconciliation."""
from __future__ import annotations

from dataclasses import dataclass

from live.order_truth import OrderTruthRecord, OrderTruthStatus


@dataclass(frozen=True)
class SandboxOrderState:
    client_order_id: str
    venue_order_id: str | None
    instrument_id: str | None
    status: OrderTruthStatus
    remaining_qty: float | None = None


class SandboxOrderStore:
    def __init__(self) -> None:
        self._orders_by_client_id: dict[str, SandboxOrderState] = {}
        self._client_ids_by_venue_id: dict[str, str] = {}

    def register_order(
        self,
        *,
        client_order_id: str,
        venue_order_id: str | None = None,
        instrument_id: str | None = None,
        status: OrderTruthStatus = OrderTruthStatus.OPEN,
        remaining_qty: float | None = None,
    ) -> None:
        state = SandboxOrderState(
            client_order_id=str(client_order_id),
            venue_order_id=None if venue_order_id is None else str(venue_order_id),
            instrument_id=instrument_id,
            status=status,
            remaining_qty=remaining_qty,
        )
        self._orders_by_client_id[state.client_order_id] = state
        if state.venue_order_id is not None:
            self._client_ids_by_venue_id[state.venue_order_id] = state.client_order_id

    def set_order_status(
        self,
        *,
        client_order_id: str,
        status: OrderTruthStatus,
        venue_order_id: str | None = None,
        instrument_id: str | None = None,
        remaining_qty: float | None = None,
    ) -> None:
        existing = self.lookup(client_order_id=client_order_id, venue_order_id=venue_order_id)
        if existing is not None:
            if venue_order_id is None:
                venue_order_id = existing.venue_order_id
            if instrument_id is None:
                instrument_id = existing.instrument_id
            if remaining_qty is None:
                remaining_qty = existing.remaining_qty

        self.register_order(
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            instrument_id=instrument_id,
            status=status,
            remaining_qty=remaining_qty,
        )

    def lookup(
        self,
        *,
        client_order_id: str | None,
        venue_order_id: str | None,
    ) -> SandboxOrderState | None:
        if client_order_id is not None:
            return self._orders_by_client_id.get(str(client_order_id))
        if venue_order_id is not None:
            client_id = self._client_ids_by_venue_id.get(str(venue_order_id))
            if client_id is not None:
                return self._orders_by_client_id.get(client_id)
        return None


class SandboxOrderTruthProvider:
    def __init__(self, *, order_store: SandboxOrderStore) -> None:
        self._order_store = order_store

    def order_status(
        self,
        *,
        client_order_id: str | None,
        venue_order_id: str | None,
    ) -> OrderTruthRecord:
        state = self._order_store.lookup(
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
        )
        if state is None:
            return OrderTruthRecord(
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                status=OrderTruthStatus.UNKNOWN,
                reason="sandbox order not tracked",
            )

        return OrderTruthRecord(
            client_order_id=state.client_order_id,
            venue_order_id=state.venue_order_id,
            status=state.status,
            remaining_qty=state.remaining_qty,
        )
