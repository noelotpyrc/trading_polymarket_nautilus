"""Synthetic wallet state for sandbox resolution and wallet-truth tests."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from live.market_metadata import WindowMetadataRegistry
from live.wallet_truth import WalletSettlement, WalletTokenPosition, WalletTruthSnapshot


@dataclass(frozen=True)
class SandboxSettlementResult:
    token_id: str
    position_size: float
    settlement_price: float
    collateral_credit: float


class SandboxWalletStore:
    def __init__(
        self,
        *,
        wallet_address: str,
        collateral_balance: float = 0.0,
        state_path: str | Path | None = None,
    ) -> None:
        self._wallet_address = wallet_address
        self._collateral_balance = collateral_balance
        self._positions_by_token_id: dict[str, float] = {}
        self._settlements: list[SandboxSettlementResult] = []
        self._state_path = None if state_path is None else Path(state_path)

        if self._state_path is not None and self._state_path.exists():
            self.sync_from_disk()
        else:
            self._persist()

    @property
    def wallet_address(self) -> str:
        return self._wallet_address

    @property
    def collateral_balance(self) -> float:
        return self._collateral_balance

    @property
    def settlements(self) -> tuple[SandboxSettlementResult, ...]:
        return tuple(self._settlements)

    @property
    def state_path(self) -> Path | None:
        return self._state_path

    def positions(self) -> dict[str, float]:
        return dict(self._positions_by_token_id)

    def set_collateral_balance(self, balance: float) -> None:
        self._collateral_balance = balance
        self._persist()

    def set_position_size(self, token_id: str, size: float) -> None:
        token_id = str(token_id)
        if size == 0:
            self._positions_by_token_id.pop(token_id, None)
        else:
            self._positions_by_token_id[token_id] = size
        self._persist()

    def adjust_position(self, token_id: str, delta_size: float) -> None:
        token_id = str(token_id)
        new_size = self._positions_by_token_id.get(token_id, 0.0) + delta_size
        self.set_position_size(token_id, new_size)

    def apply_trade(self, *, token_id: str, delta_size: float, price: float) -> None:
        self.adjust_position(token_id, delta_size)
        self._collateral_balance -= delta_size * price
        self._persist()

    def settle_token(self, token_id: str, settlement_price: float) -> SandboxSettlementResult | None:
        token_id = str(token_id)
        size = self._positions_by_token_id.get(token_id, 0.0)
        if size <= 0:
            return None

        credit = size * settlement_price
        self._collateral_balance += credit
        self._positions_by_token_id.pop(token_id, None)

        result = SandboxSettlementResult(
            token_id=token_id,
            position_size=size,
            settlement_price=settlement_price,
            collateral_credit=credit,
        )
        self._settlements.append(result)
        self._persist()
        return result

    def settle_tokens(
        self,
        settlement_prices: dict[str, float],
    ) -> list[SandboxSettlementResult]:
        results: list[SandboxSettlementResult] = []
        for token_id, settlement_price in settlement_prices.items():
            result = self.settle_token(token_id, settlement_price)
            if result is not None:
                results.append(result)
        return results

    def sync_from_disk(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return

        data = json.loads(self._state_path.read_text(encoding="utf-8"))
        self._wallet_address = str(data["wallet_address"])
        self._collateral_balance = float(data["collateral_balance"])
        self._positions_by_token_id = {
            str(token_id): float(size)
            for token_id, size in data.get("positions_by_token_id", {}).items()
        }
        self._settlements = [
            SandboxSettlementResult(
                token_id=str(item["token_id"]),
                position_size=float(item["position_size"]),
                settlement_price=float(item["settlement_price"]),
                collateral_credit=float(item["collateral_credit"]),
            )
            for item in data.get("settlements", [])
        ]

    def _persist(self) -> None:
        if self._state_path is None:
            return

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "wallet_address": self._wallet_address,
            "collateral_balance": self._collateral_balance,
            "positions_by_token_id": self._positions_by_token_id,
            "settlements": [
                {
                    "token_id": settlement.token_id,
                    "position_size": settlement.position_size,
                    "settlement_price": settlement.settlement_price,
                    "collateral_credit": settlement.collateral_credit,
                }
                for settlement in self._settlements
            ],
        }
        tmp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp_path.replace(self._state_path)


class SandboxWalletTruthProvider:
    def __init__(self, *, wallet_store: SandboxWalletStore, registry: WindowMetadataRegistry) -> None:
        self._wallet_store = wallet_store
        self._registry = registry

    def snapshot(self) -> WalletTruthSnapshot:
        self._wallet_store.sync_from_disk()
        positions: list[WalletTokenPosition] = []

        for token_id, size in sorted(self._wallet_store.positions().items()):
            metadata = self._registry.token(token_id)
            if metadata is None:
                continue
            positions.append(
                WalletTokenPosition(
                    condition_id=metadata.condition_id,
                    token_id=token_id,
                    instrument_id=metadata.instrument_id,
                    outcome_side=metadata.outcome_side,
                    outcome_label=metadata.outcome_label,
                    size=size,
                    redeemable=False,
                    mergeable=False,
                    window_slug=metadata.window_slug,
                    window_end_ns=metadata.window_end_ns,
                )
            )

        return WalletTruthSnapshot(
            wallet_address=self._wallet_store.wallet_address,
            collateral_balance=self._wallet_store.collateral_balance,
            positions=tuple(positions),
            settlements=tuple(
                WalletSettlement(
                    token_id=settlement.token_id,
                    position_size=settlement.position_size,
                    settlement_price=settlement.settlement_price,
                    collateral_credit=settlement.collateral_credit,
                )
                for settlement in self._wallet_store.settlements
            ),
        )
