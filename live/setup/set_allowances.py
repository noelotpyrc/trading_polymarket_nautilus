#!/usr/bin/env python3
"""
Step 3: Approve Polymarket exchange contracts to spend USDC from your trading wallet.

Does two things:
1. On-chain ERC20 approve() for each exchange contract (requires POL for gas)
2. Syncs Polymarket's API record of your balance/allowance

Run this once after funding your wallet.

Usage:
    python live/setup/set_allowances.py
"""

import sys
from pathlib import Path

from dotenv import dotenv_values
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-bor-rpc.publicnode.com"
ENV_PATH = Path(__file__).parents[2] / ".env"

# USDC.e on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket exchange contracts that need USDC approval
EXCHANGE_CONTRACTS = [
    ("CTF Exchange",          "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("Neg Risk CTF Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
    ("Neg Risk Adapter",      "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

MAX_UINT256 = 2**256 - 1


def load_env() -> dict:
    env = dotenv_values(ENV_PATH)
    missing = [k for k in ("PRIVATE_KEY", "WALLET_ADDRESS", "POLYMARKET_API_KEY") if not env.get(k)]
    if missing:
        print(f"ERROR: Missing in .env: {', '.join(missing)}")
        print("  Run generate_wallet.py and init_trading.py first")
        sys.exit(1)
    return env


def approve_on_chain(w3: Web3, private_key: str, wallet: str) -> None:
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_ABI,
    )

    for name, contract_addr in EXCHANGE_CONTRACTS:
        contract_addr = Web3.to_checksum_address(contract_addr)

        # Check if already approved
        current = usdc.functions.allowance(
            Web3.to_checksum_address(wallet), contract_addr
        ).call()
        if current > 0:
            print(f"  {name}: already approved ({current / 1e6:.2f} USDC allowance)")
            continue

        print(f"  {name}: approving...")
        tx = usdc.functions.approve(contract_addr, MAX_UINT256).build_transaction({
            "from":  Web3.to_checksum_address(wallet),
            "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(wallet)),
            "gas":   100_000,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        print(f"    tx: {tx_hash.hex()}")


def sync_polymarket(client: ClobClient) -> None:
    print("\nSyncing allowances with Polymarket API...")
    client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))

    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  Balance:    {int(bal['balance']) / 1e6:.2f} USDC")
    for addr, allowance in bal.get("allowances", {}).items():
        print(f"  Allowance [{addr[:10]}...]: {int(allowance) / 1e6:.2f} USDC")


def main() -> None:
    env = load_env()
    wallet = env["WALLET_ADDRESS"]
    print(f"Wallet: {wallet}")
    print()

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print("ERROR: Cannot connect to Polygon RPC")
        sys.exit(1)

    print("Setting on-chain USDC approvals (requires POL for gas)...")
    approve_on_chain(w3, env["PRIVATE_KEY"], wallet)

    client = ClobClient(host=HOST, key=env["PRIVATE_KEY"], chain_id=CHAIN_ID, signature_type=0)
    client.set_api_creds(ApiCreds(
        api_key=env["POLYMARKET_API_KEY"],
        api_secret=env["POLYMARKET_API_SECRET"],
        api_passphrase=env["POLYMARKET_API_PASSPHRASE"],
    ))

    sync_polymarket(client)

    print()
    print("Setup complete. Your wallet is ready to trade on Polymarket.")


if __name__ == "__main__":
    main()
