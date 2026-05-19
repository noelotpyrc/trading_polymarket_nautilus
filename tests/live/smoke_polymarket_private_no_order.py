#!/usr/bin/env python3
"""Smoke Polymarket private V2 read paths without placing orders.

This script validates the Nautilus 1.226 Polymarket HTTP client factory against
private CLOB V2 endpoints using either test-wallet or production credentials.
It performs read-only/control-plane calls only:

- collateral balance/allowance
- open orders
- recent trades

Usage:
    POLYMARKET_PRIVATE_NO_ORDER_SMOKE=1 ./.venv/bin/python \
        tests/live/smoke_polymarket_private_no_order.py --profile test

    POLYMARKET_PRIVATE_NO_ORDER_SMOKE=1 ./.venv/bin/python \
        tests/live/smoke_polymarket_private_no_order.py --profile prod
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import AssetType
from py_clob_client_v2.clob_types import BalanceAllowanceParams
from py_clob_client_v2.clob_types import TradeParams
from py_clob_client_v2.constants import POLYGON

from live.env import add_env_file_arg
from live.env import resolve_env_path


def _die(message: str) -> NoReturn:
    raise SystemExit(message)


def _env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    _die(f"Missing env var; expected one of: {', '.join(names)}")


def _credential_names(profile: str) -> dict[str, tuple[str, ...]]:
    if profile == "test":
        return {
            "private_key": ("POLYMARKET_TEST_PRIVATE_KEY",),
            "api_key": ("POLYMARKET_TEST_API_KEY",),
            "api_secret": ("POLYMARKET_TEST_API_SECRET",),
            "passphrase": ("POLYMARKET_TEST_API_PASSPHRASE",),
            "funder": ("POLYMARKET_TEST_WALLET_ADDRESS",),
            "signature_type": ("POLYMARKET_TEST_SIGNATURE_TYPE", "POLYMARKET_SIGNATURE_TYPE"),
        }

    return {
        "private_key": ("PRIVATE_KEY",),
        "api_key": ("POLYMARKET_API_KEY",),
        "api_secret": ("POLYMARKET_API_SECRET",),
        "passphrase": ("POLYMARKET_PASSPHRASE", "POLYMARKET_API_PASSPHRASE"),
        "funder": ("POLYMARKET_FUNDER", "WALLET_ADDRESS"),
        "signature_type": ("POLYMARKET_SIGNATURE_TYPE",),
    }


def _optional_int(names: tuple[str, ...], default: int) -> int:
    for name in names:
        value = os.getenv(name)
        if value:
            return int(value)
    return default


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("test", "prod"),
        default="test",
        help="Credential profile to use. Default: test.",
    )
    add_env_file_arg(parser)
    args = parser.parse_args()

    if os.getenv("POLYMARKET_PRIVATE_NO_ORDER_SMOKE") != "1":
        _die(
            "Set POLYMARKET_PRIVATE_NO_ORDER_SMOKE=1 to run live private "
            "Polymarket no-order smoke.",
        )

    env_path = resolve_env_path(args.env_file, wallet_profile=args.wallet_profile)
    load_dotenv(env_path, override=True)
    names = _credential_names(args.profile)

    private_key = _env(names["private_key"])
    api_key = _env(names["api_key"])
    api_secret = _env(names["api_secret"])
    passphrase = _env(names["passphrase"])
    funder = _env(names["funder"])
    signature_type = _optional_int(names["signature_type"], default=0)
    signer_address = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=POLYGON,
        key=private_key,
    ).signer.address()

    print(f"Polymarket private no-order smoke profile={args.profile}")
    print(f"env_file={env_path}")
    print(f"signer={signer_address}")
    print(f"funder={funder}")
    print(f"signature_type={signature_type}")
    print(f"api_key_prefix={api_key[:8]}")

    get_polymarket_http_client.cache_clear()
    client = get_polymarket_http_client(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        base_url="https://clob.polymarket.com",
        private_key=private_key,
        funder=funder,
        signature_type=signature_type,
    )
    print(f"client={type(client).__module__}.{type(client).__name__}")
    print(f"client_host={client.host}")

    balance = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type),
    )
    if not isinstance(balance, dict):
        _die(f"Unexpected balance response type: {type(balance)!r}")
    print(f"balance={balance.get('balance')}")
    allowances = balance.get("allowances")
    if isinstance(allowances, dict):
        print(f"allowance_keys={len(allowances)}")
    else:
        print("allowance_keys=unknown")

    orders = client.get_open_orders()
    if not isinstance(orders, list):
        _die(f"Unexpected open-orders response type: {type(orders)!r}")
    print(f"open_orders={len(orders)}")

    trades = client.get_trades(TradeParams(maker_address=funder), only_first_page=True)
    if not isinstance(trades, list):
        _die(f"Unexpected trades response type: {type(trades)!r}")
    print(f"trades={len(trades)}")

    print("Polymarket private no-order smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
