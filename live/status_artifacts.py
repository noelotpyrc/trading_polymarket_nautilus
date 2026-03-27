"""Helpers for durable current-status and status-history artifacts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class StatusArtifactWriter:
    """Writes the latest status snapshot and optional append-only history."""

    latest_path: Path
    history_path: Path | None = None

    def write(self, payload: dict[str, object]) -> None:
        self.latest_path.parent.mkdir(parents=True, exist_ok=True)
        self.latest_path.write_text(
            json.dumps(payload, default=_json_default, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if self.history_path is None:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=_json_default, sort_keys=True) + "\n")
            handle.flush()


def _json_default(value: object) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)
