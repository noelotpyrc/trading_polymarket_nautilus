from __future__ import annotations

from eth_account import Account

from live.setup import init_builder_relayer


class FakeClobClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_builder_api_keys(self):
        return [{"key": "builder-existing-key"}]

    def create_builder_api_key(self):
        return {
            "key": "builder-new-key",
            "secret": "builder-new-secret",
            "passphrase": "builder-new-pass",
        }


def _write_env(path, account, *, builder: bool = False) -> None:
    lines = [
        f"PRIVATE_KEY={account.key.hex()}",
        f"WALLET_ADDRESS={account.address}",
        "POLYMARKET_API_KEY=clob-key",
        "POLYMARKET_API_SECRET=clob-secret",
        "POLYMARKET_API_PASSPHRASE=clob-pass",
    ]
    if builder:
        lines.extend(
            [
                "BUILDER_API_KEY=existing-builder-key",
                "BUILDER_SECRET=existing-builder-secret",
                "BUILDER_PASS_PHRASE=existing-builder-pass",
            ],
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_env_rejects_mismatched_wallet_address(tmp_path):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            account.address,
            "0x2222222222222222222222222222222222222222",
        ),
        encoding="utf-8",
    )

    try:
        init_builder_relayer._load_env(str(env_path), wallet_profile=None)
    except SystemExit as exc:
        assert "does not match PRIVATE_KEY owner" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_status_lists_builder_keys(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    monkeypatch.setattr(init_builder_relayer, "ClobClient", FakeClobClient)

    parser = init_builder_relayer.build_parser()
    args = parser.parse_args(["status", "--env-file", str(env_path)])
    args.func(args)

    out = capsys.readouterr().out
    assert "remote_builder_key_count=1" in out
    assert "remote_builder_key_1_prefix=builder-" in out


def test_create_without_execute_is_dry_run(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    monkeypatch.setattr(init_builder_relayer, "ClobClient", FakeClobClient)

    parser = init_builder_relayer.build_parser()
    args = parser.parse_args(["create", "--env-file", str(env_path)])
    args.func(args)

    out = capsys.readouterr().out
    assert "mode=DRY-RUN" in out
    assert "would_call=create_builder_api_key" in out


def test_create_skips_when_builder_key_exists(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account, builder=True)
    monkeypatch.setattr(init_builder_relayer, "ClobClient", FakeClobClient)

    parser = init_builder_relayer.build_parser()
    args = parser.parse_args(["create", "--env-file", str(env_path), "--execute"])
    args.func(args)

    out = capsys.readouterr().out
    assert "mode=SKIP" in out


def test_create_execute_writes_builder_creds(tmp_path, monkeypatch, capsys):
    account = Account.create()
    env_path = tmp_path / "wallet.env"
    _write_env(env_path, account)
    monkeypatch.setattr(init_builder_relayer, "ClobClient", FakeClobClient)

    parser = init_builder_relayer.build_parser()
    args = parser.parse_args(["create", "--env-file", str(env_path), "--execute"])
    args.func(args)

    out = capsys.readouterr().out
    assert "updated_env=BUILDER_API_KEY,BUILDER_SECRET,BUILDER_PASS_PHRASE,RELAYER_URL" in out
    text = env_path.read_text(encoding="utf-8")
    assert "BUILDER_API_KEY='builder-new-key'" in text
    assert "BUILDER_SECRET='builder-new-secret'" in text
    assert "BUILDER_PASS_PHRASE='builder-new-pass'" in text
    assert f"RELAYER_URL='{init_builder_relayer.DEFAULT_RELAYER_URL}'" in text
