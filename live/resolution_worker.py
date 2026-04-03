"""External wallet-based resolution worker."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

try:
    from web3.exceptions import TimeExhausted, Web3RPCError
except Exception:  # pragma: no cover - optional dependency guard for tests/imports
    TimeExhausted = ()
    Web3RPCError = ()

try:
    from requests import RequestException
except Exception:  # pragma: no cover - optional dependency guard for tests/imports
    RequestException = ()

from live.market_metadata import WindowMetadataRegistry
from live.resolution import MarketResolution, fetch_market_resolution
from live.sandbox_wallet import SandboxSettlementResult, SandboxWalletStore
from live.wallet_truth import WalletTokenPosition, WalletTruthProvider


@dataclass(frozen=True)
class ResolutionScanResult:
    condition_id: str
    instrument_id: str
    token_id: str
    position_size: float
    resolved: bool
    settlement_price: float | None
    token_won: bool | None
    status: str
    transaction_hash: str | None = None
    error_message: str | None = None


class ResolutionExecutor(Protocol):
    def settle(
        self,
        *,
        positions: tuple[WalletTokenPosition, ...],
        resolution: MarketResolution,
    ) -> list[ResolutionScanResult]:
        """Apply settlement or record the required action for a condition."""


class SandboxResolutionExecutor:
    def __init__(self, wallet_store: SandboxWalletStore) -> None:
        self._wallet_store = wallet_store

    def settle(
        self,
        *,
        positions: tuple[WalletTokenPosition, ...],
        resolution: MarketResolution,
    ) -> list[ResolutionScanResult]:
        settlement_prices = {
            position.token_id: 1.0 if position.token_id == resolution.winning_token_id else 0.0
            for position in positions
        }
        settled_by_token = {
            result.token_id: result
            for result in self._wallet_store.settle_tokens(settlement_prices)
        }

        results: list[ResolutionScanResult] = []
        for position in positions:
            result: SandboxSettlementResult | None = settled_by_token.get(position.token_id)
            settlement_price = settlement_prices[position.token_id]
            status = "settled" if result is not None else "already_settled"
            results.append(
                ResolutionScanResult(
                    condition_id=position.condition_id,
                    instrument_id=position.instrument_id,
                    token_id=position.token_id,
                    position_size=position.size,
                    resolved=True,
                    settlement_price=settlement_price,
                    token_won=resolution.winning_token_id == position.token_id,
                    status=status,
                )
            )
        return results


class ResolutionWorker:
    def __init__(
        self,
        *,
        registry: WindowMetadataRegistry,
        wallet_truth_provider: WalletTruthProvider,
        executor: ResolutionExecutor,
        restrict_to_registry: bool = True,
        resolution_fetcher=fetch_market_resolution,
    ) -> None:
        self._registry = registry
        self._wallet_truth_provider = wallet_truth_provider
        self._executor = executor
        self._restrict_to_registry = restrict_to_registry
        self._resolution_fetcher = resolution_fetcher

    def scan_once(self) -> list[ResolutionScanResult]:
        snapshot = self._wallet_truth_provider.snapshot()
        grouped_positions: dict[str, list[WalletTokenPosition]] = {}
        for position in snapshot.positions:
            if position.size <= 0:
                continue
            if self._restrict_to_registry and not self._registry.contains(
                condition_id=position.condition_id,
                token_id=position.token_id,
            ):
                continue
            grouped_positions.setdefault(position.condition_id, []).append(position)

        results: list[ResolutionScanResult] = []
        for condition_id, positions in grouped_positions.items():
            try:
                resolution = self._resolution_fetcher(condition_id, positions[0].token_id)
            except Exception as exc:
                results.extend(
                    self._error_results_for_positions(
                        positions=positions,
                        status=_classify_worker_exception(stage="resolution", exc=exc),
                        error_message=f"{exc.__class__.__name__}: {exc}",
                    )
                )
                continue
            if not resolution.resolved or resolution.token_won is None:
                for position in positions:
                    results.append(
                        ResolutionScanResult(
                            condition_id=position.condition_id,
                            instrument_id=position.instrument_id,
                            token_id=position.token_id,
                            position_size=position.size,
                            resolved=False,
                            settlement_price=None,
                            token_won=None,
                            status="pending",
                        )
                    )
                continue

            try:
                results.extend(
                    self._executor.settle(positions=tuple(positions), resolution=resolution)
                )
            except Exception as exc:
                results.extend(
                    self._error_results_for_positions(
                        positions=positions,
                        resolution=resolution,
                        status=_classify_worker_exception(stage="redemption", exc=exc),
                        error_message=f"{exc.__class__.__name__}: {exc}",
                    )
                )

        return results

    def _error_results_for_positions(
        self,
        *,
        positions: list[WalletTokenPosition],
        status: str,
        error_message: str,
        resolution: MarketResolution | None = None,
    ) -> list[ResolutionScanResult]:
        results: list[ResolutionScanResult] = []
        for position in positions:
            token_won = (
                None
                if resolution is None or resolution.winning_token_id is None
                else position.token_id == resolution.winning_token_id
            )
            settlement_price = None
            if token_won is True:
                settlement_price = 1.0
            elif token_won is False:
                settlement_price = 0.0
            results.append(
                ResolutionScanResult(
                    condition_id=position.condition_id,
                    instrument_id=position.instrument_id,
                    token_id=position.token_id,
                    position_size=position.size,
                    resolved=resolution is not None and resolution.resolved,
                    settlement_price=settlement_price,
                    token_won=token_won,
                    status=status,
                    error_message=error_message,
                )
            )
        return results


def _classify_worker_exception(*, stage: str, exc: Exception) -> str:
    if isinstance(exc, TimeExhausted):
        return f"{stage}_error_confirmation"
    if isinstance(exc, Web3RPCError):
        return f"{stage}_error_rpc"
    if isinstance(exc, (RequestException, ConnectionError, TimeoutError, OSError)):
        return f"{stage}_error_transport"
    return f"{stage}_error_unexpected"
