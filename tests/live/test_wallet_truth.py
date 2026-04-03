"""Tests for wallet-truth metadata, providers, and sandbox settlement."""
from types import SimpleNamespace

import pytest
from web3.exceptions import TimeExhausted, Web3RPCError

from live.market_metadata import ResolvedWindowMetadata, WindowMetadataRegistry
from live.redemption import ProdRedemptionExecutor, _bytes32, _format_tx_hash
from live.resolution import MarketResolution
from live.resolution_worker import ResolutionScanResult, ResolutionWorker, SandboxResolutionExecutor
from live.sandbox_wallet import SandboxWalletStore, SandboxWalletTruthProvider
from live.wallet_truth import ProdWalletTruthProvider, WalletTokenPosition


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _registry() -> WindowMetadataRegistry:
    return WindowMetadataRegistry([
        ResolvedWindowMetadata(
            slug="btc-updown-15m-1000",
            condition_id="cond-1",
            window_end_ns=1_000,
            yes_token_id="yes-1",
            no_token_id="no-1",
            yes_outcome_label="Up",
            no_outcome_label="Down",
            selected_outcome_side="yes",
        ),
        ResolvedWindowMetadata(
            slug="btc-updown-15m-1900",
            condition_id="cond-2",
            window_end_ns=1_900,
            yes_token_id="yes-2",
            no_token_id="no-2",
            yes_outcome_label="Up",
            no_outcome_label="Down",
            selected_outcome_side="yes",
        ),
    ])


def test_registry_indexes_tokens_and_instruments():
    registry = _registry()

    assert registry.allowed_condition_ids() == frozenset({"cond-1", "cond-2"})
    assert registry.allowed_token_ids() == frozenset({"yes-1", "no-1", "yes-2", "no-2"})
    assert registry.token("yes-1").instrument_id == "cond-1-yes-1.POLYMARKET"
    assert registry.token_for_instrument("cond-1-no-1.POLYMARKET").outcome_side == "no"
    assert registry.contains(condition_id="cond-2", token_id="no-2") is True
    assert registry.contains(condition_id="cond-2", token_id="yes-1") is False


def test_prod_wallet_truth_provider_filters_positions_to_registry(monkeypatch):
    registry = _registry()

    class FakeBalanceClient:
        def get_balance_allowance(self, params):
            assert params.asset_type == "COLLATERAL"
            return {"balance": "12500000"}

    responses = [
        [
            {
                "conditionId": "cond-1",
                "asset": "yes-1",
                "size": "2.5",
                "redeemable": False,
                "mergeable": False,
            },
            {
                "conditionId": "foreign",
                "asset": "other",
                "size": "99",
                "redeemable": True,
                "mergeable": False,
            },
        ],
        [],
    ]

    monkeypatch.setattr(
        "live.wallet_truth.requests.get",
        lambda url, params, timeout: DummyResponse(responses.pop(0)),
    )

    provider = ProdWalletTruthProvider(
        wallet_address="0xabc",
        balance_client=FakeBalanceClient(),
        registry=registry,
    )
    snapshot = provider.snapshot()

    assert snapshot.wallet_address == "0xabc"
    assert snapshot.collateral_balance == 12.5
    assert len(snapshot.positions) == 1
    position = snapshot.positions[0]
    assert position.condition_id == "cond-1"
    assert position.token_id == "yes-1"
    assert position.instrument_id == "cond-1-yes-1.POLYMARKET"
    assert position.outcome_side == "yes"
    assert position.outcome_label == "Up"
    assert position.size == 2.5


def test_prod_wallet_truth_provider_can_include_unregistered_positions(monkeypatch):
    registry = _registry()

    class FakeBalanceClient:
        def get_balance_allowance(self, params):
            return {"balance": "12500000"}

    responses = [
        [
            {
                "conditionId": "cond-1",
                "asset": "yes-1",
                "size": "2.5",
                "redeemable": False,
                "mergeable": False,
            },
            {
                "conditionId": "foreign",
                "asset": "other",
                "size": "99",
                "redeemable": True,
                "mergeable": False,
            },
        ],
        [],
    ]

    monkeypatch.setattr(
        "live.wallet_truth.requests.get",
        lambda url, params, timeout: DummyResponse(responses.pop(0)),
    )

    provider = ProdWalletTruthProvider(
        wallet_address="0xabc",
        balance_client=FakeBalanceClient(),
        registry=registry,
        restrict_to_registry=False,
    )
    snapshot = provider.snapshot()

    assert len(snapshot.positions) == 2
    foreign = next(position for position in snapshot.positions if position.condition_id == "foreign")
    assert foreign.token_id == "other"
    assert foreign.instrument_id == "foreign-other.POLYMARKET"
    assert foreign.outcome_side == "unknown"
    assert foreign.outcome_label is None
    assert foreign.window_slug == ""
    assert foreign.window_end_ns == 0
    assert foreign.redeemable is True


