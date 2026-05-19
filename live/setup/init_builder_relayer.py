#!/usr/bin/env python3
"""Create or inspect Polymarket builder API credentials for relayer use.

Default mode is read-only. Add `--execute` to create a builder key and write it
to the selected env file when no builder key exists.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import set_key
from eth_account import Account
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds
from py_clob_client_v2.constants import POLYGON

from live.env import add_env_file_arg
from live.env import project_dotenv_values
from live.env import resolve_env_path

HOST = "https://clob.polymarket.com"
DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com/"


@dataclass(frozen=True)
class BuilderRelayerEnv:
    env_path: Path
    private_key: str
    owner_address: str
    api_key: str
    api_secret: str
    api_passphrase: str
    builder_api_key: str | None
    builder_secret: str | None
    builder_passphrase: str | None


def _env_any(env: dict[str, str | None], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return None


def _require_any(env: dict[str, str | None], names: tuple[str, ...]) -> str:
    value = _env_any(env, names)
    if not value:
        raise SystemExit(f"Missing env var; expected one of: {', '.join(names)}")
    return value


def _load_env(env_file: str | None, *, wallet_profile: str | None) -> BuilderRelayerEnv:
    env_path = resolve_env_path(env_file, wallet_profile=wallet_profile)
    env = project_dotenv_values(env_file, wallet_profile=wallet_profile)
    private_key = _require_any(env, ("PRIVATE_KEY",))
    owner_address = Account.from_key(private_key).address
    configured_owner = _env_any(env, ("WALLET_ADDRESS",))
    if configured_owner and configured_owner.lower() != owner_address.lower():
        raise SystemExit(
            f"WALLET_ADDRESS {configured_owner} does not match PRIVATE_KEY owner {owner_address}",
        )

    return BuilderRelayerEnv(
        env_path=env_path,
        private_key=private_key,
        owner_address=owner_address,
        api_key=_require_any(env, ("POLYMARKET_API_KEY",)),
        api_secret=_require_any(env, ("POLYMARKET_API_SECRET",)),
        api_passphrase=_require_any(env, ("POLYMARKET_API_PASSPHRASE", "POLYMARKET_PASSPHRASE")),
        builder_api_key=_env_any(env, ("BUILDER_API_KEY",)),
        builder_secret=_env_any(env, ("BUILDER_SECRET",)),
        builder_passphrase=_env_any(env, ("BUILDER_PASS_PHRASE", "BUILDER_PASSPHRASE")),
    )


def _client(config: BuilderRelayerEnv) -> ClobClient:
    return ClobClient(
        HOST,
        chain_id=POLYGON,
        key=config.private_key,
        creds=ApiCreds(
            api_key=config.api_key,
            api_secret=config.api_secret,
            api_passphrase=config.api_passphrase,
        ),
        signature_type=0,
        funder=config.owner_address,
    )


def _print_header(config: BuilderRelayerEnv) -> None:
    print(f"env_file={config.env_path}")
    print(f"owner_eoa={config.owner_address}")
    print(f"existing_builder_key={'yes' if config.builder_api_key else 'no'}")


def _coerce_key_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise SystemExit(f"Unexpected builder key response type: {type(payload).__name__}")
    key = payload.get("key") or payload.get("apiKey") or payload.get("api_key")
    secret = payload.get("secret") or payload.get("apiSecret") or payload.get("api_secret")
    passphrase = (
        payload.get("passphrase")
        or payload.get("apiPassphrase")
        or payload.get("api_passphrase")
    )
    if not key or not secret or not passphrase:
        raise SystemExit(f"Unexpected builder key response keys: {sorted(payload)}")
    return {
        "BUILDER_API_KEY": str(key),
        "BUILDER_SECRET": str(secret),
        "BUILDER_PASS_PHRASE": str(passphrase),
    }


def cmd_status(args: argparse.Namespace) -> None:
    config = _load_env(args.env_file, wallet_profile=args.wallet_profile)
    client = _client(config)
    _print_header(config)
    keys = client.get_builder_api_keys()
    count = len(keys) if isinstance(keys, list) else "unknown"
    print(f"remote_builder_key_count={count}")
    if isinstance(keys, list):
        for index, item in enumerate(keys, start=1):
            if isinstance(item, dict):
                key = item.get("key") or item.get("apiKey") or item.get("api_key") or ""
                print(f"remote_builder_key_{index}_prefix={str(key)[:8]}")


def cmd_create(args: argparse.Namespace) -> None:
    config = _load_env(args.env_file, wallet_profile=args.wallet_profile)
    _print_header(config)
    if config.builder_api_key and not args.force:
        print("mode=SKIP")
        print("reason=builder credentials already exist in env; use --force to create a new key")
        return
    if not args.execute:
        print("mode=DRY-RUN")
        print("would_call=create_builder_api_key")
        print("would_write=BUILDER_API_KEY,BUILDER_SECRET,BUILDER_PASS_PHRASE,RELAYER_URL")
        return

    client = _client(config)
    print("mode=EXECUTE")
    payload = _coerce_key_payload(client.create_builder_api_key())
    for key, value in payload.items():
        set_key(str(config.env_path), key, value)
    set_key(str(config.env_path), "RELAYER_URL", DEFAULT_RELAYER_URL)
    print("updated_env=BUILDER_API_KEY,BUILDER_SECRET,BUILDER_PASS_PHRASE,RELAYER_URL")
    print(f"builder_key_prefix={payload['BUILDER_API_KEY'][:8]}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="List builder key status.")
    add_env_file_arg(status)
    status.set_defaults(func=cmd_status)

    create = subparsers.add_parser("create", help="Create and write builder relayer credentials.")
    add_env_file_arg(create)
    create.add_argument("--execute", action="store_true", help="Create and write credentials.")
    create.add_argument("--force", action="store_true", help="Create a new key even if env already has one.")
    create.set_defaults(func=cmd_create)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
