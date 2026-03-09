#!/usr/bin/env python3
"""
Step 2: Derive Polymarket L2 API credentials from a private key.

No funding required — pure cryptographic signing, no on-chain tx.

Default: reads PRIVATE_KEY, writes POLYMARKET_API_* (production).
--test:  reads POLYMARKET_TEST_PRIVATE_KEY, writes POLYMARKET_TEST_API_* (sandbox).

Usage:
    python live/setup/init_trading.py           # production credentials
    python live/setup/init_trading.py --test    # test credentials (no funding needed)
"""
import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
ENV_PATH = Path(__file__).parents[2] / ".env"


def load_env(private_key_var: str, addr_var: str) -> dict:
    env = dotenv_values(ENV_PATH)
    if not env.get(private_key_var):
        print(f"ERROR: No {private_key_var} found in .env")
        flag = " --test" if "TEST" in private_key_var else ""
        print(f"  Run: python live/setup/generate_wallet.py{flag} first")
        sys.exit(1)
    return env


def build_client(private_key: str) -> ClobClient:
    return ClobClient(
        host=HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=0,
    )


def verify_connection(client: ClobClient) -> None:
    print("Verifying connection to CLOB API...")
    try:
        ok = client.get_ok()
        print(f"  {ok}")
    except Exception as e:
        print(f"  WARNING: Could not reach CLOB API: {e}")


def derive_and_save_creds(
    client: ClobClient,
    key_var: str,
    secret_var: str,
    passphrase_var: str,
) -> ApiCreds:
    env = dotenv_values(ENV_PATH)

    if env.get(key_var):
        print(f"API credentials already in .env ({key_var}) — skipping derivation")
        return ApiCreds(
            api_key=env[key_var],
            api_secret=env[secret_var],
            api_passphrase=env[passphrase_var],
        )

    print("Deriving API credentials...")
    creds = client.create_or_derive_api_creds()
    set_key(ENV_PATH, key_var, creds.api_key)
    set_key(ENV_PATH, secret_var, creds.api_secret)
    set_key(ENV_PATH, passphrase_var, creds.api_passphrase)
    print("  Saved to .env")
    return creds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", action="store_true",
        help="Derive credentials for the test wallet (sandbox mode)"
    )
    args = parser.parse_args()

    if args.test:
        private_key_var = "POLYMARKET_TEST_PRIVATE_KEY"
        addr_var = "POLYMARKET_TEST_WALLET_ADDRESS"
        api_key_var = "POLYMARKET_TEST_API_KEY"
        api_secret_var = "POLYMARKET_TEST_API_SECRET"
        api_passphrase_var = "POLYMARKET_TEST_API_PASSPHRASE"
        label = "TEST"
    else:
        private_key_var = "PRIVATE_KEY"
        addr_var = "WALLET_ADDRESS"
        api_key_var = "POLYMARKET_API_KEY"
        api_secret_var = "POLYMARKET_API_SECRET"
        api_passphrase_var = "POLYMARKET_API_PASSPHRASE"
        label = "PRODUCTION"

    env = load_env(private_key_var, addr_var)
    print(f"{label} wallet: {env.get(addr_var, '(unknown)')}")
    print()

    client = build_client(env[private_key_var])
    verify_connection(client)
    print()

    creds = derive_and_save_creds(client, api_key_var, api_secret_var, api_passphrase_var)
    client.set_api_creds(creds)

    print()
    print("API credentials ready.")
    if args.test:
        print("Test wallet setup complete. No funding needed.")
        print(
            "Run sandbox mode: "
            "python live/runs/profiles/random_signal_15m_sandbox.py"
        )
    else:
        print("Next step (requires POL for gas): python live/setup/set_allowances.py")


if __name__ == "__main__":
    main()
