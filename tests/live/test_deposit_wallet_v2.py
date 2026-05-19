from __future__ import annotations

from eth_account import Account

from live.setup import deposit_wallet_v2


class FakeRelayer:
    def __init__(self, *_args, **_kwargs) -> None:
        self.deploy_called = False

    def get_expected_deposit_wallet(self) -> str:
        return "0x1111111111111111111111111111111111111111"

    def get_deployed(self, address: str, signer_type: str | None = None) -> bool:
        assert address == "0x1111111111111111111111111111111111111111"
        assert signer_type == "WALLET"
        return False

    def deploy_deposit_wallet(self):
        self.deploy_called = True
        raise AssertionError("deploy should not be called in dry-run")


class FakeFunctionCall:
    def __init__(self, value):
        self.value = value

    def call(self):
        return self.value

    def _encode_transaction_data(self):
        return "0xencoded"


class FakeFunctions:
    def __init__(self, balances, allowances=None, approvals=None):
        self.balances = balances
        self.allowances = allowances or {}
        self.approvals = approvals or {}

    def balanceOf(self, address):
        return FakeFunctionCall(self.balances[address.lower()])

    def allowance(self, owner, spender):
        return FakeFunctionCall(self.allowances.get((owner.lower(), spender.lower()), 0))

    def approve(self, _spender, _amount):
        return FakeFunctionCall(True)

    def isApprovedForAll(self, owner, operator):
        return FakeFunctionCall(self.approvals.get((owner.lower(), operator.lower()), False))

    def setApprovalForAll(self, _operator, _approved):
        return FakeFunctionCall(True)


class FakeContract:
    def __init__(self, balances, allowances=None, approvals=None):
        self.functions = FakeFunctions(balances, allowances=allowances, approvals=approvals)


class FakeEth:
    def __init__(self, balances, allowances=None, approvals=None):
        self.balances = balances
        self.allowances = allowances or {}
        self.approvals = approvals or {}

    def contract(self, **kwargs):
        address = kwargs["address"].lower()
        if address == deposit_wallet_v2.PUSD_ADDRESS.lower():
            return FakeContract(self.balances, allowances=self.allowances)
        if address == deposit_wallet_v2.CTF_ADDRESS.lower():
            return FakeContract(self.balances, approvals=self.approvals)
        raise AssertionError(f"unexpected contract address: {address}")


class FakeWeb3:
    def __init__(self, balances, allowances=None, approvals=None):
        self.eth = FakeEth(balances, allowances=allowances, approvals=approvals)


class FakeClobClient:
    def __init__(self, *_args, **kwargs) -> None:
        assert kwargs["signature_type"] == 3
        assert kwargs["funder"] == "0x1111111111111111111111111111111111111111"
        self.updated = False

    def update_balance_allowance(self, _params) -> None:
        self.updated = True

    def get_balance_allowance(self, _params) -> dict:
        assert self.updated
        return {
            "balance": "6060000",
            "allowances": {
                deposit_wallet_v2.V2_OPERATORS[0][1]: str(deposit_wallet_v2.MAX_UINT256),
            },
        }


def _write_env(path, account) -> None:
    path.write_text(
        "\n".join(
            [
                f"PRIVATE_KEY={account.key.hex()}",
                f"WALLET_ADDRESS={account.address}",
                "BUILDER_API_KEY=builder-key",
                "BUILDER_SECRET=builder-secret",
                "BUILDER_PASS_PHRASE=builder-pass",
            ],
        )
        + "\n",
        encoding="utf-8",
    )


def test_load_deposit_wallet_env_validates_owner_address(tmp_path):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)

    config = deposit_wallet_v2._load_deposit_wallet_env(str(env_path), wallet_profile=None)

    assert config.env_path == env_path.resolve()
    assert config.owner_address == account.address
    assert config.relayer_url == deposit_wallet_v2.DEFAULT_RELAYER_URL
    assert config.chain_id == deposit_wallet_v2.DEFAULT_CHAIN_ID


