#!/usr/bin/env python3
"""
Step 2: Derive Polymarket L2 API credentials from your private key.

No funding required — this is pure cryptographic signing, no on-chain tx.

Usage:
    python live/setup/init_trading.py
"""

import sys
from pathlib import Path

from dotenv import dotenv_values, set_key
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
ENV_PATH = Path(__file__).parents[2] / ".env"


def load_env() -> dict:
    env = dotenv_values(ENV_PATH)
    if not env.get("PRIVATE_KEY"):
        print("ERROR: No PRIVATE_KEY found in .env")
        print("  Run: python live/setup/generate_wallet.py first")
        sys.exit(1)
    return env


def build_client(private_key: str) -> ClobClient:
    return ClobClient(
        host=HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=0,  # Standard EOA
    )


def verify_connection(client: ClobClient) -> None:
    print("Verifying connection to CLOB API...")
    try:
        ok = client.get_ok()
        print(f"  {ok}")
    except Exception as e:
        print(f"  WARNING: Could not reach CLOB API: {e}")


def derive_and_save_creds(client: ClobClient) -> ApiCreds:
    env = dotenv_values(ENV_PATH)

    if env.get("POLYMARKET_API_KEY"):
        print("API credentials already in .env — skipping derivation")
        return ApiCreds(
            api_key=env["POLYMARKET_API_KEY"],
            api_secret=env["POLYMARKET_API_SECRET"],
            api_passphrase=env["POLYMARKET_API_PASSPHRASE"],
        )

    print("Deriving API credentials...")
    creds = client.create_or_derive_api_creds()
    set_key(ENV_PATH, "POLYMARKET_API_KEY", creds.api_key)
    set_key(ENV_PATH, "POLYMARKET_API_SECRET", creds.api_secret)
    set_key(ENV_PATH, "POLYMARKET_API_PASSPHRASE", creds.api_passphrase)
    print("  Saved to .env")
    return creds


def main() -> None:
    env = load_env()
    print(f"Wallet: {env.get('WALLET_ADDRESS', '(unknown)')}")
    print()

    client = build_client(env["PRIVATE_KEY"])
    verify_connection(client)
    print()

    creds = derive_and_save_creds(client)
    client.set_api_creds(creds)

    print()
    print("API credentials ready.")
    print("Next step (requires POL for gas): python live/setup/set_allowances.py")


if __name__ == "__main__":
    main()
