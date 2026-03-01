#!/usr/bin/env python3
"""
Step 1: Generate a new EOA wallet for Polymarket trading.

Writes PRIVATE_KEY and WALLET_ADDRESS to .env in the project root.
Run this once — do NOT run again or it will generate a different wallet.

Usage:
    python live/setup/generate_wallet.py
"""

import sys
from pathlib import Path

from eth_account import Account
from dotenv import dotenv_values

ENV_PATH = Path(__file__).parents[2] / ".env"


def main() -> None:
    # Guard: don't overwrite an existing key
    existing = dotenv_values(ENV_PATH)
    if existing.get("PRIVATE_KEY"):
        print("ERROR: .env already contains a PRIVATE_KEY. Aborting to avoid overwriting.")
        print(f"  Wallet address: {existing.get('WALLET_ADDRESS', '(unknown)')}")
        sys.exit(1)

    account = Account.create()
    private_key = account.key.hex()
    address = account.address

    with ENV_PATH.open("a") as f:
        f.write(f"PRIVATE_KEY={private_key}\n")
        f.write(f"WALLET_ADDRESS={address}\n")

    print("Wallet generated and saved to .env")
    print(f"  Address:     {address}")
    print(f"  Private key: {private_key}")
    print()
    print("Next steps:")
    print("  1. Send USDC.e to your address on Polygon")
    print("  2. Send a small amount of POL (for gas) to your address on Polygon")
    print("  3. Run: python live/setup/init_trading.py")


if __name__ == "__main__":
    main()