def test_sandbox_wallet_truth_provider_and_resolution_worker_settle_winner():
    registry = _registry()
    store = SandboxWalletStore(wallet_address="sandbox-wallet", collateral_balance=10.0)
    store.set_position_size("yes-1", 3.0)
    store.set_position_size("no-1", 1.0)
    provider = SandboxWalletTruthProvider(wallet_store=store, registry=registry)
    executor = SandboxResolutionExecutor(store)

    worker = ResolutionWorker(
        registry=registry,
        wallet_truth_provider=provider,
        executor=executor,
        resolution_fetcher=lambda condition_id, token_id: MarketResolution(
            condition_id=condition_id,
            token_id=token_id,
            market_closed=True,
            target_token_outcome="Up",
            winning_token_id="yes-1",
            winning_outcome="Up",
        ),
    )

    results = worker.scan_once()

    assert len(results) == 2
    results_by_token = {result.token_id: result for result in results}
    assert results_by_token["yes-1"].status == "settled"
    assert results_by_token["yes-1"].settlement_price == 1.0
    assert results_by_token["yes-1"].token_won is True
    assert results_by_token["no-1"].status == "settled"
    assert results_by_token["no-1"].settlement_price == 0.0
    assert results_by_token["no-1"].token_won is False
    assert store.collateral_balance == 13.0
    assert provider.snapshot().positions == ()
    assert {settlement.token_id for settlement in provider.snapshot().settlements} == {"yes-1", "no-1"}


def test_resolution_worker_leaves_unresolved_position_pending():
    registry = _registry()
    store = SandboxWalletStore(wallet_address="sandbox-wallet", collateral_balance=10.0)
    store.set_position_size("yes-2", 4.0)
    provider = SandboxWalletTruthProvider(wallet_store=store, registry=registry)
    executor = SandboxResolutionExecutor(store)

    worker = ResolutionWorker(
        registry=registry,
        wallet_truth_provider=provider,
        executor=executor,
        resolution_fetcher=lambda condition_id, token_id: MarketResolution(
            condition_id=condition_id,
            token_id=token_id,
            market_closed=False,
            target_token_outcome="Up",
            winning_token_id=None,
            winning_outcome=None,
        ),
    )

    results = worker.scan_once()

    assert len(results) == 1
    assert results[0].condition_id == "cond-2"
    assert results[0].status == "pending"
    assert store.collateral_balance == 10.0
    assert provider.snapshot().position_for_token("yes-2").size == 4.0


def test_resolution_worker_can_process_unregistered_positions_when_enabled():
    class DummyProvider:
        def snapshot(self):
            return type(
                "Snapshot",
                (),
                {
                    "positions": (
                        WalletTokenPosition(
                            condition_id="foreign-cond",
                            token_id="foreign-token",
                            instrument_id="foreign-cond-foreign-token.POLYMARKET",
                            outcome_side="unknown",
                            outcome_label=None,
                            size=4.0,
                            redeemable=True,
                            mergeable=False,
                            window_slug="",
                            window_end_ns=0,
                        ),
                    )
                },
            )()

    class DummyExecutor:
        def settle(self, *, positions, resolution):
            return [
                type(
                    "Result",
                    (),
                    {
                        "condition_id": positions[0].condition_id,
                        "instrument_id": positions[0].instrument_id,
                        "token_id": positions[0].token_id,
                        "position_size": positions[0].size,
                        "resolved": True,
                        "settlement_price": 1.0,
                        "token_won": True,
                        "status": "ready_to_redeem",
                        "transaction_hash": None,
                    },
                )()
            ]

    worker = ResolutionWorker(
        registry=_registry(),
        wallet_truth_provider=DummyProvider(),
        executor=DummyExecutor(),
        restrict_to_registry=False,
        resolution_fetcher=lambda condition_id, token_id: MarketResolution(
            condition_id=condition_id,
            token_id=token_id,
            market_closed=True,
            target_token_outcome="Up",
            winning_token_id=token_id,
            winning_outcome="Up",
        ),
    )

    results = worker.scan_once()

    assert len(results) == 1
    assert results[0].condition_id == "foreign-cond"
    assert results[0].instrument_id == "foreign-cond-foreign-token.POLYMARKET"
    assert results[0].status == "ready_to_redeem"


