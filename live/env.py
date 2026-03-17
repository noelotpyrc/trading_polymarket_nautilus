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


def bootstrap_env_file(argv: list[str] | None = None) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=None)
    parsed, remaining = parser.parse_known_args(args)
    if parsed.env_file is not None:
        load_dotenv(set_env_file(parsed.env_file), override=True)
    return remaining
