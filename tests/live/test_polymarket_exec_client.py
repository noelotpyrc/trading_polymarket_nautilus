import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from live.polymarket_exec_client import FillAwarePolymarketExecutionClient
from live.polymarket_exec_client import FillAwarePolymarketLiveExecClientFactory
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.identifiers import AccountId, InstrumentId, VenueOrderId
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.core.uuid import UUID4


PM_INSTRUMENT_ID = InstrumentId.from_str(
    "0x53686986adbbb5cfa65b39ad112aa07a322ffcc0a3a96958caf9d3e411c70ecd-"
    "20273077868563310640340620174681326658131957923751259675193429291086076379673"
    ".POLYMARKET",
)


class _DummyClock:
    def __init__(self, ts_now: int) -> None:
        self._ts_now = ts_now

    def timestamp_ns(self) -> int:
        return self._ts_now


class _TestClient:
    _FILL_REPORT_LOOKBACK_BUFFER = (
        FillAwarePolymarketExecutionClient._FILL_REPORT_LOOKBACK_BUFFER
    )
    _FILL_REPORT_MAX_ATTEMPTS = (
        FillAwarePolymarketExecutionClient._FILL_REPORT_MAX_ATTEMPTS
    )
    _FILL_REPORT_RETRY_DELAY_SECS = 0
    _fill_report_query_start = FillAwarePolymarketExecutionClient._fill_report_query_start
    _weighted_average_fill_price = FillAwarePolymarketExecutionClient._weighted_average_fill_price
    _enrich_order_status_report_with_fill_truth = (
        FillAwarePolymarketExecutionClient._enrich_order_status_report_with_fill_truth
    )
    _fetch_fill_reports_for_order_status = (
        FillAwarePolymarketExecutionClient._fetch_fill_reports_for_order_status
    )

    def __init__(self, fills):
        self._clock = _DummyClock(ts_now=1_777_777_777_000_000_000)
        self._fills = fills
        self.last_fill_command = None
        self.fill_report_calls = 0

    async def generate_fill_reports(self, command):
        self.last_fill_command = command
        self.fill_report_calls += 1
        if isinstance(self._fills, list) and self._fills and isinstance(self._fills[0], list):
            index = min(self.fill_report_calls - 1, len(self._fills) - 1)
            return self._fills[index]
        return self._fills


def _make_order_status_report(
    *,
    filled_qty: str = "5.2",
    avg_px=None,
    price: str = "0.98",
) -> OrderStatusReport:
    return OrderStatusReport(
        account_id=AccountId("POLYMARKET-001"),
        instrument_id=PM_INSTRUMENT_ID,
        venue_order_id=VenueOrderId("123"),
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        quantity=Quantity.from_str("5.2"),
        filled_qty=Quantity.from_str(filled_qty),
        report_id=UUID4(),
        ts_accepted=1_777_778_222_000_000_000,
        ts_last=1_777_778_333_000_000_000,
        ts_init=1_777_778_222_000_000_000,
        price=Price.from_str(price),
        avg_px=avg_px,
    )


def test_fill_query_start_uses_order_accept_time():
    client = _TestClient(fills=[])
    report = _make_order_status_report()

    start = client._fill_report_query_start(report)

    assert start == pd.Timestamp(report.ts_accepted, unit="ns", tz="UTC") - pd.Timedelta(minutes=1)


def test_weighted_average_fill_price_uses_pm_fill_reports():
    client = _TestClient(fills=[])
    fills = [
        SimpleNamespace(last_qty=Decimal("2"), last_px=Decimal("0.97")),
        SimpleNamespace(last_qty=Decimal("3"), last_px=Decimal("0.99")),
    ]

    avg_px = client._weighted_average_fill_price(fills)

    assert avg_px == Decimal("0.982")


def test_enrich_order_status_report_sets_avg_px_from_fill_truth():
    fills = [
        SimpleNamespace(last_qty=Decimal("2"), last_px=Decimal("0.97")),
        SimpleNamespace(last_qty=Decimal("3"), last_px=Decimal("0.99")),
    ]
    client = _TestClient(fills=fills)
    report = _make_order_status_report(avg_px=None)

    enriched = asyncio.run(client._enrich_order_status_report_with_fill_truth(report))

    assert enriched is not report
    assert enriched.avg_px == Decimal("0.982")
    assert client.last_fill_command.instrument_id == PM_INSTRUMENT_ID
    assert client.last_fill_command.venue_order_id == VenueOrderId("123")


def test_enrich_order_status_report_skips_unfilled_reports():
    client = _TestClient(fills=[SimpleNamespace(last_qty=Decimal("1"), last_px=Decimal("0.99"))])
    report = _make_order_status_report(filled_qty="0")

    enriched = asyncio.run(client._enrich_order_status_report_with_fill_truth(report))

    assert enriched is report
    assert client.last_fill_command is None


def test_fetch_fill_reports_retries_until_fills_arrive():
    fills = [[
    ], [
        SimpleNamespace(last_qty=Decimal("2"), last_px=Decimal("0.97")),
    ]]
    client = _TestClient(fills=fills)
    report = _make_order_status_report(avg_px=None)

    fetched = asyncio.run(client._fetch_fill_reports_for_order_status(report))

    assert len(fetched) == 1
    assert client.fill_report_calls == 2


def test_fetch_fill_reports_stops_after_max_attempts_when_empty():
    client = _TestClient(fills=[])
    client._FILL_REPORT_MAX_ATTEMPTS = 3
    report = _make_order_status_report(avg_px=None)

    fetched = asyncio.run(client._fetch_fill_reports_for_order_status(report))

    assert fetched == []
    assert client.fill_report_calls == 3


def test_build_node_uses_fill_aware_polymarket_exec_factory(monkeypatch):
    from live.node import build_node

    monkeypatch.setenv("PRIVATE_KEY", "pk")
    monkeypatch.setenv("POLYMARKET_API_KEY", "api-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "passphrase")
    monkeypatch.setenv("WALLET_ADDRESS", "0xabc")
    captured = {}

    class FakeTradingNode:
        def __init__(self, config):
            captured["config"] = config

        def add_data_client_factory(self, *args):
            pass

        def add_exec_client_factory(self, name, factory):
            captured["exec_factory"] = (name, factory)

    monkeypatch.setattr("nautilus_trader.live.node.TradingNode", FakeTradingNode)

    node = build_node(["foo.POLYMARKET"], sandbox=False, binance_us=False)

    assert isinstance(node, FakeTradingNode)
    assert captured["exec_factory"] == ("POLYMARKET", FillAwarePolymarketLiveExecClientFactory)
