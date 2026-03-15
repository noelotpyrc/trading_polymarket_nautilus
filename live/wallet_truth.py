"""Wallet-truth types and providers for resolution-aware live trading."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Protocol

from dotenv import load_dotenv
import requests
from py_clob_client.client import BalanceAllowanceParams
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.clob_types import AssetType

from live.market_metadata import WindowMetadataRegistry

load_dotenv()

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
POSITIONS_HOST = "https://data-api.polymarket.com"
_USDC_SCALE = 1_000_000


@dataclass(frozen=True)
class WalletTokenPosition:
    condition_id: str
    token_id: str
    instrument_id: str
    outcome_side: str
    outcome_label: str | None
    size: float
    redeemable: bool
    mergeable: bool
    window_slug: str
    window_end_ns: int


@dataclass(frozen=True)
class WalletSettlement:
    token_id: str
    position_size: float
    settlement_price: float
    collateral_credit: float


@dataclass(frozen=True)
class WalletTruthSnapshot:
    wallet_address: str
    collateral_balance: float
    positions: tuple[WalletTokenPosition, ...]
    settlements: tuple[WalletSettlement, ...] = ()

    def position_for_token(self, token_id: str) -> WalletTokenPosition | None:
        token_id = str(token_id)
        for position in self.positions:
            if position.token_id == token_id:
                return position
        return None

    def settlement_for_token(self, token_id: str) -> WalletSettlement | None:
        token_id = str(token_id)
        for settlement in self.settlements:
            if settlement.token_id == token_id:
                return settlement
        return None


class WalletTruthProvider(Protocol):
    def snapshot(self) -> WalletTruthSnapshot:
        """Return the current wallet-truth snapshot."""


class BalanceAllowanceClient(Protocol):
    def get_balance_allowance(self, params: BalanceAllowanceParams) -> dict:
        """Return Polymarket balance-allowance data."""


class ProdWalletTruthProvider:
    """Production wallet-truth provider backed by Polymarket APIs."""

    def __init__(
        self,
        *,
        wallet_address: str,
        balance_client: BalanceAllowanceClient,
        registry: WindowMetadataRegistry,
        positions_host: str = POSITIONS_HOST,
        signature_type: int = 0,
        timeout_secs: float = 10.0,
    ) -> None:
        self._wallet_address = wallet_address
        self._balance_client = balance_client
        self._registry = registry
        self._positions_host = positions_host.rstrip("/")
        self._signature_type = signature_type
        self._timeout_secs = timeout_secs

    def snapshot(self) -> WalletTruthSnapshot:
        collateral_balance = self._fetch_collateral_balance()
        positions = self._fetch_positions()
        return WalletTruthSnapshot(
            wallet_address=self._wallet_address,
            collateral_balance=collateral_balance,
            positions=tuple(sorted(positions, key=lambda position: position.instrument_id)),
            settlements=(),
        )

    def _fetch_collateral_balance(self) -> float:
        response = self._balance_client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self._signature_type,
            ),
        )
        return _usdc_from_units(int(response.get("balance", 0)))

    def _fetch_positions(self) -> list[WalletTokenPosition]:
        results: list[WalletTokenPosition] = []
        offset = 0
        limit = 100
        allowed_token_ids = self._registry.allowed_token_ids()
        allowed_condition_ids = self._registry.allowed_condition_ids()

        while True:
            response = requests.get(
                f"{self._positions_host}/positions",
                params={
                    "user": self._wallet_address,
                    "limit": str(limit),
                    "offset": str(offset),
                    "sizeThreshold": "0",
                    "sortBy": "TOKENS",
                    "sortDirection": "DESC",
                },
                timeout=self._timeout_secs,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload:
                break

            for position in payload:
                condition_id = str(position.get("conditionId", ""))
                token_id = str(position.get("asset", ""))
                if condition_id not in allowed_condition_ids or token_id not in allowed_token_ids:
                    continue

                metadata = self._registry.token(token_id)
                if metadata is None:
                    continue

                results.append(
                    WalletTokenPosition(
                        condition_id=condition_id,
                        token_id=token_id,
                        instrument_id=metadata.instrument_id,
                        outcome_side=metadata.outcome_side,
                        outcome_label=metadata.outcome_label,
                        size=float(position.get("size", 0.0) or 0.0),
                        redeemable=bool(position.get("redeemable")),
                        mergeable=bool(position.get("mergeable")),
                        window_slug=metadata.window_slug,
                        window_end_ns=metadata.window_end_ns,
                    )
                )

            if len(payload) < limit:
                break
            offset += limit

        return results


def _usdc_from_units(units: int) -> float:
    return units / _USDC_SCALE


def make_polymarket_balance_client(*, sandbox: bool) -> tuple[ClobClient, str]:
    if sandbox:
        private_key = os.environ["POLYMARKET_TEST_PRIVATE_KEY"]
        api_key = os.environ["POLYMARKET_TEST_API_KEY"]
        api_secret = os.environ["POLYMARKET_TEST_API_SECRET"]
        api_passphrase = os.environ["POLYMARKET_TEST_API_PASSPHRASE"]
        funder = os.environ["POLYMARKET_TEST_WALLET_ADDRESS"]
    else:
        private_key = os.environ["PRIVATE_KEY"]
        api_key = os.environ["POLYMARKET_API_KEY"]
        api_secret = os.environ["POLYMARKET_API_SECRET"]
        api_passphrase = os.getenv("POLYMARKET_PASSPHRASE") or os.environ["POLYMARKET_API_PASSPHRASE"]
        funder = os.getenv("POLYMARKET_FUNDER") or os.environ["WALLET_ADDRESS"]

    client = ClobClient(
        host=HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=0,
        funder=funder,
    )
    client.set_api_creds(
        ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
    )
    return client, funder
