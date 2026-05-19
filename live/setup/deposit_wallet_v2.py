#!/usr/bin/env python3
"""Manage Polymarket V2 deposit-wallet setup for new API wallets.

Default commands are read-only. `deploy` requires `--execute`.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import set_key
from eth_account import Account
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import DepositWalletCall
from py_builder_relayer_client.models import TransactionType
from py_builder_signing_sdk.config import BuilderApiKeyCreds
from py_builder_signing_sdk.config import BuilderConfig
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds
from py_clob_client_v2.clob_types import AssetType
from py_clob_client_v2.clob_types import BalanceAllowanceParams
from py_clob_client_v2.constants import POLYGON
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from live.env import add_env_file_arg
from live.env import project_dotenv_values
from live.env import resolve_env_path
from live.env import resolve_polygon_rpc_url

DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com/"
HOST = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137
DEFAULT_RPC_URL = "https://polygon-bor-rpc.publicnode.com"
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

V2_OPERATORS = [
    ("CTF Exchange V2", "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("Neg Risk CTF Exchange V2", "0xe2222d279d744050d28e00520010520000310F59"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]

MAX_UINT256 = 2**256 - 1

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "transfer",
        "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
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
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


@dataclass(frozen=True)
class DepositWalletEnv:
    env_path: Path
    private_key: str
    owner_address: str
    relayer_url: str
    chain_id: int
    builder_api_key: str
    builder_secret: str
    builder_passphrase: str
    deposit_wallet_address: str | None


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


def _load_deposit_wallet_env(
    env_file: str | None,
    *,
    wallet_profile: str | None,
) -> DepositWalletEnv:
    env_path = resolve_env_path(env_file, wallet_profile=wallet_profile)
    env = project_dotenv_values(env_file, wallet_profile=wallet_profile)
    private_key = _require_any(env, ("PRIVATE_KEY",))
    expected_owner = Account.from_key(private_key).address
    owner_address = _env_any(env, ("WALLET_ADDRESS",))
    if owner_address and owner_address.lower() != expected_owner.lower():
        raise SystemExit(
            f"WALLET_ADDRESS {owner_address} does not match PRIVATE_KEY owner {expected_owner}",
        )

    relayer_url = _env_any(env, ("RELAYER_URL",)) or DEFAULT_RELAYER_URL
    chain_id = int(_env_any(env, ("CHAIN_ID",)) or DEFAULT_CHAIN_ID)
    return DepositWalletEnv(
        env_path=env_path,
        private_key=private_key,
        owner_address=expected_owner,
        relayer_url=relayer_url,
        chain_id=chain_id,
        builder_api_key=_require_any(env, ("BUILDER_API_KEY", "RELAYER_API_KEY")),
        builder_secret=_require_any(env, ("BUILDER_SECRET", "RELAYER_API_SECRET")),
        builder_passphrase=_require_any(
            env,
            ("BUILDER_PASS_PHRASE", "BUILDER_PASSPHRASE", "RELAYER_API_PASSPHRASE"),
        ),
        deposit_wallet_address=_env_any(env, ("DEPOSIT_WALLET_ADDRESS",)),
    )


def _make_relayer(config: DepositWalletEnv) -> RelayClient:
    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=config.builder_api_key,
            secret=config.builder_secret,
            passphrase=config.builder_passphrase,
        ),
    )
    return RelayClient(
        config.relayer_url,
        config.chain_id,
        config.private_key,
        builder_config,
    )


def _print_header(config: DepositWalletEnv) -> None:
    print(f"env_file={config.env_path}")
    print(f"owner_eoa={config.owner_address}")
    print(f"relayer_url={config.relayer_url}")
    print(f"chain_id={config.chain_id}")
    print(f"configured_deposit_wallet={config.deposit_wallet_address or '(missing)'}")


def cmd_derive(args: argparse.Namespace) -> None:
    config = _load_deposit_wallet_env(args.env_file, wallet_profile=args.wallet_profile)
    relayer = _make_relayer(config)
    derived = relayer.get_expected_deposit_wallet()
    _print_header(config)
    print(f"derived_deposit_wallet={derived}")
    if args.write_env:
        set_key(str(config.env_path), "DEPOSIT_WALLET_ADDRESS", derived)
        set_key(str(config.env_path), "POLYMARKET_FUNDER", derived)
        set_key(str(config.env_path), "POLYMARKET_SIGNATURE_TYPE", "3")
        print("updated_env=DEPOSIT_WALLET_ADDRESS,POLYMARKET_FUNDER,POLYMARKET_SIGNATURE_TYPE")


def cmd_status(args: argparse.Namespace) -> None:
    config = _load_deposit_wallet_env(args.env_file, wallet_profile=args.wallet_profile)
    relayer = _make_relayer(config)
    derived = relayer.get_expected_deposit_wallet()
    deployed = relayer.get_deployed(derived, "WALLET")
    _print_header(config)
    print(f"derived_deposit_wallet={derived}")
    print(f"deposit_wallet_deployed={deployed}")


def cmd_deploy(args: argparse.Namespace) -> None:
    config = _load_deposit_wallet_env(args.env_file, wallet_profile=args.wallet_profile)
    relayer = _make_relayer(config)
    derived = relayer.get_expected_deposit_wallet()
    _print_header(config)
    print(f"derived_deposit_wallet={derived}")
    if not args.execute:
        print("mode=DRY-RUN")
        print("would_submit=WALLET-CREATE")
        return
    print("mode=EXECUTE")
    response = relayer.deploy_deposit_wallet()
    print(f"transaction_id={response.transaction_id}")
    if response.transaction_hash:
        print(f"transaction_hash={response.transaction_hash}")
    confirmed = response.wait()
    print(f"confirmed={confirmed}")


def _w3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(resolve_polygon_rpc_url(DEFAULT_RPC_URL), request_kwargs={"timeout": 20}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise SystemExit("Cannot connect to Polygon RPC")
    return w3


def _deposit_wallet(config: DepositWalletEnv, relayer: RelayClient) -> str:
    derived = relayer.get_expected_deposit_wallet()
    if config.deposit_wallet_address and config.deposit_wallet_address.lower() != derived.lower():
        raise SystemExit(
            f"DEPOSIT_WALLET_ADDRESS {config.deposit_wallet_address} does not match derived {derived}",
        )
    return derived


def cmd_transfer_pusd(args: argparse.Namespace) -> None:
    config = _load_deposit_wallet_env(args.env_file, wallet_profile=args.wallet_profile)
    relayer = _make_relayer(config)
    deposit_wallet = _deposit_wallet(config, relayer)
    w3 = _w3()
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS), abi=ERC20_ABI)
    owner = Web3.to_checksum_address(config.owner_address)
    target = Web3.to_checksum_address(deposit_wallet)
    owner_balance = pusd.functions.balanceOf(owner).call()
    deposit_balance = pusd.functions.balanceOf(target).call()
    amount = owner_balance if args.amount == "all" else int(float(args.amount) * 1_000_000)

    _print_header(config)
    print(f"derived_deposit_wallet={deposit_wallet}")
    print(f"owner_pusd={owner_balance / 1e6:.6f}")
    print(f"deposit_wallet_pusd={deposit_balance / 1e6:.6f}")
    print(f"transfer_amount_pusd={amount / 1e6:.6f}")
    if amount <= 0:
        raise SystemExit("Transfer amount must be positive")
    if owner_balance < amount:
        raise SystemExit("Owner EOA pUSD balance is below requested transfer amount")
    if not args.execute:
        print("mode=DRY-RUN")
        print("would_submit=pUSD transfer owner_eoa -> deposit_wallet")
        return

    nonce = w3.eth.get_transaction_count(owner, "pending")
    tx = pusd.functions.transfer(target, amount).build_transaction(
        {
            "from": owner,
            "nonce": nonce,
            "gas": 100_000,
        },
    )
    signed = w3.eth.account.sign_transaction(tx, config.private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        raise SystemExit(f"pUSD transfer failed: {tx_hash.hex()}")
    print("mode=EXECUTE")
    print(f"tx_hash={tx_hash.hex()}")


def cmd_withdraw_pusd(args: argparse.Namespace) -> None:
    config = _load_deposit_wallet_env(args.env_file, wallet_profile=args.wallet_profile)
    relayer = _make_relayer(config)
    deposit_wallet = _deposit_wallet(config, relayer)
    w3 = _w3()
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS), abi=ERC20_ABI)
    owner = Web3.to_checksum_address(config.owner_address)
    wallet = Web3.to_checksum_address(deposit_wallet)
    owner_balance = pusd.functions.balanceOf(owner).call()
    deposit_balance = pusd.functions.balanceOf(wallet).call()
    amount = deposit_balance if args.amount == "all" else int(float(args.amount) * 1_000_000)

    _print_header(config)
    print(f"derived_deposit_wallet={deposit_wallet}")
    print(f"owner_pusd={owner_balance / 1e6:.6f}")
    print(f"deposit_wallet_pusd={deposit_balance / 1e6:.6f}")
    print(f"withdraw_amount_pusd={amount / 1e6:.6f}")
    if amount <= 0:
        raise SystemExit("Withdraw amount must be positive")
    if deposit_balance < amount:
        raise SystemExit("Deposit wallet pUSD balance is below requested withdraw amount")
    if not args.execute:
        print("mode=DRY-RUN")
        print("would_submit=WALLET batch pUSD transfer deposit_wallet -> owner_eoa")
        return

    nonce_payload = relayer.get_nonce(config.owner_address, TransactionType.WALLET.value)
    if not nonce_payload or nonce_payload.get("nonce") is None:
        raise SystemExit(f"Invalid wallet nonce payload: {nonce_payload}")
    calldata = pusd.functions.transfer(owner, amount)._encode_transaction_data()
    call = DepositWalletCall(
        target=PUSD_ADDRESS,
        value="0",
        data=calldata,
    )
    deadline = str(int(time.time()) + args.deadline_secs)
    response = relayer.execute_deposit_wallet_batch(
        calls=[call],
        wallet_address=deposit_wallet,
        nonce=str(nonce_payload["nonce"]),
        deadline=deadline,
    )
    print("mode=EXECUTE")
    print(f"transaction_id={response.transaction_id}")
    if response.transaction_hash:
        print(f"transaction_hash={response.transaction_hash}")
    confirmed = response.wait()
    print(f"confirmed={confirmed}")


def _approval_amount(value: str) -> int:
    if value == "max":
        return MAX_UINT256
    amount = int(float(value) * 1_000_000)
    if amount <= 0:
        raise SystemExit("Approval amount must be positive")
    return amount


def _format_pusd_amount(amount: int) -> str:
    if amount == MAX_UINT256:
        return "max"
    return f"{amount / 1e6:.6f}"


def cmd_set_allowances(args: argparse.Namespace) -> None:
    config = _load_deposit_wallet_env(args.env_file, wallet_profile=args.wallet_profile)
    relayer = _make_relayer(config)
    deposit_wallet = _deposit_wallet(config, relayer)
    approval_amount = _approval_amount(args.approval)
    w3 = _w3()
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS), abi=ERC20_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_ABI)
    wallet = Web3.to_checksum_address(deposit_wallet)
    calls: list[DepositWalletCall] = []

    _print_header(config)
    print(f"derived_deposit_wallet={deposit_wallet}")
    print(f"approval_amount_pusd={_format_pusd_amount(approval_amount)}")

    for name, address in V2_OPERATORS:
        spender = Web3.to_checksum_address(address)
        current = pusd.functions.allowance(wallet, spender).call()
        status = "OK" if current >= approval_amount else "MISSING"
        print(f"{name} pUSD allowance={_format_pusd_amount(current)} status={status}")
        if current < approval_amount:
            calls.append(
                DepositWalletCall(
                    target=PUSD_ADDRESS,
                    value="0",
                    data=pusd.functions.approve(spender, approval_amount)._encode_transaction_data(),
                ),
            )

    for name, address in V2_OPERATORS:
        operator = Web3.to_checksum_address(address)
        current = ctf.functions.isApprovedForAll(wallet, operator).call()
        status = "OK" if current else "MISSING"
        print(f"{name} CTF approved={current} status={status}")
        if not current:
            calls.append(
                DepositWalletCall(
                    target=CTF_ADDRESS,
                    value="0",
                    data=ctf.functions.setApprovalForAll(operator, True)._encode_transaction_data(),
                ),
            )

    if not calls:
        print("mode=NOOP")
        print("would_submit=0")
        return
    if not args.execute:
        print("mode=DRY-RUN")
        print(f"would_submit=WALLET batch calls={len(calls)}")
        return

    nonce_payload = relayer.get_nonce(config.owner_address, TransactionType.WALLET.value)
    if not nonce_payload or nonce_payload.get("nonce") is None:
        raise SystemExit(f"Invalid wallet nonce payload: {nonce_payload}")
    deadline = str(int(time.time()) + args.deadline_secs)
    response = relayer.execute_deposit_wallet_batch(
        calls=calls,
        wallet_address=deposit_wallet,
        nonce=str(nonce_payload["nonce"]),
        deadline=deadline,
    )
    print("mode=EXECUTE")
    print(f"submitted_calls={len(calls)}")
    print(f"transaction_id={response.transaction_id}")
    if response.transaction_hash:
        print(f"transaction_hash={response.transaction_hash}")
    confirmed = response.wait()
    print(f"confirmed={confirmed}")


def cmd_sync_clob(args: argparse.Namespace) -> None:
    config = _load_deposit_wallet_env(args.env_file, wallet_profile=args.wallet_profile)
    relayer = _make_relayer(config)
    deposit_wallet = _deposit_wallet(config, relayer)
    env = project_dotenv_values(args.env_file, wallet_profile=args.wallet_profile)
    funder = _env_any(env, ("POLYMARKET_FUNDER", "DEPOSIT_WALLET_ADDRESS"))
    if not funder:
        raise SystemExit("Missing POLYMARKET_FUNDER or DEPOSIT_WALLET_ADDRESS")
    if funder.lower() != deposit_wallet.lower():
        raise SystemExit(f"POLYMARKET_FUNDER {funder} does not match derived deposit wallet {deposit_wallet}")
    signature_type = int(_env_any(env, ("POLYMARKET_SIGNATURE_TYPE",)) or "3")
    if signature_type != 3:
        raise SystemExit(f"POLYMARKET_SIGNATURE_TYPE must be 3 for deposit wallet, got {signature_type}")

    client = ClobClient(
        HOST,
        chain_id=POLYGON,
        key=config.private_key,
        creds=ApiCreds(
            api_key=_require_any(env, ("POLYMARKET_API_KEY",)),
            api_secret=_require_any(env, ("POLYMARKET_API_SECRET",)),
            api_passphrase=_require_any(env, ("POLYMARKET_PASSPHRASE", "POLYMARKET_API_PASSPHRASE")),
        ),
        signature_type=signature_type,
        funder=deposit_wallet,
    )

    _print_header(config)
    print(f"derived_deposit_wallet={deposit_wallet}")
    print(f"clob_funder={funder}")
    print(f"signature_type={signature_type}")
    print("mode=SYNC")
    client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    balance = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=signature_type,
        ),
    )
    print(f"clob_pusd_balance={int(balance['balance']) / 1e6:.6f}")
    for address, allowance in (balance.get("allowances") or {}).items():
        print(f"clob_allowance {address}={int(allowance) / 1e6:.6f}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    derive = subparsers.add_parser("derive", help="Derive expected deposit wallet address.")
    add_env_file_arg(derive)
    derive.add_argument("--write-env", action="store_true", help="Write derived wallet vars to env file.")
    derive.set_defaults(func=cmd_derive)

    status = subparsers.add_parser("status", help="Check derived wallet deployment status.")
    add_env_file_arg(status)
    status.set_defaults(func=cmd_status)

    deploy = subparsers.add_parser("deploy", help="Deploy deposit wallet through PM relayer.")
    add_env_file_arg(deploy)
    deploy.add_argument("--execute", action="store_true", help="Submit the relayer WALLET-CREATE request.")
    deploy.set_defaults(func=cmd_deploy)

    transfer = subparsers.add_parser("transfer-pusd", help="Transfer pUSD from owner EOA to deposit wallet.")
    add_env_file_arg(transfer)
    transfer.add_argument(
        "--amount",
        default="all",
        help="pUSD amount to transfer, or 'all'. Default: all.",
    )
    transfer.add_argument("--execute", action="store_true", help="Submit the pUSD transfer transaction.")
    transfer.set_defaults(func=cmd_transfer_pusd)

    withdraw = subparsers.add_parser(
        "withdraw-pusd",
        help="Transfer pUSD from deposit wallet back to owner EOA through relayer.",
    )
    add_env_file_arg(withdraw)
    withdraw.add_argument(
        "--amount",
        default="all",
        help="pUSD amount to withdraw, or 'all'. Default: all.",
    )
    withdraw.add_argument(
        "--deadline-secs",
        type=int,
        default=600,
        help="Relayer batch signature deadline from now, in seconds. Default: 600.",
    )
    withdraw.add_argument("--execute", action="store_true", help="Submit the relayer WALLET batch.")
    withdraw.set_defaults(func=cmd_withdraw_pusd)

    allowances = subparsers.add_parser(
        "set-allowances",
        help="Set pUSD and CTF approvals from the deposit wallet through relayer.",
    )
    add_env_file_arg(allowances)
    allowances.add_argument(
        "--approval",
        default="max",
        help="pUSD approval amount, or 'max'. Default: max.",
    )
    allowances.add_argument(
        "--deadline-secs",
        type=int,
        default=600,
        help="Relayer batch signature deadline from now, in seconds. Default: 600.",
    )
    allowances.add_argument("--execute", action="store_true", help="Submit the relayer WALLET batch.")
    allowances.set_defaults(func=cmd_set_allowances)

    sync = subparsers.add_parser(
        "sync-clob",
        help="Refresh PM CLOB cached collateral balance/allowance for the deposit wallet.",
    )
    add_env_file_arg(sync)
    sync.set_defaults(func=cmd_sync_clob)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