def test_resolution_worker_converts_redemption_exception_into_retryable_results():
    registry = _registry()
    store = SandboxWalletStore(wallet_address="sandbox-wallet", collateral_balance=10.0)
    store.set_position_size("yes-1", 3.0)
    store.set_position_size("yes-2", 4.0)
    provider = SandboxWalletTruthProvider(wallet_store=store, registry=registry)

    class FlakyExecutor:
        def settle(self, *, positions, resolution):
            if resolution.condition_id == "cond-1":
                raise ConnectionError("rpc dropped")
            return [
                ResolutionScanResult(
                    condition_id=positions[0].condition_id,
                    instrument_id=positions[0].instrument_id,
                    token_id=positions[0].token_id,
                    position_size=positions[0].size,
                    resolved=True,
                    settlement_price=1.0,
                    token_won=True,
                    status="ready_to_redeem",
                )
            ]

    worker = ResolutionWorker(
        registry=registry,
        wallet_truth_provider=provider,
        executor=FlakyExecutor(),
        resolution_fetcher=lambda condition_id, token_id: MarketResolution(
            condition_id=condition_id,
            token_id=token_id,
            market_closed=True,
            target_token_outcome="Up",
            winning_token_id=token_id,
            winning_outcome="Up",
        ),
    )

    results = worker.scan_once()

    assert len(results) == 2
    results_by_token = {result.token_id: result for result in results}
    assert results_by_token["yes-1"].status == "redemption_error_transport"
    assert results_by_token["yes-1"].resolved is True
    assert results_by_token["yes-1"].settlement_price == 1.0
    assert results_by_token["yes-1"].token_won is True
    assert "rpc dropped" in results_by_token["yes-1"].error_message
    assert results_by_token["yes-2"].status == "ready_to_redeem"


def test_sandbox_wallet_store_persists_and_provider_reload_reads_new_state(tmp_path):
    registry = _registry()
    state_path = tmp_path / "wallet.json"

    writer = SandboxWalletStore(
        wallet_address="sandbox-wallet",
        collateral_balance=5.0,
        state_path=state_path,
    )
    writer.set_position_size("yes-1", 2.0)

    reader = SandboxWalletStore(
        wallet_address="ignored",
        collateral_balance=0.0,
        state_path=state_path,
    )
    provider = SandboxWalletTruthProvider(wallet_store=reader, registry=registry)

    initial = provider.snapshot()
    assert initial.collateral_balance == 5.0
    assert initial.position_for_token("yes-1").size == 2.0

    writer.set_collateral_balance(8.0)
    writer.set_position_size("yes-1", 0.0)
    writer.set_position_size("no-2", 1.5)

    updated = provider.snapshot()
    assert updated.collateral_balance == 8.0
    assert updated.position_for_token("yes-1") is None
    assert updated.position_for_token("no-2").size == 1.5

    writer.settle_token("no-2", 1.0)

    settled = provider.snapshot()
    assert settled.position_for_token("no-2") is None
    assert settled.settlement_for_token("no-2").collateral_credit == 1.5


def _position(
    *,
    condition_id: str = "cond-1",
    token_id: str = "yes-1",
    instrument_id: str = "cond-1-yes-1.POLYMARKET",
    outcome_side: str = "yes",
    outcome_label: str | None = "Up",
    size: float = 3.0,
    redeemable: bool = True,
    mergeable: bool = False,
) -> WalletTokenPosition:
    return WalletTokenPosition(
        condition_id=condition_id,
        token_id=token_id,
        instrument_id=instrument_id,
        outcome_side=outcome_side,
        outcome_label=outcome_label,
        size=size,
        redeemable=redeemable,
        mergeable=mergeable,
        window_slug="btc-updown-15m-1000",
        window_end_ns=1_000,
    )


