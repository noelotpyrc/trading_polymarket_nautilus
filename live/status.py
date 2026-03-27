#!/usr/bin/env python3
"""Read machine-readable status artifacts for a live run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = _make_arg_parser()
    args = parser.parse_args(argv)

    run_dir = None if args.run_dir is None else Path(args.run_dir).expanduser().resolve()
    node_status_path = _resolve_status_path(
        explicit=args.node_status,
        run_dir=run_dir,
        default_name="status.json",
    )
    worker_status_path = _resolve_status_path(
        explicit=args.worker_status,
        run_dir=run_dir,
        default_name="worker_status.json",
    )

    if node_status_path is None and worker_status_path is None:
        raise SystemExit("Provide a run dir or at least one status file path")

    if node_status_path is not None and node_status_path.exists():
        _print_node_status(_read_json(node_status_path), node_status_path)
    elif node_status_path is not None:
        print(f"Node status  : missing ({node_status_path})")

    if worker_status_path is not None and worker_status_path.exists():
        _print_worker_status(_read_json(worker_status_path), worker_status_path)
    elif worker_status_path is not None:
        print(f"Worker status: missing ({worker_status_path})")


def _make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read live run status artifacts")
    parser.add_argument("run_dir", nargs="?", help="Optional run directory containing status.json files")
    parser.add_argument("--node-status", default=None, help="Explicit node status.json path")
    parser.add_argument("--worker-status", default=None, help="Explicit worker_status.json path")
    return parser


def _resolve_status_path(
    *,
    explicit: str | None,
    run_dir: Path | None,
    default_name: str,
) -> Path | None:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    if run_dir is None:
        return None
    return run_dir / default_name


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _print_node_status(payload: dict[str, object], path: Path) -> None:
    signal = payload.get("signal") or {}
    current_position = payload.get("current_position") or {}
    entry_order = payload.get("entry_order") or {}
    passive_exit = payload.get("passive_exit_order") or {}
    operating_balance = payload.get("operating_balance") or {}
    print(f"Node status  : {path}")
    print(
        "  "
        f"state={payload.get('lifecycle_state')} "
        f"reason={payload.get('reason')} "
        f"window_end={payload.get('window_end_utc')} "
        f"instrument={payload.get('instrument_id')}"
    )
    print(
        "  "
        f"signal_ts={signal.get('ts', 'n/a')} "
        f"prob_yes={_fmt_float(signal.get('prob_yes_emp'))} "
        f"quote_guard={payload.get('quote_guard', 'n/a')}"
    )
    print(
        "  "
        f"position_qty={_fmt_float(current_position.get('total_quantity'))} "
        f"entry_pending={entry_order.get('pending')} "
        f"entry_order={entry_order.get('client_order_id')} "
        f"passive_exit={passive_exit.get('client_order_id')}"
    )
    print(
        "  "
        f"free_collateral={_fmt_float(operating_balance.get('free_collateral'))} "
        f"min_required={_fmt_float(operating_balance.get('minimum_required'))} "
        f"stop_reason={payload.get('process_stop_reason')}"
    )


def _print_worker_status(payload: dict[str, object], path: Path) -> None:
    counts = payload.get("status_counts") or {}
    print(f"Worker status: {path}")
    print(
        "  "
        f"status={payload.get('status')} "
        f"mode={payload.get('mode')} "
        f"positions={payload.get('position_count')} "
        f"execute_redemptions={payload.get('execute_redemptions')}"
    )
    print(
        "  "
        f"recorded_at={payload.get('recorded_at')} "
        f"status_counts={json.dumps(counts, sort_keys=True)}"
    )


def _fmt_float(value: object) -> str:
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return "n/a"


if __name__ == "__main__":
    main()
