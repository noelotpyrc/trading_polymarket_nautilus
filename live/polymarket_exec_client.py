from __future__ import annotations

import asyncio
from decimal import Decimal

import pandas as pd

from nautilus_trader.adapters.polymarket.common.credentials import (
    PolymarketWebSocketAuth,
    get_polymarket_api_key,
    get_polymarket_api_secret,
    get_polymarket_passphrase,
)
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.factories import (
    get_polymarket_http_client,
    get_polymarket_instrument_provider,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GenerateFillReports, GenerateOrderStatusReport
from nautilus_trader.execution.reports import FillReport, OrderStatusReport
from nautilus_trader.live.factories import LiveExecClientFactory


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    as_decimal = getattr(value, "as_decimal", None)
    if callable(as_decimal):
        return as_decimal()
    return Decimal(str(value))


class FillAwarePolymarketExecutionClient(PolymarketExecutionClient):
    _FILL_REPORT_LOOKBACK_BUFFER = pd.Timedelta(minutes=1)
    _FILL_REPORT_MAX_ATTEMPTS = 4
    _FILL_REPORT_RETRY_DELAY_SECS = 0.5

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        report = await super().generate_order_status_report(command)
        if report is None:
            return None
        return await self._enrich_order_status_report_with_fill_truth(report)

    async def _enrich_order_status_report_with_fill_truth(
        self,
        report: OrderStatusReport,
    ) -> OrderStatusReport:
        if report.avg_px is not None:
            return report
        if _as_decimal(report.filled_qty) in (None, Decimal("0")):
            return report
        if report.venue_order_id is None:
            return report

        fills = await self._fetch_fill_reports_for_order_status(report)
        avg_px = self._weighted_average_fill_price(fills)
        if avg_px is None:
            return report

        data = report.to_dict()
        data["avg_px"] = avg_px
        return OrderStatusReport.from_dict(data)

    async def _fetch_fill_reports_for_order_status(
        self,
        report: OrderStatusReport,
    ) -> list[FillReport]:
        command = GenerateFillReports(
            instrument_id=report.instrument_id,
            venue_order_id=report.venue_order_id,
            start=self._fill_report_query_start(report),
            end=None,
            command_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        attempts = max(1, int(self._FILL_REPORT_MAX_ATTEMPTS))
        for attempt in range(attempts):
            fills = await self.generate_fill_reports(command)
            if fills:
                return fills
            if attempt + 1 < attempts and self._FILL_REPORT_RETRY_DELAY_SECS > 0:
                await asyncio.sleep(self._FILL_REPORT_RETRY_DELAY_SECS)
        return []

    def _fill_report_query_start(self, report: OrderStatusReport) -> pd.Timestamp:
        ts_reference = report.ts_accepted or report.ts_last or report.ts_init
        return pd.Timestamp(ts_reference, unit="ns", tz="UTC") - self._FILL_REPORT_LOOKBACK_BUFFER

    def _weighted_average_fill_price(
        self,
        fills: list[FillReport],
    ) -> Decimal | None:
        if not fills:
            return None

        total_qty = Decimal("0")
        total_notional = Decimal("0")
        for fill in fills:
            qty = _as_decimal(fill.last_qty)
            px = _as_decimal(fill.last_px)
            if qty is None or px is None or qty <= 0:
                continue
            total_qty += qty
            total_notional += qty * px

        if total_qty <= 0:
            return None

        return total_notional / total_qty


class FillAwarePolymarketLiveExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: PolymarketExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> FillAwarePolymarketExecutionClient:
        http_client = get_polymarket_http_client(
            private_key=config.private_key,
            signature_type=config.signature_type,
            funder=config.funder,
            api_key=config.api_key,
            api_secret=config.api_secret,
            passphrase=config.passphrase,
            base_url=config.base_url_http,
        )
        ws_auth = PolymarketWebSocketAuth(
            apiKey=config.api_key or get_polymarket_api_key(),
            secret=config.api_secret or get_polymarket_api_secret(),
            passphrase=config.passphrase or get_polymarket_passphrase(),
        )
        provider = get_polymarket_instrument_provider(
            client=http_client,
            clock=clock,
            config=config.instrument_config,
        )
        return FillAwarePolymarketExecutionClient(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            ws_auth=ws_auth,
            name=name,
        )
