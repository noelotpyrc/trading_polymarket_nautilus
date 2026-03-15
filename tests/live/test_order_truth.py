"""Tests for order-truth providers and sandbox order state."""
from live.order_truth import OrderTruthStatus, ProdOrderTruthProvider
from live.sandbox_order import SandboxOrderStore, SandboxOrderTruthProvider


class FakeClobClient:
    def __init__(self, *, open_orders=None, order_payload=None, error=None):
        self._open_orders = [] if open_orders is None else open_orders
        self._order_payload = order_payload
        self._error = error

    def get_orders(self, params):
        assert params.id == "V-1"
        return list(self._open_orders)

    def get_order(self, order_id):
        assert order_id == "V-1"
        if self._error is not None:
            raise self._error
        return self._order_payload


def test_prod_order_truth_provider_reports_open_when_order_is_still_resting():
    provider = ProdOrderTruthProvider(
        clob_client=FakeClobClient(
            open_orders=[{"id": "V-1", "size": "5", "size_matched": "2"}],
        )
    )

    truth = provider.order_status(client_order_id="O-1", venue_order_id="V-1")

    assert truth.status is OrderTruthStatus.OPEN
    assert truth.remaining_qty == 3.0


def test_prod_order_truth_provider_maps_terminal_get_order_status():
    provider = ProdOrderTruthProvider(
        clob_client=FakeClobClient(
            open_orders=[],
            order_payload={"id": "V-1", "status": "CANCELED"},
        )
    )

    truth = provider.order_status(client_order_id="O-1", venue_order_id="V-1")

    assert truth.status is OrderTruthStatus.CANCELED


def test_prod_order_truth_provider_treats_missing_order_as_not_found():
    provider = ProdOrderTruthProvider(
        clob_client=FakeClobClient(
            open_orders=[],
            error=RuntimeError("404 order not found"),
        )
    )

    truth = provider.order_status(client_order_id="O-1", venue_order_id="V-1")

    assert truth.status is OrderTruthStatus.NOT_FOUND


def test_sandbox_order_truth_provider_round_trips_order_status():
    store = SandboxOrderStore()
    provider = SandboxOrderTruthProvider(order_store=store)
    store.register_order(
        client_order_id="O-1",
        venue_order_id="V-1",
        instrument_id="cond-1-yes-1.POLYMARKET",
        status=OrderTruthStatus.OPEN,
        remaining_qty=3.0,
    )

    initial = provider.order_status(client_order_id="O-1", venue_order_id="V-1")
    assert initial.status is OrderTruthStatus.OPEN
    assert initial.remaining_qty == 3.0

    store.set_order_status(
        client_order_id="O-1",
        venue_order_id="V-1",
        status=OrderTruthStatus.NOT_FOUND,
        remaining_qty=0.0,
    )

    updated = provider.order_status(client_order_id="O-1", venue_order_id="V-1")
    assert updated.status is OrderTruthStatus.NOT_FOUND
    assert updated.remaining_qty == 0.0
