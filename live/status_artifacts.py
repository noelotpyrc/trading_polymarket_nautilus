"""Helpers for durable current-status and status-history artifacts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class StatusArtifactWriter:
    """Writes the latest status snapshot and optional append-only history."""

    latest_path: Path
    history_path: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def write(self, payload: dict[str, object]) -> None:
        latest_text = json.dumps(payload, default=_json_default, indent=2, sort_keys=True) + "\n"
        history_text = json.dumps(payload, default=_json_default, sort_keys=True) + "\n"

        with self._lock:
            _atomic_write_text(self.latest_path, latest_text)
            if self.history_path is None:
                return
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(history_text)
                handle.flush()
                os.fsync(handle.fileno())


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _json_default(value: object) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)
