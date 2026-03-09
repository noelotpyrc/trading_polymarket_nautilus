#!/usr/bin/env python3
"""
Step 1: Generate a new EOA wallet.

Default: writes PRIVATE_KEY and WALLET_ADDRESS to .env (production wallet).
--test:  writes POLYMARKET_TEST_PRIVATE_KEY and POLYMARKET_TEST_WALLET_ADDRESS (sandbox wallet).

Run each mode once only — re-running will abort if the key already exists.

Usage:
    python live/setup/generate_wallet.py           # production wallet
    python live/setup/generate_wallet.py --test    # sandbox/test wallet (no funding needed)
"""
import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values
from eth_account import Account

ENV_PATH = Path(__file__).parents[2] / ".env"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", action="store_true",
        help="Generate a test wallet (zero-funds, sandbox mode only)"
    )
    args = parser.parse_args()

    if args.test:
        key_var, addr_var = "POLYMARKET_TEST_PRIVATE_KEY", "POLYMARKET_TEST_WALLET_ADDRESS"
        label = "TEST"
    else:
        key_var, addr_var = "PRIVATE_KEY", "WALLET_ADDRESS"
        label = "PRODUCTION"

    existing = dotenv_values(ENV_PATH)
    if existing.get(key_var):
        print(f"ERROR: .env already contains {key_var}. Aborting to avoid overwriting.")
        print(f"  Address: {existing.get(addr_var, '(unknown)')}")
        sys.exit(1)

    account = Account.create()
    private_key = account.key.hex()
    address = account.address

    with ENV_PATH.open("a") as f:
        f.write(f"{key_var}={private_key}\n")
        f.write(f"{addr_var}={address}\n")

    print(f"{label} wallet generated and saved to .env")
    print(f"  Address:     {address}")
    print(f"  Private key: {private_key}")
    print()

    if args.test:
        print("Next step (no funding needed):")
        print("  python live/setup/init_trading.py --test")
    else:
        print("Next steps:")
        print("  1. Send USDC.e to your address on Polygon")
        print("  2. Send a small amount of POL (for gas) to your address on Polygon")
        print("  3. Run: python live/setup/init_trading.py")


if __name__ == "__main__":
    main()