def test_prod_redemption_executor_dry_run_marks_condition_ready_to_redeem():
    executor = ProdRedemptionExecutor(
        private_key="0x" + ("11" * 32),
        wallet_address="0x0000000000000000000000000000000000000001",
        rpc_url="http://localhost:8545",
        dry_run=True,
    )
    resolution = MarketResolution(
        condition_id="0x" + ("22" * 32),
        token_id="yes-1",
        market_closed=True,
        target_token_outcome="Up",
        winning_token_id="yes-1",
        winning_outcome="Up",
    )

    results = executor.settle(
        positions=(
            _position(token_id="yes-1", instrument_id="cond-1-yes-1.POLYMARKET", redeemable=True),
            _position(
                token_id="no-1",
                instrument_id="cond-1-no-1.POLYMARKET",
                outcome_side="no",
                outcome_label="Down",
                redeemable=False,
            ),
        ),
        resolution=resolution,
    )

    assert [result.status for result in results] == ["ready_to_redeem", "ready_to_redeem"]
    assert [result.settlement_price for result in results] == [1.0, 0.0]
    assert all(result.transaction_hash is None for result in results)


def test_prod_redemption_executor_requires_redeemable_position_by_default():
    executor = ProdRedemptionExecutor(
        private_key="0x" + ("11" * 32),
        wallet_address="0x0000000000000000000000000000000000000001",
        rpc_url="http://localhost:8545",
        dry_run=True,
    )
    resolution = MarketResolution(
        condition_id="0x" + ("22" * 32),
        token_id="yes-1",
        market_closed=True,
        target_token_outcome="Up",
        winning_token_id="yes-1",
        winning_outcome="Up",
    )

    results = executor.settle(
        positions=(
            _position(token_id="yes-1", redeemable=False),
            _position(
                token_id="no-1",
                instrument_id="cond-1-no-1.POLYMARKET",
                outcome_side="no",
                outcome_label="Down",
                redeemable=False,
            ),
        ),
        resolution=resolution,
    )

    assert [result.status for result in results] == ["not_redeemable", "not_redeemable"]
    assert [result.settlement_price for result in results] == [1.0, 0.0]


def test_prod_redemption_executor_retries_underpriced_replacement(monkeypatch):
    executor = ProdRedemptionExecutor(
        private_key="0x" + ("11" * 32),
        wallet_address="0x0000000000000000000000000000000000000001",
        rpc_url="http://localhost:8545",
        dry_run=False,
    )
    resolution = MarketResolution(
        condition_id="0x" + ("22" * 32),
        token_id="yes-1",
        market_closed=True,
        target_token_outcome="Up",
        winning_token_id="yes-1",
        winning_outcome="Up",
    )
    sent_gas_prices: list[int] = []

    class FakeTxHash:
        def __init__(self, value: str) -> None:
            self._value = value

        def hex(self) -> str:
            return self._value

    class FakeAccount:
        def sign_transaction(self, tx, private_key):
            return SimpleNamespace(raw_transaction=tx)

    class FakeEth:
        gas_price = 100
        account = FakeAccount()

        def get_transaction_count(self, wallet_address):
            return 7

        def estimate_gas(self, tx):
            return 55_000

        def send_raw_transaction(self, raw_tx):
            sent_gas_prices.append(raw_tx["gasPrice"])
            if len(sent_gas_prices) == 1:
                raise Web3RPCError({"code": -32000, "message": "replacement transaction underpriced"})
            return FakeTxHash("0xabc")

        def wait_for_transaction_receipt(self, tx_hash):
            return {"status": 1}

    class FakeRedeemCall:
        def build_transaction(self, params):
            return dict(params)

    class FakeFunctions:
        def redeemPositions(self, *args):
            return FakeRedeemCall()

    executor._w3 = SimpleNamespace(eth=FakeEth())
    executor._ctf = SimpleNamespace(functions=FakeFunctions())

    results = executor.settle(
        positions=(
            _position(token_id="yes-1", instrument_id="cond-1-yes-1.POLYMARKET", redeemable=True),
        ),
        resolution=resolution,
    )

    assert len(results) == 1
    assert results[0].status == "redeemed"
    assert results[0].transaction_hash == "0xabc"
    assert sent_gas_prices == [100, 113]


