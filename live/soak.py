#!/usr/bin/env python3
"""Run bounded soak sessions for one or more live runner profiles."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from live.profiles import ProfileError, RunnerProfile, available_profile_names, load_profile

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "logs" / "soak"


def main(argv: list[str] | None = None) -> None:
    parser = _make_arg_parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in available_profile_names():
            print(name)
        return

    if not args.profiles:
        parser.error("at least one profile is required unless --list is used")
    if args.with_resolution_worker and args.resolution_interval_secs <= 0:
        parser.error("--resolution-interval-secs must be positive")

    try:
        batch = run_soak_batch(
            profile_refs=args.profiles,
            run_secs=args.run_secs,
            hours_ahead=args.hours_ahead,
            output_root=Path(args.output_root),
            label=args.label,
            keep_going=args.keep_going,
            allow_live=args.allow_live,
            allow_unbounded=args.allow_unbounded,
            sandbox_wallet_state_path=args.sandbox_wallet_state_path,
            sandbox_starting_usdc=args.sandbox_starting_usdc,
            with_resolution_worker=args.with_resolution_worker,
            resolution_interval_secs=args.resolution_interval_secs,
        )
    except (ProfileError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    _print_batch_summary(batch)
    if batch["status"] != "passed":
        raise SystemExit(1)


def run_soak_batch(
    *,
    profile_refs: list[str],
    run_secs: int | None,
    hours_ahead: int | None,
    output_root: Path,
    label: str | None,
    keep_going: bool,
    allow_live: bool,
    allow_unbounded: bool,
    sandbox_wallet_state_path: str | None = None,
    sandbox_starting_usdc: float | None = None,
    with_resolution_worker: bool = False,
    resolution_interval_secs: int = 30,
) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=True)
    batch_dir = _make_batch_dir(output_root, label=label)
    batch_dir.mkdir(parents=True, exist_ok=False)

    results: list[dict[str, object]] = []
    batch_started_at = _utc_now()
    status = "passed"

    for index, profile_ref in enumerate(profile_refs, start=1):
        profile = load_profile(profile_ref)
        profile = _prepare_profile(
            profile,
            run_secs=run_secs,
            hours_ahead=hours_ahead,
            allow_live=allow_live,
            allow_unbounded=allow_unbounded,
        )
        result = _run_profile(
            index=index,
            profile_ref=profile_ref,
            profile=profile,
            batch_dir=batch_dir,
            sandbox_wallet_state_path=sandbox_wallet_state_path,
            sandbox_starting_usdc=sandbox_starting_usdc,
            with_resolution_worker=with_resolution_worker,
            resolution_interval_secs=resolution_interval_secs,
        )
        results.append(result)
        if result["status"] != "passed":
            status = "failed"
            if not keep_going:
                break

    batch_finished_at = _utc_now()
    batch_summary = {
        "batch_dir": str(batch_dir),
        "status": status,
        "started_at": batch_started_at.isoformat(),
        "finished_at": batch_finished_at.isoformat(),
        "duration_secs": round((batch_finished_at - batch_started_at).total_seconds(), 3),
        "profile_count": len(results),
        "hours_ahead_override": hours_ahead,
        "run_secs_override": run_secs,
        "sandbox_starting_usdc_override": sandbox_starting_usdc,
        "with_resolution_worker": with_resolution_worker,
        "resolution_interval_secs": resolution_interval_secs if with_resolution_worker else None,
        "results": results,
    }
    _write_json(batch_dir / "summary.json", batch_summary)
    return batch_summary


def _make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded soak sessions for live profiles")
    parser.add_argument("profiles", nargs="*", help="Profile names or TOML paths to run sequentially")
    parser.add_argument("--list", action="store_true", help="List available checked-in profiles and exit")
    parser.add_argument("--hours-ahead", type=int, default=None,
                        help="Override each profile window preload horizon in hours")
    parser.add_argument("--run-secs", type=int, default=None,
                        help="Override each profile runtime for bounded soak sessions")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT),
                        help=f"Directory for soak logs and summaries (default: {DEFAULT_OUTPUT_ROOT})")
    parser.add_argument("--label", default=None,
                        help="Optional batch label appended to the timestamped output directory")
    parser.add_argument("--keep-going", action="store_true",
                        help="Continue running later profiles even if one profile exits non-zero")
    parser.add_argument("--allow-live", action="store_true",
                        help="Allow live profiles. Default is sandbox-only for soak safety.")
    parser.add_argument("--allow-unbounded", action="store_true",
                        help="Allow profiles with no effective runtime bound.")
    parser.add_argument("--sandbox-wallet-state-path", default=None,
                        help="Optional shared sandbox wallet-state JSON file. Defaults to a per-run file.")
    parser.add_argument("--sandbox-starting-usdc", type=float, default=None,
                        help="Override sandbox starting USDC.e balance for simulated execution.")
    parser.add_argument("--with-resolution-worker", action="store_true",
                        help="Also run the external resolution worker against the shared sandbox wallet state.")
    parser.add_argument("--resolution-interval-secs", type=int, default=30,
                        help="Polling interval for the companion resolution worker (default: 30)")
    return parser


def _prepare_profile(
    profile: RunnerProfile,
    *,
    run_secs: int | None,
    hours_ahead: int | None,
    allow_live: bool,
    allow_unbounded: bool,
) -> RunnerProfile:
    if hours_ahead is not None:
        profile = profile.with_hours_ahead(hours_ahead)
    if run_secs is not None:
        profile = profile.with_run_secs(run_secs)

    if not allow_live and not profile.sandbox:
        raise ProfileError(
            f"Soak runner only allows sandbox profiles by default: {profile.name!r}. "
            "Pass --allow-live to override."
        )
    if not allow_unbounded and profile.run_secs is None:
        raise ProfileError(
            f"Soak runner requires a bounded runtime for {profile.name!r}. "
            "Set --run-secs or pass --allow-unbounded."
        )

    return profile


def _run_profile(
    *,
    index: int,
    profile_ref: str,
    profile: RunnerProfile,
    batch_dir: Path,
    sandbox_wallet_state_path: str | None,
    sandbox_starting_usdc: float | None,
    with_resolution_worker: bool,
    resolution_interval_secs: int,
) -> dict[str, object]:
    run_dir = batch_dir / f"{index:02d}_{_safe_name(profile.name)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    if with_resolution_worker and not profile.sandbox:
        raise ProfileError("Resolution worker mode only supports sandbox profiles")

    command = [
        sys.executable,
        str(PROJECT_ROOT / "live" / "runs" / "profile.py"),
        _command_profile_ref(profile_ref, fallback_name=profile.name),
    ]
    command.extend(["--hours-ahead", str(profile.hours_ahead)])
    if profile.run_secs is not None:
        command.extend(["--run-secs", str(profile.run_secs)])
    effective_sandbox_starting_usdc = (
        profile.sandbox_starting_usdc
        if sandbox_starting_usdc is None
        else sandbox_starting_usdc
    )
    if effective_sandbox_starting_usdc is not None:
        command.extend(["--sandbox-starting-usdc", str(effective_sandbox_starting_usdc)])
    wallet_state_path: Path | None = None
    if profile.sandbox:
        wallet_state_path = (
            Path(sandbox_wallet_state_path)
            if sandbox_wallet_state_path is not None
            else run_dir / "wallet_state.json"
        )
        command.extend(["--sandbox-wallet-state-path", str(wallet_state_path)])

    started_at = _utc_now()
    log_path = run_dir / "runner.log"
    worker_log_path: Path | None = None
    worker_command: list[str] | None = None
    worker_process: subprocess.Popen[str] | None = None
    worker_exit_code: int | None = None
    worker_terminated = False

    _write_json(run_dir / "profile.json", profile.to_dict())
    (run_dir / "command.txt").write_text(" ".join(command) + "\n", encoding="utf-8")

    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# profile={profile.name}\n")
        handle.write(f"# started_at={started_at.isoformat()}\n")
        handle.write(f"# command={' '.join(command)}\n\n")
        handle.flush()
        worker_handle = None
        try:
            if with_resolution_worker:
                assert wallet_state_path is not None
                worker_log_path = run_dir / "worker.log"
                worker_command = [
                    sys.executable,
                    str(PROJECT_ROOT / "live" / "run_resolution.py"),
                    _command_profile_ref(profile_ref, fallback_name=profile.name),
                    "--hours-ahead",
                    str(profile.hours_ahead),
                    "--sandbox-wallet-state-path",
                    str(wallet_state_path),
                    "--interval-secs",
                    str(resolution_interval_secs),
                ]
                if effective_sandbox_starting_usdc is not None:
                    worker_command.extend(
                        ["--sandbox-starting-usdc", str(effective_sandbox_starting_usdc)]
                    )
                (run_dir / "worker_command.txt").write_text(
                    " ".join(worker_command) + "\n",
                    encoding="utf-8",
                )
                worker_handle = worker_log_path.open("w", encoding="utf-8")
                worker_handle.write(f"# profile={profile.name}\n")
                worker_handle.write(f"# started_at={started_at.isoformat()}\n")
                worker_handle.write(f"# command={' '.join(worker_command)}\n\n")
                worker_handle.flush()
                worker_process = subprocess.Popen(
                    worker_command,
                    cwd=PROJECT_ROOT,
                    stdout=worker_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        finally:
            if worker_process is not None:
                worker_exit_code, worker_terminated = _stop_worker(worker_process)
            if worker_handle is not None:
                worker_handle.close()

    finished_at = _utc_now()
    status = "passed" if completed.returncode == 0 else "failed"
    if worker_process is not None and not worker_terminated and worker_exit_code not in {0, None}:
        status = "failed"
    result = {
        "profile_name": profile.name,
        "profile_ref": profile_ref,
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_secs": round((finished_at - started_at).total_seconds(), 3),
        "exit_code": completed.returncode,
        "hours_ahead": profile.hours_ahead,
        "run_secs": profile.run_secs,
        "sandbox": profile.sandbox,
        "sandbox_starting_usdc": effective_sandbox_starting_usdc,
        "wallet_state_path": None if wallet_state_path is None else str(wallet_state_path),
        "run_dir": str(run_dir),
        "log_path": str(log_path),
        "command": command,
        "resolution_worker": with_resolution_worker,
        "worker_log_path": None if worker_log_path is None else str(worker_log_path),
        "worker_command": worker_command,
        "worker_exit_code": worker_exit_code,
        "worker_terminated_by_harness": worker_terminated,
    }
    _write_json(run_dir / "summary.json", result)
    return result


def _stop_worker(process: subprocess.Popen[str]) -> tuple[int | None, bool]:
    exit_code = process.poll()
    if exit_code is not None:
        return exit_code, False

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    return process.returncode, True


def _command_profile_ref(profile_ref: str, *, fallback_name: str) -> str:
    candidate = Path(profile_ref)
    if candidate.exists():
        return str(candidate.resolve())
    return fallback_name


def _make_batch_dir(output_root: Path, *, label: str | None) -> Path:
    timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
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


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_batch_summary(batch: dict[str, object]) -> None:
    print(f"Soak batch: {batch['status']}")
    print(f"Artifacts : {batch['batch_dir']}")
    for result in batch["results"]:
        worker_suffix = ""
        if result.get("resolution_worker"):
            worker_suffix = f", worker={result['worker_log_path']}"
        print(
            f"  - {result['profile_name']}: {result['status']} "
            f"(exit={result['exit_code']}, log={result['log_path']}{worker_suffix})"
        )


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


if __name__ == "__main__":
    main()
