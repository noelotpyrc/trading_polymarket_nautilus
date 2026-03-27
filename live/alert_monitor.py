#!/usr/bin/env python3
"""Watch live status artifacts and emit structured alert events."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any


_REDEMPTION_PENDING_STATUSES = {"ready_to_redeem", "settled"}


@dataclass
class AlertMonitor:
    alerts_path: Path
    run_dir: Path | None = None
    node_status_path: Path | None = None
    worker_status_path: Path | None = None
    runner_log_path: Path | None = None
    worker_log_path: Path | None = None
    node_stale_secs: int = 90
    worker_stale_secs: int = 120
    pending_order_stale_secs: int = 120
    resolution_pending_secs: int = 600
    redeemable_repeat_scans: int = 3
    tail_lines: int = 20
    _active_condition_keys: set[str] = field(default_factory=set)
    _resolution_pending_first_seen: dict[str, datetime] = field(default_factory=dict)
    _redeemable_scan_counts: dict[str, int] = field(default_factory=dict)

    def poll(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        now = now or datetime.now(tz=timezone.utc)
        node_status = _read_json(self.node_status_path)
        worker_status = _read_json(self.worker_status_path)
        current_conditions: dict[str, dict[str, object]] = {}

        current_conditions.update(
            self._evaluate_node_status(node_status=node_status, worker_status=worker_status, now=now)
        )
        current_conditions.update(
            self._evaluate_worker_status(worker_status=worker_status, node_status=node_status, now=now)
        )

        new_alerts = [
            current_conditions[key]
            for key in current_conditions
            if key not in self._active_condition_keys
        ]
        self._active_condition_keys = set(current_conditions)
        for alert in new_alerts:
            self._write_alert(alert)
            self._print_alert(alert)
        return new_alerts

    def _evaluate_node_status(
        self,
        *,
        node_status: dict[str, object] | None,
        worker_status: dict[str, object] | None,
        now: datetime,
    ) -> dict[str, dict[str, object]]:
        if not node_status:
            return {}

        conditions: dict[str, dict[str, object]] = {}
        lifecycle_state = str(node_status.get("lifecycle_state") or "unknown")
        recorded_at = _parse_timestamp(node_status.get("recorded_at"))
        status_age_secs = _elapsed_secs(now, recorded_at)

        if (
            lifecycle_state != "stopped"
            and status_age_secs is not None
            and status_age_secs > self.node_stale_secs
        ):
            conditions["node_status_stale"] = self._make_alert(
                alert_type="node_status_stale",
                severity="error",
                message=(
                    f"Node status is stale ({status_age_secs}s old, threshold={self.node_stale_secs}s)"
                ),
                source="node",
                now=now,
                node_status=node_status,
                worker_status=worker_status,
                details={
                    "lifecycle_state": lifecycle_state,
                    "status_age_secs": status_age_secs,
                    "threshold_secs": self.node_stale_secs,
                },
            )

        stop_reason = node_status.get("process_stop_reason")
        if lifecycle_state == "stopped" and stop_reason and not _is_benign_stop_reason(str(stop_reason)):
            conditions[f"node_stopped:{stop_reason}"] = self._make_alert(
                alert_type="node_stopped",
                severity="warning",
                message=f"Node stopped with reason: {stop_reason}",
                source="node",
                now=now,
                node_status=node_status,
                worker_status=worker_status,
                details={
                    "stop_reason": stop_reason,
                },
            )

        entry_order = node_status.get("entry_order") or {}
        entry_order_id = entry_order.get("client_order_id")
        entry_order_age_secs = _int_or_none(entry_order.get("age_secs"))
        if entry_order.get("pending") and entry_order_id and entry_order_age_secs is not None:
            if entry_order_age_secs > self.pending_order_stale_secs:
                conditions[f"entry_order_pending_too_long:{entry_order_id}"] = self._make_alert(
                    alert_type="entry_order_pending_too_long",
                    severity="warning",
                    message=(
                        f"Entry order {entry_order_id} pending for {entry_order_age_secs}s "
                        f"(threshold={self.pending_order_stale_secs}s)"
                    ),
                    source="node",
                    now=now,
                    node_status=node_status,
                    worker_status=worker_status,
                    details={
                        "client_order_id": entry_order_id,
                        "age_secs": entry_order_age_secs,
                        "threshold_secs": self.pending_order_stale_secs,
                    },
                )

        passive_exit_order = node_status.get("passive_exit_order") or {}
        passive_exit_order_id = passive_exit_order.get("client_order_id")
        passive_exit_age_secs = _int_or_none(passive_exit_order.get("age_secs"))
        if passive_exit_order_id and passive_exit_age_secs is not None:
            if passive_exit_age_secs > self.pending_order_stale_secs:
                conditions[f"passive_exit_pending_too_long:{passive_exit_order_id}"] = self._make_alert(
                    alert_type="passive_exit_pending_too_long",
                    severity="warning",
                    message=(
                        f"Passive exit order {passive_exit_order_id} open for {passive_exit_age_secs}s "
                        f"(threshold={self.pending_order_stale_secs}s)"
                    ),
                    source="node",
                    now=now,
                    node_status=node_status,
                    worker_status=worker_status,
                    details={
                        "client_order_id": passive_exit_order_id,
                        "age_secs": passive_exit_age_secs,
                        "threshold_secs": self.pending_order_stale_secs,
                    },
                )

        pending_instruments = {
            str(instrument_id)
            for instrument_id in (node_status.get("resolution_pending_instruments") or [])
        }
        for instrument_id in list(self._resolution_pending_first_seen):
            if instrument_id not in pending_instruments:
                self._resolution_pending_first_seen.pop(instrument_id, None)

        for instrument_id in sorted(pending_instruments):
            first_seen = self._resolution_pending_first_seen.setdefault(
                instrument_id,
                recorded_at or now,
            )
            pending_age_secs = _elapsed_secs(now, first_seen)
            if pending_age_secs is None or pending_age_secs <= self.resolution_pending_secs:
                continue
            conditions[f"resolution_pending_too_long:{instrument_id}"] = self._make_alert(
                alert_type="resolution_pending_too_long",
                severity="warning",
                message=(
                    f"Residual position {instrument_id} pending resolution for {pending_age_secs}s "
                    f"(threshold={self.resolution_pending_secs}s)"
                ),
                source="node",
                now=now,
                node_status=node_status,
                worker_status=worker_status,
                details={
                    "instrument_id": instrument_id,
                    "pending_age_secs": pending_age_secs,
                    "threshold_secs": self.resolution_pending_secs,
                },
            )

        return conditions

    def _evaluate_worker_status(
        self,
        *,
        worker_status: dict[str, object] | None,
        node_status: dict[str, object] | None,
        now: datetime,
    ) -> dict[str, dict[str, object]]:
        if not worker_status:
            self._redeemable_scan_counts.clear()
            return {}

        conditions: dict[str, dict[str, object]] = {}
        node_lifecycle_state = None if node_status is None else str(node_status.get("lifecycle_state") or "unknown")
        recorded_at = _parse_timestamp(worker_status.get("recorded_at"))
        status_age_secs = _elapsed_secs(now, recorded_at)
        if (
            node_lifecycle_state != "stopped"
            and status_age_secs is not None
            and status_age_secs > self.worker_stale_secs
        ):
            conditions["worker_status_stale"] = self._make_alert(
                alert_type="worker_status_stale",
                severity="error",
                message=(
                    f"Worker status is stale ({status_age_secs}s old, threshold={self.worker_stale_secs}s)"
                ),
                source="worker",
                now=now,
                node_status=node_status,
                worker_status=worker_status,
                details={
                    "status": worker_status.get("status"),
                    "status_age_secs": status_age_secs,
                    "threshold_secs": self.worker_stale_secs,
                },
            )

        if not worker_status.get("execute_redemptions"):
            self._redeemable_scan_counts.clear()
            return conditions

        current_redeemable_ids: set[str] = set()
        for result in worker_status.get("results") or []:
            status = str(result.get("status") or "")
            if status not in _REDEMPTION_PENDING_STATUSES:
                continue
            instrument_id = str(result.get("instrument_id") or result.get("condition_id") or "")
            if not instrument_id:
                continue
            current_redeemable_ids.add(instrument_id)
            seen_count = self._redeemable_scan_counts.get(instrument_id, 0) + 1
            self._redeemable_scan_counts[instrument_id] = seen_count
            if seen_count < self.redeemable_repeat_scans:
                continue
            conditions[f"redeemable_position_not_cleared:{instrument_id}"] = self._make_alert(
                alert_type="redeemable_position_not_cleared",
                severity="warning",
                message=(
                    f"Worker has seen {instrument_id} in status={status} for "
                    f"{seen_count} scans (threshold={self.redeemable_repeat_scans})"
                ),
                source="worker",
                now=now,
                node_status=node_status,
                worker_status=worker_status,
                details={
                    "instrument_id": instrument_id,
                    "status": status,
                    "seen_scans": seen_count,
                    "threshold_scans": self.redeemable_repeat_scans,
                },
            )

        for instrument_id in list(self._redeemable_scan_counts):
            if instrument_id not in current_redeemable_ids:
                self._redeemable_scan_counts.pop(instrument_id, None)

        return conditions

    def _make_alert(
        self,
        *,
        alert_type: str,
        severity: str,
        message: str,
        source: str,
        now: datetime,
        node_status: dict[str, object] | None,
        worker_status: dict[str, object] | None,
        details: dict[str, object],
    ) -> dict[str, object]:
        return {
            "recorded_at": now.isoformat(),
            "alert_type": alert_type,
            "severity": severity,
            "message": message,
            "source": source,
            "run_dir": None if self.run_dir is None else str(self.run_dir),
            "node_status_path": None if self.node_status_path is None else str(self.node_status_path),
            "worker_status_path": None if self.worker_status_path is None else str(self.worker_status_path),
            "node_status_recorded_at": None if node_status is None else node_status.get("recorded_at"),
            "worker_status_recorded_at": None if worker_status is None else worker_status.get("recorded_at"),
            "details": details,
            "runner_log_tail": self._tail_lines(self.runner_log_path) if source == "node" else [],
            "worker_log_tail": self._tail_lines(self.worker_log_path) if source == "worker" else [],
        }

    def _write_alert(self, payload: dict[str, object]) -> None:
        self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
        with self.alerts_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()

    def _print_alert(self, payload: dict[str, object]) -> None:
        print(
            f"[ALERT] {payload['severity']} {payload['alert_type']}: {payload['message']}"
        )

    def _tail_lines(self, path: Path | None) -> list[str]:
        if path is None or not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if self.tail_lines <= 0:
            return []
        return lines[-self.tail_lines :]


def main(argv: list[str] | None = None) -> None:
    parser = _make_arg_parser()
    args = parser.parse_args(argv)

    run_dir = None if args.run_dir is None else Path(args.run_dir).expanduser().resolve()
    node_status_path = _resolve_path(
        explicit=args.node_status,
        run_dir=run_dir,
        default_name="status.json",
    )
    worker_status_path = _resolve_path(
        explicit=args.worker_status,
        run_dir=run_dir,
        default_name="worker_status.json",
    )
    alerts_path = _resolve_path(
        explicit=args.alerts_path,
        run_dir=run_dir,
        default_name="alerts.jsonl",
    )
    runner_log_path = _resolve_path(
        explicit=args.runner_log,
        run_dir=run_dir,
        default_name="runner.log",
    )
    worker_log_path = _resolve_path(
        explicit=args.worker_log,
        run_dir=run_dir,
        default_name="worker.log",
    )
    if alerts_path is None:
        raise SystemExit("Provide a run dir or --alerts-path")
    allow_missing_startup_status = args.allow_missing_startup_status and not args.once
    if _status_inputs_missing(node_status_path, worker_status_path) and not allow_missing_startup_status:
        location = str(run_dir) if run_dir is not None else "the provided status paths"
        raise SystemExit(f"No status files found under {location}")

    monitor = AlertMonitor(
        run_dir=run_dir,
        alerts_path=alerts_path,
        node_status_path=node_status_path,
        worker_status_path=worker_status_path,
        runner_log_path=runner_log_path,
        worker_log_path=worker_log_path,
        node_stale_secs=args.node_stale_secs,
        worker_stale_secs=args.worker_stale_secs,
        pending_order_stale_secs=args.pending_order_stale_secs,
        resolution_pending_secs=args.resolution_pending_secs,
        redeemable_repeat_scans=args.redeemable_repeat_scans,
        tail_lines=args.tail_lines,
    )

    if args.once:
        alerts = monitor.poll()
        if not alerts:
            print("No alerts.")
        return

    try:
        while True:
            monitor.poll()
            time.sleep(args.interval_secs)
    except KeyboardInterrupt:
        return


def _make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor live status artifacts and emit alerts")
    parser.add_argument("run_dir", nargs="?", help="Run directory containing status and log files")
    parser.add_argument("--node-status", default=None, help="Explicit node status.json path")
    parser.add_argument("--worker-status", default=None, help="Explicit worker_status.json path")
    parser.add_argument("--runner-log", default=None, help="Explicit runner.log path")
    parser.add_argument("--worker-log", default=None, help="Explicit worker.log path")
    parser.add_argument("--alerts-path", default=None, help="Explicit alerts.jsonl output path")
    parser.add_argument("--interval-secs", type=int, default=15, help="Polling interval")
    parser.add_argument("--node-stale-secs", type=int, default=90, help="Alert when node status is older than this")
    parser.add_argument("--worker-stale-secs", type=int, default=120, help="Alert when worker status is older than this")
    parser.add_argument(
        "--pending-order-stale-secs",
        type=int,
        default=120,
        help="Alert when entry or passive exit orders are older than this",
    )
    parser.add_argument(
        "--resolution-pending-secs",
        type=int,
        default=600,
        help="Alert when residual resolution remains pending longer than this",
    )
    parser.add_argument(
        "--redeemable-repeat-scans",
        type=int,
        default=3,
        help="Alert when redeemable positions remain after this many worker scans",
    )
    parser.add_argument("--tail-lines", type=int, default=20, help="Include this many log tail lines in alerts")
    parser.add_argument("--once", action="store_true", help="Evaluate current status once and exit")
    parser.add_argument(
        "--allow-missing-startup-status",
        action="store_true",
        help="Allow continuous monitoring to start before status files exist",
    )
    return parser


def _resolve_path(*, explicit: str | None, run_dir: Path | None, default_name: str) -> Path | None:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    if run_dir is None:
        return None
    return run_dir / default_name


def _read_json(path: Path | None) -> dict[str, object] | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _elapsed_secs(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    return max(0, int((now - then).total_seconds()))


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_benign_stop_reason(reason: str) -> bool:
    benign_fragments = (
        "rehearsal bound reached",
        "auto-stop timer elapsed",
    )
    lowered = reason.lower()
    return any(fragment in lowered for fragment in benign_fragments)


def _status_inputs_missing(
    node_status_path: Path | None,
    worker_status_path: Path | None,
) -> bool:
    node_exists = node_status_path is not None and node_status_path.exists()
    worker_exists = worker_status_path is not None and worker_status_path.exists()
    return not node_exists and not worker_exists


if __name__ == "__main__":
    main()
