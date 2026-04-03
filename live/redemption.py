"""Production redemption backend for external Polymarket resolution handling."""
from __future__ import annotations

from dataclasses import dataclass
import math

from web3 import Web3
from web3.exceptions import TimeExhausted, Web3RPCError
from web3.middleware import ExtraDataToPOAMiddleware

from live.resolution import MarketResolution
from live.resolution_worker import ResolutionExecutor, ResolutionScanResult
from live.wallet_truth import WalletTokenPosition

POLYGON_CHAIN_ID = 137
DEFAULT_POLYGON_RPC_URL = "https://polygon-bor-rpc.publicnode.com"
CTF_CONTRACT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_CONTRACT_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_BINARY_INDEX_SETS = [1, 2]
_REPLACEMENT_SEND_MAX_ATTEMPTS = 5
_REPLACEMENT_GAS_BUMP_FACTOR = 1.125

CTF_REDEEM_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
]


@dataclass(frozen=True)
class RedemptionTxResult:
    tx_hash: str
    receipt_confirmed: bool


class RedemptionSubmissionBlocked(RuntimeError):
    """Raised when a replacement-style resend cannot dislodge an existing pending tx."""


class ProdRedemptionExecutor(ResolutionExecutor):
    def __init__(
        self,
        *,
        private_key: str,
        wallet_address: str,
        rpc_url: str = DEFAULT_POLYGON_RPC_URL,
        ctf_contract_address: str = CTF_CONTRACT_ADDRESS,
        collateral_address: str = USDC_CONTRACT_ADDRESS,
        chain_id: int = POLYGON_CHAIN_ID,
        dry_run: bool = True,
        require_redeemable: bool = True,
    ) -> None:
        self._private_key = private_key
        self._wallet_address = Web3.to_checksum_address(wallet_address)
        self._collateral_address = Web3.to_checksum_address(collateral_address)
        self._chain_id = chain_id
        self._dry_run = dry_run
        self._require_redeemable = require_redeemable

        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._ctf = self._w3.eth.contract(
            address=Web3.to_checksum_address(ctf_contract_address),
            abi=CTF_REDEEM_ABI,
        )

    def settle(
        self,
        *,
        positions: tuple[WalletTokenPosition, ...],
        resolution: MarketResolution,
    ) -> list[ResolutionScanResult]:
        if self._require_redeemable and not any(position.redeemable for position in positions):
            return [
                ResolutionScanResult(
                    condition_id=position.condition_id,
                    instrument_id=position.instrument_id,
                    token_id=position.token_id,
                    position_size=position.size,
                    resolved=True,
                    settlement_price=1.0 if position.token_id == resolution.winning_token_id else 0.0,
                    token_won=position.token_id == resolution.winning_token_id,
                    status="not_redeemable",
                )
                for position in positions
            ]

        if self._dry_run:
            status = "ready_to_redeem"
            tx_hash = None
        else:
            try:
                tx_result = self._redeem_condition(resolution.condition_id)
            except RedemptionSubmissionBlocked:
                status = "submission_blocked_underpriced"
                tx_hash = None
            else:
                status = "redeemed" if tx_result.receipt_confirmed else "submitted_pending_receipt"
                tx_hash = tx_result.tx_hash
        return [
            ResolutionScanResult(
                condition_id=position.condition_id,
                instrument_id=position.instrument_id,
                token_id=position.token_id,
                position_size=position.size,
                resolved=True,
                settlement_price=1.0 if position.token_id == resolution.winning_token_id else 0.0,
                token_won=position.token_id == resolution.winning_token_id,
                status=status,
                transaction_hash=tx_hash,
            )
            for position in positions
        ]

    def _redeem_condition(self, condition_id: str) -> RedemptionTxResult:
        nonce = self._w3.eth.get_transaction_count(self._wallet_address)
        gas_price = self._w3.eth.gas_price
        tx_hash = None
        last_underpriced_exc: Web3RPCError | None = None
        for _ in range(_REPLACEMENT_SEND_MAX_ATTEMPTS):
            tx = self._ctf.functions.redeemPositions(
                self._collateral_address,
                bytes(32),
                _bytes32(condition_id),
                _BINARY_INDEX_SETS,
            ).build_transaction(
                {
                    "from": self._wallet_address,
                    "nonce": nonce,
                    "chainId": self._chain_id,
                    "gasPrice": gas_price,
                }
            )
            tx["gas"] = self._w3.eth.estimate_gas(tx)
            signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
            try:
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                break
            except Web3RPCError as exc:
                if not _is_underpriced_replacement(exc):
                    raise
                last_underpriced_exc = exc
                gas_price = _bump_gas_price(gas_price)
        else:
            raise RedemptionSubmissionBlocked(
                f"Could not replace pending redemption transaction for nonce={nonce} "
                f"after {_REPLACEMENT_SEND_MAX_ATTEMPTS} attempts"
            ) from last_underpriced_exc

        try:
            self._w3.eth.wait_for_transaction_receipt(tx_hash)
        except TimeExhausted:
            return RedemptionTxResult(tx_hash=_format_tx_hash(tx_hash), receipt_confirmed=False)

        return RedemptionTxResult(tx_hash=_format_tx_hash(tx_hash), receipt_confirmed=True)


def _bytes32(value: str) -> bytes:
    value = value.removeprefix("0x")
    raw = bytes.fromhex(value)
    if len(raw) != 32:
        raise ValueError(f"Expected 32-byte hex value, got {len(raw)} bytes")
    return raw


def _is_underpriced_replacement(exc: Web3RPCError) -> bool:
    return "replacement transaction underpriced" in str(exc).lower()


def _bump_gas_price(gas_price: int) -> int:
    return max(gas_price + 1, math.ceil(gas_price * _REPLACEMENT_GAS_BUMP_FACTOR))


def _format_tx_hash(tx_hash) -> str:
    value = tx_hash.hex()
    return value if value.startswith("0x") else f"0x{value}"
