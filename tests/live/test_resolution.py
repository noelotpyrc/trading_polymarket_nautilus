"""Unit tests for Polymarket market-resolution polling helpers."""
from live import resolution


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def test_fetch_market_resolution_parses_resolved_winner(monkeypatch):
    payload = {
        "closed": True,
        "tokens": [
            {"token_id": "yes-token", "outcome": "Yes", "winner": True},
            {"token_id": "no-token", "outcome": "No", "winner": False},
        ],
    }

    monkeypatch.setattr(
        resolution.requests,
        "get",
        lambda url, timeout: DummyResponse(payload),
    )

    result = resolution.fetch_market_resolution("cond-1", "yes-token")

    assert result.condition_id == "cond-1"
    assert result.token_id == "yes-token"
    assert result.market_closed is True
    assert result.target_token_outcome == "Yes"
    assert result.winning_token_id == "yes-token"
    assert result.winning_outcome == "Yes"
    assert result.resolved is True
    assert result.token_won is True
    assert result.settlement_price == 1.0


def test_fetch_market_resolution_handles_unresolved_market(monkeypatch):
    payload = {
        "closed": False,
        "tokens": [
            {"token_id": "yes-token", "outcome": "Yes", "winner": False},
            {"token_id": "no-token", "outcome": "No", "winner": False},
        ],
    }

    monkeypatch.setattr(
        resolution.requests,
        "get",
        lambda url, timeout: DummyResponse(payload),
    )

    result = resolution.fetch_market_resolution("cond-1", "no-token")

    assert result.market_closed is False
    assert result.target_token_outcome == "No"
    assert result.winning_token_id is None
    assert result.winning_outcome is None
    assert result.resolved is False
    assert result.token_won is None
    assert result.settlement_price is None
