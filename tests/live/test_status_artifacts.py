"""Tests for atomic status artifact writes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading

from live.status_artifacts import StatusArtifactWriter


def test_status_writer_keeps_latest_json_parseable_under_concurrent_reads(tmp_path):
    latest_path = tmp_path / "status.json"
    history_path = tmp_path / "status_history.jsonl"
    writer = StatusArtifactWriter(latest_path=latest_path, history_path=history_path)
    stop = threading.Event()
    failures: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            if not latest_path.exists():
                continue
            text = latest_path.read_text(encoding="utf-8")
            if not text.strip():
                continue
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                failures.append(f"{exc}: {text!r}")
                stop.set()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        for seq in range(200):
            writer.write(
                {
                    "recorded_at": datetime(2026, 3, 29, 1, 0, seq % 60, tzinfo=timezone.utc),
                    "seq": seq,
                    "payload": "x" * 2048,
                }
            )
    finally:
        stop.set()
        reader_thread.join(timeout=5)

    assert failures == []
    assert json.loads(latest_path.read_text(encoding="utf-8"))["seq"] == 199
    history_rows = [
        json.loads(line)
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(history_rows) == 200
