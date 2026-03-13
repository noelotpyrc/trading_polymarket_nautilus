"""Helpers for polling Polymarket market resolution state."""
from dataclasses import dataclass

import requests

HOST = "https://clob.polymarket.com"


@dataclass(frozen=True)
class MarketResolution:
    condition_id: str
    token_id: str
    market_closed: bool
    target_token_outcome: str | None
    winning_token_id: str | None
    winning_outcome: str | None

    @property
    def resolved(self) -> bool:
        return self.market_closed and self.winning_token_id is not None

    @property
    def token_won(self) -> bool | None:
        if not self.resolved:
            return None
        return self.winning_token_id == self.token_id

    @property
    def settlement_price(self) -> float | None:
        won = self.token_won
        if won is None:
            return None
        return 1.0 if won else 0.0


def fetch_market_resolution(
    condition_id: str,
    token_id: str,
    *,
    timeout_secs: float = 5.0,
) -> MarketResolution:
    response = requests.get(f"{HOST}/markets/{condition_id}", timeout=timeout_secs)
    response.raise_for_status()
    market = response.json()

    tokens = market.get("tokens", []) or []
    target_token = None
    winning_token = None

    for token in tokens:
        token_value = str(token.get("token_id", ""))
        if token_value == token_id:
            target_token = token
        if token.get("winner") is True:
            winning_token = token

    return MarketResolution(
        condition_id=condition_id,
        token_id=token_id,
        market_closed=bool(market.get("closed")),
        target_token_outcome=_token_outcome(target_token),
        winning_token_id=None if winning_token is None else str(winning_token.get("token_id")),
        winning_outcome=_token_outcome(winning_token),
    )


def _token_outcome(token: dict | None) -> str | None:
    if not token:
        return None
    outcome = token.get("outcome")
    return None if outcome in (None, "") else str(outcome)
