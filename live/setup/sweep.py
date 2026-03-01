#!/usr/bin/env python3
"""
Sweep excess USDC from the trading wallet to a safe wallet.

Run this manually or on a schedule before/after trading sessions.
Keeps a minimum balance in the trading wallet and transfers the rest.

TODO before deployment:
    - Implement the actual USDC transfer transaction
    - Decide on KEEP_AMOUNT (how much to leave in trading wallet)
    - Set SAFE_WALLET_ADDRESS in .env
    - Optionally schedule via cron or integrate into trading loop

Usage:
    python live/setup/sweep.py
"""

import sys
from pathlib import Path
from dotenv import dotenv_values

ENV_PATH = Path(__file__).parents[2] / ".env"

# How much USDC to keep in the trading wallet (in USD)
KEEP_AMOUNT = 50.0


def main() -> None:
    env = dotenv_values(ENV_PATH)

    trading_wallet = env.get("WALLET_ADDRESS")
    safe_wallet = env.get("SAFE_WALLET_ADDRESS")

    if not trading_wallet:
        print("ERROR: WALLET_ADDRESS not set in .env")
        sys.exit(1)

    if not safe_wallet:
        print("ERROR: SAFE_WALLET_ADDRESS not set in .env")
        print("  Add your safe wallet address to .env first")
        sys.exit(1)

    print(f"Trading wallet : {trading_wallet}")
    print(f"Safe wallet    : {safe_wallet}")
    print(f"Keep amount    : ${KEEP_AMOUNT} USDC")
    print()

    # TODO: fetch actual USDC balance from Polygon
    # from web3 import Web3
    # w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    # usdc_contract = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
    # balance = usdc_contract.functions.balanceOf(trading_wallet).call() / 1e6

    # TODO: if balance > KEEP_AMOUNT, transfer (balance - KEEP_AMOUNT) to safe_wallet

    raise NotImplementedError("Sweep not implemented yet — complete before deployment")


if __name__ == "__main__":
    main()
