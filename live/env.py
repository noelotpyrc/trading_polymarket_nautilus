"""Shared helpers for selecting and loading project env files."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
ENV_FILE_VAR = "POLY_ENV_FILE"


def add_env_file_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to an alternate env file to load instead of the repo .env",
    )


def resolve_env_path(env_file: str | os.PathLike[str] | None = None) -> Path:
    candidate = env_file or os.getenv(ENV_FILE_VAR)
    if candidate:
        return Path(candidate).expanduser().resolve()
    return DEFAULT_ENV_PATH


def set_env_file(env_file: str | os.PathLike[str] | None) -> Path:
    path = resolve_env_path(env_file)
    os.environ[ENV_FILE_VAR] = str(path)
    return path


def load_project_env(env_file: str | os.PathLike[str] | None = None) -> Path:
    path = set_env_file(env_file) if env_file is not None else resolve_env_path()
    load_dotenv(path, override=False)
    return path


def project_dotenv_values(env_file: str | os.PathLike[str] | None = None) -> dict[str, str | None]:
    return dotenv_values(resolve_env_path(env_file))


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
    parsed, remaining = parser.parse_known_args(args)
    if parsed.env_file is not None:
        load_dotenv(set_env_file(parsed.env_file), override=True)
    return remaining
