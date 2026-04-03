from types import SimpleNamespace

import pytest

from live.market_metadata import ResolvedWindowMetadata
from live.wallet_truth import WalletTokenPosition, WalletTruthSnapshot
from live import redeem_oneoff


def _metadata() -> ResolvedWindowMetadata:
    return ResolvedWindowMetadata(
        slug="btc-updown-15m-1773869400",
        condition_id="cond-1",
        window_end_ns=1_773_870_300_000_000_000,
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_outcome_label="Up",
        no_outcome_label="Down",
        selected_outcome_side="yes",
    )


def _snapshot() -> WalletTruthSnapshot:
    return WalletTruthSnapshot(
        wallet_address="0xwallet",
        collateral_balance=12.34,
        positions=(
            WalletTokenPosition(
                condition_id="cond-1",
                token_id="yes-token",
                instrument_id="cond-1-yes-token.POLYMARKET",
                outcome_side="yes",
                outcome_label="Up",
                size=5.11,
                redeemable=True,
                mergeable=False,
                window_slug="btc-updown-15m-1773869400",
                window_end_ns=1_773_870_300_000_000_000,
            ),
        ),
        settlements=(),
    )


def test_resolve_market_metadata_by_slug_parses_market(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "conditionId": "cond-1",
                    "clobTokenIds": "[\"yes-token\", \"no-token\"]",
                    "outcomes": "[\"Up\", \"Down\"]",
                }
            ]

    monkeypatch.setattr(redeem_oneoff.requests, "get", lambda *args, **kwargs: FakeResponse())

    result = redeem_oneoff.resolve_market_metadata_by_slug(
        "btc-updown-15m-1773869400",
        outcome_side="yes",
    )

    assert result == _metadata()


def test_main_dry_run_prints_redemption_results(monkeypatch, capsys):
    metadata = _metadata()
    snapshot = _snapshot()
    calls = {}

    monkeypatch.setenv("PRIVATE_KEY", "0x" + ("11" * 32))
    monkeypatch.setenv("WALLET_ADDRESS", "0xwallet")
    monkeypatch.setattr(redeem_oneoff, "resolve_market_metadata_by_slug", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(redeem_oneoff, "make_polymarket_balance_client", lambda sandbox: (object(), "0xwallet"))
    monkeypatch.setattr(
        redeem_oneoff,
        "ProdWalletTruthProvider",
        lambda wallet_address, balance_client, registry: SimpleNamespace(snapshot=lambda: snapshot),
    )
    monkeypatch.setattr(
        redeem_oneoff,
        "fetch_market_resolution",
        lambda condition_id, token_id: SimpleNamespace(
            resolved=True,
            winning_outcome="Up",
            winning_token_id="yes-token",
        ),
    )

    def fake_executor_ctor(**kwargs):
        calls["executor"] = kwargs
        return SimpleNamespace(
            settle=lambda positions, resolution: [
                SimpleNamespace(
                    instrument_id="cond-1-yes-token.POLYMARKET",
                    position_size=5.11,
                    status="ready_to_redeem",
                    settlement_price=1.0,
                    transaction_hash=None,
                )
            ]
        )

    monkeypatch.setattr(redeem_oneoff, "ProdRedemptionExecutor", fake_executor_ctor)

    redeem_oneoff.main([
        "--market-slug",
        "btc-updown-15m-1773869400",
        "--yes",
    ])

    out = capsys.readouterr().out
    assert "Market slug  : btc-updown-15m-1773869400" in out
    assert "Positions    : 1" in out
    assert "status=ready_to_redeem" in out
    assert calls["executor"]["dry_run"] is True


def test_main_uses_polygon_rpc_url_from_env_when_cli_omitted(monkeypatch):
    metadata = _metadata()
    snapshot = _snapshot()
    calls = {}

    monkeypatch.setenv("PRIVATE_KEY", "0x" + ("11" * 32))
    monkeypatch.setenv("WALLET_ADDRESS", "0xwallet")
    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.example")
    monkeypatch.setattr(redeem_oneoff, "resolve_market_metadata_by_slug", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(redeem_oneoff, "make_polymarket_balance_client", lambda sandbox: (object(), "0xwallet"))
    monkeypatch.setattr(
        redeem_oneoff,
        "ProdWalletTruthProvider",
        lambda wallet_address, balance_client, registry: SimpleNamespace(snapshot=lambda: snapshot),
    )
    monkeypatch.setattr(
        redeem_oneoff,
        "fetch_market_resolution",
        lambda condition_id, token_id: SimpleNamespace(
            resolved=True,
            winning_outcome="Up",
            winning_token_id="yes-token",
        ),
    )

    def fake_executor_ctor(**kwargs):
        calls["executor"] = kwargs
        return SimpleNamespace(settle=lambda positions, resolution: [])

    monkeypatch.setattr(redeem_oneoff, "ProdRedemptionExecutor", fake_executor_ctor)

    redeem_oneoff.main([
        "--market-slug",
        "btc-updown-15m-1773869400",
        "--yes",
    ])

    assert calls["executor"]["rpc_url"] == "https://rpc.example"


def test_main_requires_position(monkeypatch):
    metadata = _metadata()

    monkeypatch.setattr(redeem_oneoff, "resolve_market_metadata_by_slug", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(redeem_oneoff, "make_polymarket_balance_client", lambda sandbox: (object(), "0xwallet"))
    monkeypatch.setattr(
        redeem_oneoff,
        "ProdWalletTruthProvider",
        lambda wallet_address, balance_client, registry: SimpleNamespace(
            snapshot=lambda: WalletTruthSnapshot(
                wallet_address="0xwallet",
                collateral_balance=1.0,
                positions=(),
                settlements=(),
            )
        ),
    )
    monkeypatch.setattr(
        redeem_oneoff,
        "fetch_market_resolution",
        lambda condition_id, token_id: SimpleNamespace(
            resolved=True,
            winning_outcome="Up",
            winning_token_id="yes-token",
        ),
    )

    with pytest.raises(SystemExit, match="No wallet position found"):
        redeem_oneoff.main([
            "--market-slug",
            "btc-updown-15m-1773869400",
            "--yes",
        ])
