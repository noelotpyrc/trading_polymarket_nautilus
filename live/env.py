"""Shared helpers for selecting and loading project env files."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
import tomllib

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_WALLET_PROFILES_PATH = PROJECT_ROOT / "live" / "wallet_profiles.toml"
ENV_FILE_VAR = "POLY_ENV_FILE"
WALLET_PROFILE_VAR = "POLY_WALLET_PROFILE"
WALLET_PROFILES_FILE_VAR = "POLY_WALLET_PROFILES_FILE"
POLYGON_RPC_URL_VAR = "POLYGON_RPC_URL"


def add_env_file_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to an alternate env file to load instead of the repo .env",
    )
    parser.add_argument(
        "--wallet-profile",
        default=None,
        help="Wallet profile alias from live/wallet_profiles.toml",
    )


def resolve_env_path(
    env_file: str | os.PathLike[str] | None = None,
    *,
    wallet_profile: str | None = None,
) -> Path:
    if env_file is not None and wallet_profile is not None:
        raise ValueError("--env-file and --wallet-profile are mutually exclusive")

    candidate = env_file or os.getenv(ENV_FILE_VAR)
    if candidate:
        return Path(candidate).expanduser().resolve()
    profile = wallet_profile or os.getenv(WALLET_PROFILE_VAR)
    if profile:
        return resolve_wallet_profile_env_path(profile)
    return DEFAULT_ENV_PATH


def resolve_wallet_profile_env_path(profile: str) -> Path:
    profiles = _load_wallet_profiles()
    try:
        raw_env_file = profiles[profile]["env_file"]
    except KeyError as exc:
        known = ", ".join(sorted(profiles)) or "<none>"
        raise ValueError(f"Unknown wallet profile {profile!r}. Known profiles: {known}") from exc
    if not isinstance(raw_env_file, str) or not raw_env_file:
        raise ValueError(f"Wallet profile {profile!r} must define a non-empty env_file")

    path = Path(raw_env_file).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def set_env_file(
    env_file: str | os.PathLike[str] | None = None,
    *,
    wallet_profile: str | None = None,
) -> Path:
    path = resolve_env_path(env_file, wallet_profile=wallet_profile)
    os.environ[ENV_FILE_VAR] = str(path)
    if wallet_profile is not None:
        os.environ[WALLET_PROFILE_VAR] = wallet_profile
    return path


def load_project_env(
    env_file: str | os.PathLike[str] | None = None,
    *,
    wallet_profile: str | None = None,
) -> Path:
    path = (
        set_env_file(env_file, wallet_profile=wallet_profile)
        if env_file is not None or wallet_profile is not None
        else resolve_env_path()
    )
    load_dotenv(path, override=False)
    return path


def project_dotenv_values(
    env_file: str | os.PathLike[str] | None = None,
    *,
    wallet_profile: str | None = None,
) -> dict[str, str | None]:
    return dotenv_values(resolve_env_path(env_file, wallet_profile=wallet_profile))


def resolve_polygon_rpc_url(default: str) -> str:
    return os.getenv(POLYGON_RPC_URL_VAR) or default


def validate_required_env_vars(
    *,
    sandbox: bool,
    env_file: str | os.PathLike[str] | None = None,
) -> None:
    env = {
        **project_dotenv_values(env_file),
        **os.environ,
    }
    required = _required_env_var_candidates(sandbox=sandbox)

    missing = []
    for candidates in required:
        if any(env.get(name) for name in candidates):
            continue
        if len(candidates) == 1:
            missing.append(candidates[0])
        else:
            missing.append(" or ".join(candidates))

    if missing:
        mode = "sandbox" if sandbox else "live"
        joined = ", ".join(missing)
        raise SystemExit(f"Missing required {mode} env vars: {joined}")


def _required_env_var_candidates(*, sandbox: bool) -> list[tuple[str, ...]]:
    if sandbox:
        return [
            ("POLYMARKET_TEST_PRIVATE_KEY",),
            ("POLYMARKET_TEST_API_KEY",),
            ("POLYMARKET_TEST_API_SECRET",),
            ("POLYMARKET_TEST_API_PASSPHRASE",),
            ("POLYMARKET_TEST_WALLET_ADDRESS",),
        ]
    return [
        ("PRIVATE_KEY",),
        ("POLYMARKET_API_KEY",),
        ("POLYMARKET_API_SECRET",),
        ("POLYMARKET_PASSPHRASE", "POLYMARKET_API_PASSPHRASE"),
        ("POLYMARKET_FUNDER", "WALLET_ADDRESS"),
    ]


def bootstrap_env_file(argv: list[str] | None = None) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--wallet-profile", default=None)
    parsed, remaining = parser.parse_known_args(args)
    if parsed.env_file is not None and parsed.wallet_profile is not None:
        raise SystemExit("--env-file and --wallet-profile are mutually exclusive")
    if parsed.env_file is not None or parsed.wallet_profile is not None:
        load_dotenv(
            set_env_file(parsed.env_file, wallet_profile=parsed.wallet_profile),
            override=True,
        )
    return remaining


def _load_wallet_profiles() -> dict[str, dict[str, object]]:
    path = Path(os.getenv(WALLET_PROFILES_FILE_VAR) or DEFAULT_WALLET_PROFILES_PATH)
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    return {
        str(name): value
        for name, value in payload.items()
        if isinstance(value, dict)
    }
