"""Tests for shared env-file helpers."""
from pathlib import Path

from live import env


def test_resolve_env_path_defaults_to_repo_env():
    assert env.resolve_env_path() == env.DEFAULT_ENV_PATH


def test_load_project_env_honors_explicit_env_file(tmp_path, monkeypatch):
    env_path = tmp_path / "alt.env"
    env_path.write_text("PRIVATE_KEY=from-alt\n", encoding="utf-8")
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.delenv(env.ENV_FILE_VAR, raising=False)

    loaded = env.load_project_env(env_path)

    assert loaded == env_path.resolve()
    assert env.resolve_env_path() == env_path.resolve()
    assert env.os.environ["PRIVATE_KEY"] == "from-alt"
    assert env.project_dotenv_values()["PRIVATE_KEY"] == "from-alt"


def test_resolve_wallet_profile_maps_alias_to_env_file(tmp_path, monkeypatch):
    profiles_path = tmp_path / "wallet_profiles.toml"
    profiles_path.write_text(
        '[local_test]\nenv_file = "vault/.env.local_test"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv(env.ENV_FILE_VAR, raising=False)
    monkeypatch.delenv(env.WALLET_PROFILE_VAR, raising=False)
    monkeypatch.setenv(env.WALLET_PROFILES_FILE_VAR, str(profiles_path))

    assert env.resolve_env_path(wallet_profile="local_test") == (
        env.PROJECT_ROOT / "vault" / ".env.local_test"
    ).resolve()


def test_bootstrap_wallet_profile_strips_flag_and_sets_env_path(tmp_path, monkeypatch):
    profiles_path = tmp_path / "wallet_profiles.toml"
    profiles_path.write_text(
        '[local_test]\nenv_file = "vault/.env.local_test"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv(env.ENV_FILE_VAR, raising=False)
    monkeypatch.delenv(env.WALLET_PROFILE_VAR, raising=False)
    monkeypatch.setenv(env.WALLET_PROFILES_FILE_VAR, str(profiles_path))

    remaining = env.bootstrap_env_file([
        "--wallet-profile",
        "local_test",
        "--once",
    ])

    assert remaining == ["--once"]
    assert env.os.environ[env.WALLET_PROFILE_VAR] == "local_test"
    assert Path(env.resolve_env_path()) == (
        env.PROJECT_ROOT / "vault" / ".env.local_test"
    ).resolve()


def test_bootstrap_rejects_env_file_and_wallet_profile_together(tmp_path, monkeypatch):
    env_path = tmp_path / "wallet.env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.delenv(env.ENV_FILE_VAR, raising=False)
    monkeypatch.delenv(env.WALLET_PROFILE_VAR, raising=False)

    try:
        env.bootstrap_env_file([
            "--env-file",
            str(env_path),
            "--wallet-profile",
            "local_test",
        ])
    except SystemExit as exc:
        assert str(exc) == "--env-file and --wallet-profile are mutually exclusive"
    else:
        raise AssertionError("expected SystemExit")


def test_bootstrap_env_file_strips_flag_and_sets_env_path(tmp_path, monkeypatch):
    env_path = tmp_path / "wallet.env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.delenv(env.ENV_FILE_VAR, raising=False)

    remaining = env.bootstrap_env_file([
        "--env-file",
        str(env_path),
        "--once",
        "--interval-secs",
        "10",
    ])

    assert remaining == ["--once", "--interval-secs", "10"]
    assert Path(env.resolve_env_path()) == env_path.resolve()


def test_resolve_polygon_rpc_url_prefers_env(monkeypatch):
    monkeypatch.setenv(env.POLYGON_RPC_URL_VAR, "https://rpc.example")

    assert env.resolve_polygon_rpc_url("https://default.example") == "https://rpc.example"
