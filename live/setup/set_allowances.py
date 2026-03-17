#!/usr/bin/env python3
"""
Step 3: Approve Polymarket exchange contracts to spend from your trading wallet.

Does three things:
1. On-chain ERC20 approve() for each exchange contract — allows spending USDC (for BUY orders)
2. On-chain ERC1155 setApprovalForAll() on the CTF contract — allows transferring outcome tokens (for SELL orders)
3. Syncs Polymarket's API record of your balance/allowance

Run this once after funding your wallet.

Usage:
    python live/setup/set_allowances.py
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from live.env import add_env_file_arg, project_dotenv_values, resolve_env_path

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-bor-rpc.publicnode.com"

# USDC.e on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# CTF (Conditional Token Framework) contract — holds ERC1155 outcome tokens
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Polymarket exchange contracts that need both USDC + CTF approval
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

ERC1155_ABI = [
    {
        "name": "setApprovalForAll",
        "type": "function",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "isApprovedForAll",
        "type": "function",
        "inputs": [
            {"name": "account",  "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

MAX_UINT256 = 2**256 - 1


def load_env(env_file: str | None) -> dict:
    env = project_dotenv_values(env_file)
    missing = [k for k in ("PRIVATE_KEY", "WALLET_ADDRESS", "POLYMARKET_API_KEY") if not env.get(k)]
    if missing:
        print(f"ERROR: Missing in {resolve_env_path(env_file)}: {', '.join(missing)}")
        print("  Run generate_wallet.py and init_trading.py first")
        sys.exit(1)
    return env


def approve_usdc(w3: Web3, private_key: str, wallet: str) -> None:
    """ERC20 approve — lets exchange contracts spend USDC (required for BUY orders)."""
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_ABI,
    )
    wallet_cs = Web3.to_checksum_address(wallet)

    for name, contract_addr in EXCHANGE_CONTRACTS:
        contract_addr = Web3.to_checksum_address(contract_addr)
        current = usdc.functions.allowance(wallet_cs, contract_addr).call()
        if current > 0:
            print(f"  {name}: already approved ({current / 1e6:.2f} USDC allowance)")
            continue

        print(f"  {name}: approving USDC...")
        tx = usdc.functions.approve(contract_addr, MAX_UINT256).build_transaction({
            "from":  wallet_cs,
            "nonce": w3.eth.get_transaction_count(wallet_cs),
            "gas":   100_000,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        print(f"    confirmed")


def approve_ctf(w3: Web3, private_key: str, wallet: str) -> None:
    """ERC1155 setApprovalForAll — lets exchange contracts transfer outcome tokens (required for SELL orders)."""
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=ERC1155_ABI,
    )
    wallet_cs = Web3.to_checksum_address(wallet)

    for name, contract_addr in EXCHANGE_CONTRACTS:
        contract_addr = Web3.to_checksum_address(contract_addr)
        already = ctf.functions.isApprovedForAll(wallet_cs, contract_addr).call()
        if already:
            print(f"  {name}: CTF already approved")
            continue

        print(f"  {name}: approving CTF (outcome tokens)...")
        tx = ctf.functions.setApprovalForAll(contract_addr, True).build_transaction({
            "from":  wallet_cs,
            "nonce": w3.eth.get_transaction_count(wallet_cs),
            "gas":   100_000,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        print(f"    confirmed")


def sync_polymarket(client: ClobClient) -> None:
    print("\nSyncing allowances with Polymarket API...")
    client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))

    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  Balance:    {int(bal['balance']) / 1e6:.2f} USDC")
    for addr, allowance in bal.get("allowances", {}).items():
        print(f"  Allowance [{addr[:10]}...]: {int(allowance) / 1e6:.2f} USDC")


def main() -> None:
    parser = argparse.ArgumentParser()
    add_env_file_arg(parser)
    args = parser.parse_args()

    env = load_env(args.env_file)
    wallet = env["WALLET_ADDRESS"]
    print(f"Wallet: {wallet}")
    print()

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print("ERROR: Cannot connect to Polygon RPC")
        sys.exit(1)

    print("Setting on-chain USDC approvals (required for BUY orders)...")
    approve_usdc(w3, env["PRIVATE_KEY"], wallet)

    print("\nSetting on-chain CTF approvals (required for SELL orders)...")
    approve_ctf(w3, env["PRIVATE_KEY"], wallet)

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