def test_prod_redemption_executor_marks_timeout_as_pending_receipt():
    executor = ProdRedemptionExecutor(
        private_key="0x" + ("11" * 32),
        wallet_address="0x0000000000000000000000000000000000000001",
        rpc_url="http://localhost:8545",
        dry_run=False,
    )
    resolution = MarketResolution(
        condition_id="0x" + ("22" * 32),
        token_id="yes-1",
        market_closed=True,
        target_token_outcome="Up",
        winning_token_id="yes-1",
        winning_outcome="Up",
    )

    class FakeTxHash:
        def hex(self) -> str:
            return "0xdef"

    class FakeAccount:
        def sign_transaction(self, tx, private_key):
            return SimpleNamespace(raw_transaction=tx)

    class FakeEth:
        gas_price = 100
        account = FakeAccount()

        def get_transaction_count(self, wallet_address):
            return 7

        def estimate_gas(self, tx):
            return 55_000

        def send_raw_transaction(self, raw_tx):
            return FakeTxHash()

        def wait_for_transaction_receipt(self, tx_hash):
            raise TimeExhausted("receipt timeout")

    class FakeRedeemCall:
        def build_transaction(self, params):
            return dict(params)

    class FakeFunctions:
        def redeemPositions(self, *args):
            return FakeRedeemCall()

    executor._w3 = SimpleNamespace(eth=FakeEth())
    executor._ctf = SimpleNamespace(functions=FakeFunctions())

    results = executor.settle(
        positions=(
            _position(token_id="yes-1", instrument_id="cond-1-yes-1.POLYMARKET", redeemable=True),
        ),
        resolution=resolution,
    )

    assert len(results) == 1
    assert results[0].status == "submitted_pending_receipt"
    assert results[0].transaction_hash == "0xdef"


def test_prod_redemption_executor_marks_exhausted_underpriced_replacement_as_blocked():
    executor = ProdRedemptionExecutor(
        private_key="0x" + ("11" * 32),
        wallet_address="0x0000000000000000000000000000000000000001",
        rpc_url="http://localhost:8545",
        dry_run=False,
    )
    resolution = MarketResolution(
        condition_id="0x" + ("22" * 32),
        token_id="yes-1",
        market_closed=True,
        target_token_outcome="Up",
        winning_token_id="yes-1",
        winning_outcome="Up",
    )

    class FakeAccount:
        def sign_transaction(self, tx, private_key):
            return SimpleNamespace(raw_transaction=tx)

    class FakeEth:
        gas_price = 100
        account = FakeAccount()

        def get_transaction_count(self, wallet_address):
            return 7

        def estimate_gas(self, tx):
            return 55_000

        def send_raw_transaction(self, raw_tx):
            raise Web3RPCError({"code": -32000, "message": "replacement transaction underpriced"})

        def wait_for_transaction_receipt(self, tx_hash):
            raise AssertionError("wait_for_transaction_receipt should not be called")

    class FakeRedeemCall:
        def build_transaction(self, params):
            return dict(params)

    class FakeFunctions:
        def redeemPositions(self, *args):
            return FakeRedeemCall()

    executor._w3 = SimpleNamespace(eth=FakeEth())
    executor._ctf = SimpleNamespace(functions=FakeFunctions())

    results = executor.settle(
        positions=(
            _position(token_id="yes-1", instrument_id="cond-1-yes-1.POLYMARKET", redeemable=True),
        ),
        resolution=resolution,
    )

    assert len(results) == 1
    assert results[0].status == "submission_blocked_underpriced"
    assert results[0].transaction_hash is None


def test_bytes32_rejects_non_32_byte_hex():
    try:
        _bytes32("0x1234")
    except ValueError as exc:
        assert "Expected 32-byte hex value" in str(exc)
    else:
        raise AssertionError("Expected _bytes32 to reject short hex input")


def test_format_tx_hash_adds_missing_0x_prefix():
    class FakeTxHash:
        def hex(self) -> str:
            return "abcd"

    assert _format_tx_hash(FakeTxHash()) == "0xabcd"