def test_load_deposit_wallet_env_rejects_mismatched_wallet_address(tmp_path):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    text = env_path.read_text(encoding="utf-8")
    env_path.write_text(
        text.replace(account.address, "0x2222222222222222222222222222222222222222"),
        encoding="utf-8",
    )

    try:
        deposit_wallet_v2._load_deposit_wallet_env(str(env_path), wallet_profile=None)
    except SystemExit as exc:
        assert "does not match PRIVATE_KEY owner" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_derive_write_env_sets_deposit_wallet_vars(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    monkeypatch.setattr(deposit_wallet_v2, "RelayClient", FakeRelayer)

    parser = deposit_wallet_v2.build_parser()
    args = parser.parse_args(["derive", "--env-file", str(env_path), "--write-env"])
    args.func(args)

    out = capsys.readouterr().out
    assert "derived_deposit_wallet=0x1111111111111111111111111111111111111111" in out
    env_text = env_path.read_text(encoding="utf-8")
    assert "DEPOSIT_WALLET_ADDRESS='0x1111111111111111111111111111111111111111'" in env_text
    assert "POLYMARKET_FUNDER='0x1111111111111111111111111111111111111111'" in env_text
    assert "POLYMARKET_SIGNATURE_TYPE='3'" in env_text


def test_status_uses_wallet_signer_type(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    monkeypatch.setattr(deposit_wallet_v2, "RelayClient", FakeRelayer)

    parser = deposit_wallet_v2.build_parser()
    args = parser.parse_args(["status", "--env-file", str(env_path)])
    args.func(args)

    out = capsys.readouterr().out
    assert "deposit_wallet_deployed=False" in out


def test_deploy_without_execute_is_dry_run(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    monkeypatch.setattr(deposit_wallet_v2, "RelayClient", FakeRelayer)

    parser = deposit_wallet_v2.build_parser()
    args = parser.parse_args(["deploy", "--env-file", str(env_path)])
    args.func(args)

    out = capsys.readouterr().out
    assert "mode=DRY-RUN" in out
    assert "would_submit=WALLET-CREATE" in out


def test_transfer_pusd_without_execute_is_dry_run(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    monkeypatch.setattr(deposit_wallet_v2, "RelayClient", FakeRelayer)
    monkeypatch.setattr(
        deposit_wallet_v2,
        "_w3",
        lambda: FakeWeb3(
            {
                account.address.lower(): 6_060_000,
                "0x1111111111111111111111111111111111111111": 0,
            },
        ),
    )

    parser = deposit_wallet_v2.build_parser()
    args = parser.parse_args(["transfer-pusd", "--env-file", str(env_path)])
    args.func(args)

    out = capsys.readouterr().out
    assert "transfer_amount_pusd=6.060000" in out
    assert "mode=DRY-RUN" in out
    assert "would_submit=pUSD transfer owner_eoa -> deposit_wallet" in out


def test_withdraw_pusd_without_execute_is_dry_run(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    monkeypatch.setattr(deposit_wallet_v2, "RelayClient", FakeRelayer)
    monkeypatch.setattr(
        deposit_wallet_v2,
        "_w3",
        lambda: FakeWeb3(
            {
                account.address.lower(): 0,
                "0x1111111111111111111111111111111111111111": 6_060_000,
            },
        ),
    )

    parser = deposit_wallet_v2.build_parser()
    args = parser.parse_args(["withdraw-pusd", "--env-file", str(env_path), "--amount", "0.5"])
    args.func(args)

    out = capsys.readouterr().out
    assert "withdraw_amount_pusd=0.500000" in out
    assert "mode=DRY-RUN" in out
    assert "would_submit=WALLET batch pUSD transfer deposit_wallet -> owner_eoa" in out


def test_set_allowances_without_execute_is_dry_run(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    wallet = "0x1111111111111111111111111111111111111111"
    existing_allowances = {
        (
            wallet,
            deposit_wallet_v2.V2_OPERATORS[0][1].lower(),
        ): deposit_wallet_v2.MAX_UINT256,
    }
    existing_approvals = {
        (
            wallet,
            deposit_wallet_v2.V2_OPERATORS[0][1].lower(),
        ): True,
    }
    monkeypatch.setattr(deposit_wallet_v2, "RelayClient", FakeRelayer)
    monkeypatch.setattr(
        deposit_wallet_v2,
        "_w3",
        lambda: FakeWeb3(
            {wallet: 6_060_000},
            allowances=existing_allowances,
            approvals=existing_approvals,
        ),
    )

    parser = deposit_wallet_v2.build_parser()
    args = parser.parse_args(["set-allowances", "--env-file", str(env_path)])
    args.func(args)

    out = capsys.readouterr().out
    assert "approval_amount_pusd=max" in out
    assert "CTF Exchange V2 pUSD allowance=max status=OK" in out
    assert "Neg Risk CTF Exchange V2 pUSD allowance=0.000000 status=MISSING" in out
    assert "CTF Exchange V2 CTF approved=True status=OK" in out
    assert "Neg Risk CTF Exchange V2 CTF approved=False status=MISSING" in out
    assert "mode=DRY-RUN" in out
    assert "would_submit=WALLET batch calls=4" in out


def test_sync_clob_uses_deposit_wallet_funder(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    with env_path.open("a", encoding="utf-8") as fh:
        fh.write("POLYMARKET_API_KEY=pm-key\n")
        fh.write("POLYMARKET_API_SECRET=pm-secret\n")
        fh.write("POLYMARKET_API_PASSPHRASE=pm-pass\n")
        fh.write("POLYMARKET_FUNDER=0x1111111111111111111111111111111111111111\n")
        fh.write("POLYMARKET_SIGNATURE_TYPE=3\n")
    monkeypatch.setattr(deposit_wallet_v2, "RelayClient", FakeRelayer)
    monkeypatch.setattr(deposit_wallet_v2, "ClobClient", FakeClobClient)

    parser = deposit_wallet_v2.build_parser()
    args = parser.parse_args(["sync-clob", "--env-file", str(env_path)])
    args.func(args)

    out = capsys.readouterr().out
    assert "clob_funder=0x1111111111111111111111111111111111111111" in out
    assert "signature_type=3" in out
    assert "mode=SYNC" in out
    assert "clob_pusd_balance=6.060000" in out
