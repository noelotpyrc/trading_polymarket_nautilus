"""Production redemption backend for external Polymarket resolution handling."""
from __future__ import annotations

from dataclasses import dataclass

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from live.resolution import MarketResolution
from live.resolution_worker import ResolutionExecutor, ResolutionScanResult
from live.wallet_truth import WalletTokenPosition

POLYGON_CHAIN_ID = 137
DEFAULT_POLYGON_RPC_URL = "https://polygon-bor-rpc.publicnode.com"
CTF_CONTRACT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_CONTRACT_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_BINARY_INDEX_SETS = [1, 2]

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

        tx_hash = None if self._dry_run else self._redeem_condition(resolution.condition_id)
        status = "ready_to_redeem" if self._dry_run else "redeemed"
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

    def _redeem_condition(self, condition_id: str) -> str:
        tx = self._ctf.functions.redeemPositions(
            self._collateral_address,
            bytes(32),
            _bytes32(condition_id),
            _BINARY_INDEX_SETS,
        ).build_transaction(
            {
                "from": self._wallet_address,
                "nonce": self._w3.eth.get_transaction_count(self._wallet_address),
                "chainId": self._chain_id,
                "gasPrice": self._w3.eth.gas_price,
            }
        )
        tx["gas"] = self._w3.eth.estimate_gas(tx)
        signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        self._w3.eth.wait_for_transaction_receipt(tx_hash)
        return tx_hash.hex()


def _bytes32(value: str) -> bytes:
    value = value.removeprefix("0x")
    raw = bytes.fromhex(value)
    if len(raw) != 32:
        raise ValueError(f"Expected 32-byte hex value, got {len(raw)} bytes")
    return raw
