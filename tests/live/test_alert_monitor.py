"""Tests for the Stage 13 alert monitor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from live import alert_monitor
from live.alert_monitor import AlertMonitor


def _write_json(path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _node_status(
    *,
    recorded_at: datetime,
    lifecycle_state: str = "running",
    process_stop_reason: str | None = None,
    entry_pending: bool = False,
    entry_age_secs: int | None = None,
    entry_order_id: str | None = None,
    passive_exit_order_id: str | None = None,
    passive_exit_age_secs: int | None = None,
    resolution_pending_instruments: list[str] | None = None,
) -> dict[str, object]:
    return {
        "recorded_at": recorded_at.isoformat(),
        "lifecycle_state": lifecycle_state,
        "process_stop_reason": process_stop_reason,
        "entry_order": {
            "pending": entry_pending,
            "client_order_id": entry_order_id,
            "age_secs": entry_age_secs,
        },
        "passive_exit_order": {
            "client_order_id": passive_exit_order_id,
            "age_secs": passive_exit_age_secs,
        },
        "resolution_pending_instruments": resolution_pending_instruments or [],
    }


def _worker_status(
    *,
    recorded_at: datetime,
    execute_redemptions: bool = False,
    status: str = "idle",
    results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "recorded_at": recorded_at.isoformat(),
        "execute_redemptions": execute_redemptions,
        "status": status,
        "results": results or [],
    }


def test_monitor_emits_node_stale_alert_with_runner_log_context(tmp_path):
    now = datetime(2026, 3, 26, 13, 0, 0, tzinfo=timezone.utc)
    node_status_path = tmp_path / "status.json"
    worker_status_path = tmp_path / "worker_status.json"
    runner_log_path = tmp_path / "runner.log"
    worker_log_path = tmp_path / "worker.log"
    alerts_path = tmp_path / "alerts.jsonl"

    _write_json(
        node_status_path,
        _node_status(recorded_at=now - timedelta(seconds=150)),
    )
    _write_json(
        worker_status_path,
        _worker_status(recorded_at=now),
    )
    runner_log_path.write_text("line1\nline2\nnode tail\n", encoding="utf-8")
    worker_log_path.write_text("worker line\n", encoding="utf-8")

    monitor = AlertMonitor(
        alerts_path=alerts_path,
        node_status_path=node_status_path,
        worker_status_path=worker_status_path,
        runner_log_path=runner_log_path,
        worker_log_path=worker_log_path,
        node_stale_secs=90,
        tail_lines=2,
    )

    alerts = monitor.poll(now=now)

    assert [alert["alert_type"] for alert in alerts] == ["node_status_stale"]
    assert alerts[0]["runner_log_tail"] == ["line2", "node tail"]
    stored = [
        json.loads(line)
        for line in alerts_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert stored[0]["alert_type"] == "node_status_stale"

    assert monitor.poll(now=now + timedelta(seconds=10)) == []


def test_monitor_emits_pending_entry_order_alert(tmp_path):
    now = datetime(2026, 3, 26, 13, 0, 0, tzinfo=timezone.utc)
    node_status_path = tmp_path / "status.json"
    alerts_path = tmp_path / "alerts.jsonl"

    _write_json(
        node_status_path,
        _node_status(
            recorded_at=now,
            entry_pending=True,
            entry_age_secs=181,
            entry_order_id="E-1",
        ),
    )

    monitor = AlertMonitor(
        alerts_path=alerts_path,
        node_status_path=node_status_path,
        pending_order_stale_secs=120,
    )

    alerts = monitor.poll(now=now)

    assert [alert["alert_type"] for alert in alerts] == ["entry_order_pending_too_long"]
    assert alerts[0]["details"]["client_order_id"] == "E-1"


def test_monitor_tracks_resolution_pending_duration_across_polls(tmp_path):
    now = datetime(2026, 3, 26, 13, 0, 0, tzinfo=timezone.utc)
    node_status_path = tmp_path / "status.json"
    alerts_path = tmp_path / "alerts.jsonl"
    instrument_id = "a.POLYMARKET"

    monitor = AlertMonitor(
        alerts_path=alerts_path,
        node_status_path=node_status_path,
        resolution_pending_secs=300,
    )

    _write_json(
        node_status_path,
        _node_status(
            recorded_at=now,
            resolution_pending_instruments=[instrument_id],
        ),
    )
    assert monitor.poll(now=now) == []

    later = now + timedelta(seconds=301)
    _write_json(
        node_status_path,
        _node_status(
            recorded_at=later,
            resolution_pending_instruments=[instrument_id],
        ),
    )
    alerts = monitor.poll(now=later)

    assert [alert["alert_type"] for alert in alerts] == ["resolution_pending_too_long"]
    assert alerts[0]["details"]["instrument_id"] == instrument_id


def test_monitor_emits_redeemable_position_not_cleared_when_execute_enabled(tmp_path):
    now = datetime(2026, 3, 26, 13, 0, 0, tzinfo=timezone.utc)
    worker_status_path = tmp_path / "worker_status.json"
    worker_log_path = tmp_path / "worker.log"
    alerts_path = tmp_path / "alerts.jsonl"
    instrument_id = "a.POLYMARKET"
    worker_log_path.write_text("scan1\nscan2\nworker tail\n", encoding="utf-8")

    monitor = AlertMonitor(
        alerts_path=alerts_path,
        worker_status_path=worker_status_path,
        worker_log_path=worker_log_path,
        redeemable_repeat_scans=2,
        tail_lines=1,
    )

    _write_json(
        worker_status_path,
        _worker_status(
            recorded_at=now,
            execute_redemptions=True,
            status="tracking_positions",
            results=[
                {
                    "instrument_id": instrument_id,
                    "status": "ready_to_redeem",
                }
            ],
        ),
    )
    assert monitor.poll(now=now) == []

    later = now + timedelta(seconds=30)
    _write_json(
        worker_status_path,
        _worker_status(
            recorded_at=later,
            execute_redemptions=True,
            status="tracking_positions",
            results=[
                {
                    "instrument_id": instrument_id,
                    "status": "ready_to_redeem",
                }
            ],
        ),
    )
    alerts = monitor.poll(now=later)

    assert [alert["alert_type"] for alert in alerts] == ["redeemable_position_not_cleared"]
    assert alerts[0]["worker_log_tail"] == ["worker tail"]


def test_monitor_suppresses_worker_stale_alert_after_node_stops(tmp_path):
    now = datetime(2026, 3, 26, 13, 0, 0, tzinfo=timezone.utc)
    node_status_path = tmp_path / "status.json"
    worker_status_path = tmp_path / "worker_status.json"
    alerts_path = tmp_path / "alerts.jsonl"

    _write_json(
        node_status_path,
        _node_status(
            recorded_at=now - timedelta(seconds=300),
            lifecycle_state="stopped",
            process_stop_reason="Completed 1 managed lifecycle(s) — rehearsal bound reached",
        ),
    )
    _write_json(
        worker_status_path,
        _worker_status(
            recorded_at=now - timedelta(seconds=300),
        ),
    )

    monitor = AlertMonitor(
        alerts_path=alerts_path,
        node_status_path=node_status_path,
        worker_status_path=worker_status_path,
        worker_stale_secs=120,
    )

    assert monitor.poll(now=now) == []


def test_main_once_prints_no_alerts_for_clean_run(tmp_path, capsys):
    now = datetime(2026, 3, 26, 13, 0, 0, tzinfo=timezone.utc)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(
        run_dir / "status.json",
        _node_status(
            recorded_at=now,
            lifecycle_state="stopped",
            process_stop_reason="Completed 1 managed lifecycle(s) — rehearsal bound reached",
        ),
    )
    _write_json(
        run_dir / "worker_status.json",
        _worker_status(
            recorded_at=now,
        ),
    )

    alert_monitor.main([str(run_dir), "--once"])

    assert capsys.readouterr().out.strip() == "No alerts."


def test_monitor_treats_auto_stop_timer_as_benign(tmp_path):
    now = datetime(2026, 3, 26, 13, 0, 0, tzinfo=timezone.utc)
    node_status_path = tmp_path / "status.json"
    worker_status_path = tmp_path / "worker_status.json"
    alerts_path = tmp_path / "alerts.jsonl"

    _write_json(
        node_status_path,
        _node_status(
            recorded_at=now,
            lifecycle_state="stopped",
            process_stop_reason="Auto-stop timer elapsed after 600s",
        ),
    )
    _write_json(
        worker_status_path,
        _worker_status(
            recorded_at=now,
        ),
    )

    monitor = AlertMonitor(
        alerts_path=alerts_path,
        node_status_path=node_status_path,
        worker_status_path=worker_status_path,
    )

    assert monitor.poll(now=now) == []


def test_main_errors_when_no_status_files_exist(tmp_path):
    missing_run_dir = tmp_path / "missing"

    with pytest.raises(SystemExit, match="No status files found"):
        alert_monitor.main([str(missing_run_dir), "--once"])
