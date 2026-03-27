#!/usr/bin/env python3
"""Run a checked-in live runner profile."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from live.env import add_env_file_arg, bootstrap_env_file, load_project_env
from live.profiles import ProfileError, RunnerProfile, available_profile_names, load_profile
from live.runs.common import run_strategy, validate_strategy_config

_BOOTSTRAP_ARGV = bootstrap_env_file()
load_project_env()
DEFAULT_OUTPUT_ROOT = Path(PROJECT_ROOT) / "logs" / "profile_runs"


@dataclass(frozen=True)
class RunArtifacts:
    run_dir: Path
    log_path: Path
    command_path: Path
    summary_path: Path
    status_path: Path
    status_history_path: Path


class TeeStream:
    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def run_profile(
    profile: RunnerProfile,
    *,
    sandbox_wallet_state_path: str | None = None,
    sandbox_starting_usdc: float | None = None,
    log_path: str | None = None,
) -> None:
    validate_strategy_config(profile.strategy, profile.strategy_config)
    effective_sandbox_starting_usdc = (
        profile.sandbox_starting_usdc
        if sandbox_starting_usdc is None
        else sandbox_starting_usdc
    )
    run_kwargs = {
        "strategy_name": profile.strategy,
        "slug_pattern": profile.slug_pattern,
        "hours_ahead": profile.hours_ahead,
        "outcome_side": profile.outcome_side,
        "sandbox": profile.sandbox,
        "binance_us": profile.binance_us,
        "run_secs": profile.run_secs,
        "strategy_config": profile.strategy_config,
    }
    if effective_sandbox_starting_usdc is not None:
        run_kwargs["sandbox_starting_usdc"] = effective_sandbox_starting_usdc
    if sandbox_wallet_state_path is not None:
        run_kwargs["sandbox_wallet_state_path"] = sandbox_wallet_state_path
    if log_path is not None:
        run_kwargs["log_path"] = log_path
    run_strategy(**run_kwargs)


def main(argv: list[str] | None = None) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_profile_arg_parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in available_profile_names():
            print(name)
        return

    if not args.profile:
        parser.error("profile is required unless --list is used")

    main_for_profile(
        args.profile,
        argv=_fixed_profile_argv(args),
        command_argv=argv,
    )


def main_for_profile(
    profile_name: str,
    argv: list[str] | None = None,
    *,
    command_argv: list[str] | None = None,
) -> None:
    argv = _BOOTSTRAP_ARGV if argv is None else bootstrap_env_file(argv)
    parser = _make_fixed_profile_arg_parser(profile_name)
    args = parser.parse_args(argv)

    try:
        profile = load_profile(profile_name)
        if args.hours_ahead is not None:
            profile = profile.with_hours_ahead(args.hours_ahead)
        if args.run_secs is not None:
            profile = profile.with_run_secs(args.run_secs)
        if args.sandbox_starting_usdc is not None:
            profile = profile.with_sandbox_starting_usdc(args.sandbox_starting_usdc)
        validate_strategy_config(profile.strategy, profile.strategy_config)
        if args.print_profile:
            print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
            return
        run_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            *(command_argv if command_argv is not None else argv),
        ]
        run_profile_with_artifacts(
            profile_name=profile_name,
            profile=profile,
            command=run_command,
            sandbox_wallet_state_path=args.sandbox_wallet_state_path,
            output_root=Path(args.output_root),
            label=args.label,
            events_path=Path(args.events_path).expanduser().resolve() if args.events_path else None,
            status_path=Path(args.status_path).expanduser().resolve() if args.status_path else None,
            status_history_path=(
                Path(args.status_history_path).expanduser().resolve()
                if args.status_history_path
                else None
            ),
        )
    except (ProfileError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _make_profile_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a checked-in live runner profile")
    add_env_file_arg(parser)
    parser.add_argument("profile", nargs="?", help="Profile name or path to a TOML file")
    parser.add_argument("--list", action="store_true", help="List available checked-in profiles and exit")
    parser.add_argument("--hours-ahead", type=int, default=None,
                        help="Override profile window preload horizon in hours")
    parser.add_argument("--run-secs", type=int, default=None,
                        help="Override profile runtime for a bounded manual run")
    parser.add_argument("--sandbox-wallet-state-path", default=None,
                        help="Optional shared sandbox wallet-state JSON file for resolution tests")
    parser.add_argument("--sandbox-starting-usdc", type=float, default=None,
                        help="Override sandbox starting USDC.e balance for simulated execution")
    parser.add_argument("--print-profile", action="store_true",
                        help="Print the resolved profile JSON and exit")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT),
                        help=f"Directory for persisted logs and summaries (default: {DEFAULT_OUTPUT_ROOT})")
    parser.add_argument("--label", default=None,
                        help="Optional label appended to the timestamped output directory")
    parser.add_argument("--events-path", default=None,
                        help="Optional path for structured diagnostic events JSONL")
    parser.add_argument("--status-path", default=None,
                        help="Optional path for the latest machine-readable node status JSON")
    parser.add_argument("--status-history-path", default=None,
                        help="Optional path for append-only machine-readable node status history JSONL")
    return parser


def _make_fixed_profile_arg_parser(profile_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Run live profile {profile_name}")
    add_env_file_arg(parser)
    parser.add_argument("--hours-ahead", type=int, default=None,
                        help="Override profile window preload horizon in hours")
    parser.add_argument("--run-secs", type=int, default=None,
                        help="Override profile runtime for a bounded manual run")
    parser.add_argument("--sandbox-wallet-state-path", default=None,
                        help="Optional shared sandbox wallet-state JSON file for resolution tests")
    parser.add_argument("--sandbox-starting-usdc", type=float, default=None,
                        help="Override sandbox starting USDC.e balance for simulated execution")
    parser.add_argument("--print-profile", action="store_true",
                        help="Print the resolved profile JSON and exit")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT),
                        help=f"Directory for persisted logs and summaries (default: {DEFAULT_OUTPUT_ROOT})")
    parser.add_argument("--label", default=None,
                        help="Optional label appended to the timestamped output directory")
    parser.add_argument("--events-path", default=None,
                        help="Optional path for structured diagnostic events JSONL")
    parser.add_argument("--status-path", default=None,
                        help="Optional path for the latest machine-readable node status JSON")
    parser.add_argument("--status-history-path", default=None,
                        help="Optional path for append-only machine-readable node status history JSONL")
    return parser


def _fixed_profile_argv(args) -> list[str]:
    argv: list[str] = []
    if args.env_file is not None:
        argv.extend(["--env-file", args.env_file])
    if args.hours_ahead is not None:
        argv.extend(["--hours-ahead", str(args.hours_ahead)])
    if args.run_secs is not None:
        argv.extend(["--run-secs", str(args.run_secs)])
    if args.sandbox_wallet_state_path is not None:
        argv.extend(["--sandbox-wallet-state-path", args.sandbox_wallet_state_path])
    if args.sandbox_starting_usdc is not None:
        argv.extend(["--sandbox-starting-usdc", str(args.sandbox_starting_usdc)])
    if args.print_profile:
        argv.append("--print-profile")
    if args.output_root is not None:
        argv.extend(["--output-root", args.output_root])
    if args.label is not None:
        argv.extend(["--label", args.label])
    if args.events_path is not None:
        argv.extend(["--events-path", args.events_path])
    if args.status_path is not None:
        argv.extend(["--status-path", args.status_path])
    if args.status_history_path is not None:
        argv.extend(["--status-history-path", args.status_history_path])
    return argv


def run_profile_with_artifacts(
    *,
    profile_name: str,
    profile: RunnerProfile,
    command: list[str],
    sandbox_wallet_state_path: str | None,
    output_root: Path,
    label: str | None,
    events_path: Path | None,
    status_path: Path | None,
    status_history_path: Path | None,
) -> None:
    artifacts = prepare_artifacts(output_root=output_root, label=label or profile_name)
    effective_events_path = events_path
    if effective_events_path is None and profile.strategy == "vol_signal":
        effective_events_path = artifacts.run_dir / "events.jsonl"
    effective_status_path = status_path
    if effective_status_path is None and profile.strategy == "vol_signal":
        effective_status_path = artifacts.status_path
    effective_status_history_path = status_history_path
    if effective_status_history_path is None and profile.strategy == "vol_signal":
        effective_status_history_path = artifacts.status_history_path
    effective_profile = profile
    if profile.strategy == "vol_signal":
        effective_profile = replace(
            profile,
            strategy_config={
                **profile.strategy_config,
                "events_path": None if effective_events_path is None else str(effective_events_path),
                "status_path": None if effective_status_path is None else str(effective_status_path),
                "status_history_path": (
                    None if effective_status_history_path is None else str(effective_status_history_path)
                ),
            },
        )
    started_at = utc_now()
    artifacts.command_path.write_text(" ".join(command) + "\n", encoding="utf-8")
    summary: dict[str, object] = {
        "run_dir": str(artifacts.run_dir),
        "log_path": str(artifacts.log_path),
        "events_path": None if effective_events_path is None else str(effective_events_path),
        "status_path": None if effective_status_path is None else str(effective_status_path),
        "status_history_path": (
            None if effective_status_history_path is None else str(effective_status_history_path)
        ),
        "command": command,
        "status": "failed",
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "duration_secs": None,
        "profile_name": profile_name,
        "profile": effective_profile.to_dict(),
        "message": None,
    }

    with artifacts.log_path.open("w", encoding="utf-8") as log_handle:
        tee_stdout = TeeStream(sys.stdout, log_handle)
        tee_stderr = TeeStream(sys.stderr, log_handle)
        with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
            print(f"Artifacts : {artifacts.run_dir}")
            print(f"Command   : {' '.join(command)}")
            if effective_events_path is not None:
                print(f"Events    : {effective_events_path}")
            if effective_status_path is not None:
                print(f"Status    : {effective_status_path}")
            if effective_status_history_path is not None:
                print(f"StatusLog : {effective_status_history_path}")
            try:
                run_profile(
                    effective_profile,
                    sandbox_wallet_state_path=sandbox_wallet_state_path,
                    log_path=str(artifacts.log_path),
                )
            except KeyboardInterrupt as exc:
                summary["status"] = "stopped"
                summary["message"] = "interrupted by operator"
                print("Interrupted by operator")
                raise SystemExit(130) from exc
            except BaseException as exc:
                summary["status"] = "failed"
                summary["message"] = str(exc)
                raise
            else:
                summary["status"] = "completed"
                summary["message"] = "runner exited cleanly"
            finally:
                finished_at = utc_now()
                summary["finished_at"] = finished_at.isoformat()
                summary["duration_secs"] = round((finished_at - started_at).total_seconds(), 3)
                artifacts.summary_path.write_text(
                    json.dumps(summary, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(f"Summary   : {artifacts.summary_path}")


def prepare_artifacts(*, output_root: Path, label: str | None) -> RunArtifacts:
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = _make_run_dir(output_root=output_root, label=label)
    run_dir.mkdir(parents=True, exist_ok=False)
    return RunArtifacts(
        run_dir=run_dir,
        log_path=run_dir / "runner.log",
        command_path=run_dir / "command.txt",
        summary_path=run_dir / "summary.json",
        status_path=run_dir / "status.json",
        status_history_path=run_dir / "status_history.jsonl",
    )


def _make_run_dir(*, output_root: Path, label: str | None) -> Path:
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    if not label:
        return output_root / timestamp
    return output_root / f"{timestamp}_{_safe_name(label)}"


def _safe_name(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in {"-", "_"}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or "run"


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


if __name__ == "__main__":
    main()
