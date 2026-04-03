"""Durable fact model for onchain transaction attempts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
import tempfile


class RetryPosture(StrEnum):
    SUBMIT_ALLOWED = "submit_allowed"
    OBSERVE_ONLY = "observe_only"
    REPLACEMENT_ALLOWED = "replacement_allowed"
    DONE = "done"
    MANUAL_ATTENTION = "manual_attention"


@dataclass(frozen=True)
class TransactionAttemptRecord:
    key: str
    tx_hash: str | None = None
    submitted_at: datetime | None = None
    confirmed_at: datetime | None = None
    last_observed_at: datetime | None = None
    last_error_kind: str | None = None
    last_error_message: str | None = None
    replacement_blocked_count: int = 0

    def with_observation(self, *, observed_at: datetime) -> "TransactionAttemptRecord":
        return TransactionAttemptRecord(
            key=self.key,
            tx_hash=self.tx_hash,
            submitted_at=self.submitted_at,
            confirmed_at=self.confirmed_at,
            last_observed_at=_ensure_utc(observed_at),
            last_error_kind=self.last_error_kind,
            last_error_message=self.last_error_message,
            replacement_blocked_count=self.replacement_blocked_count,
        )


def observe_submit(
    record: TransactionAttemptRecord | None,
    *,
    key: str,
    tx_hash: str,
    observed_at: datetime,
) -> TransactionAttemptRecord:
    observed_at = _ensure_utc(observed_at)
    previous = _coerce_record(record, key=key)
    submitted_at = previous.submitted_at or observed_at
    return TransactionAttemptRecord(
        key=key,
        tx_hash=tx_hash,
        submitted_at=submitted_at,
        confirmed_at=previous.confirmed_at,
        last_observed_at=observed_at,
        last_error_kind=None,
        last_error_message=None,
        replacement_blocked_count=previous.replacement_blocked_count,
    )


def observe_confirmed(
    record: TransactionAttemptRecord | None,
    *,
    key: str,
    confirmed_at: datetime,
    tx_hash: str | None = None,
) -> TransactionAttemptRecord:
    confirmed_at = _ensure_utc(confirmed_at)
    previous = _coerce_record(record, key=key)
    return TransactionAttemptRecord(
        key=key,
        tx_hash=tx_hash or previous.tx_hash,
        submitted_at=previous.submitted_at,
        confirmed_at=confirmed_at,
        last_observed_at=confirmed_at,
        last_error_kind=None,
        last_error_message=None,
        replacement_blocked_count=previous.replacement_blocked_count,
    )


def observe_error(
    record: TransactionAttemptRecord | None,
    *,
    key: str,
    observed_at: datetime,
    error_kind: str,
    error_message: str,
    tx_hash: str | None = None,
    replacement_blocked: bool = False,
) -> TransactionAttemptRecord:
    observed_at = _ensure_utc(observed_at)
    previous = _coerce_record(record, key=key)
    effective_tx_hash = tx_hash or previous.tx_hash
    submitted_at = previous.submitted_at
    if effective_tx_hash is not None and submitted_at is None:
        submitted_at = observed_at
    return TransactionAttemptRecord(
        key=key,
        tx_hash=effective_tx_hash,
        submitted_at=submitted_at,
        confirmed_at=previous.confirmed_at,
        last_observed_at=observed_at,
        last_error_kind=error_kind,
        last_error_message=error_message,
        replacement_blocked_count=(
            previous.replacement_blocked_count + 1
            if replacement_blocked
            else previous.replacement_blocked_count
        ),
    )


def derive_retry_posture(
    record: TransactionAttemptRecord | None,
    *,
    now: datetime,
    replacement_grace_secs: int = 300,
    replacement_blocked_limit: int = 3,
) -> RetryPosture:
    if record is None:
        return RetryPosture.SUBMIT_ALLOWED

    now = _ensure_utc(now)
    if record.confirmed_at is not None:
        return RetryPosture.DONE
    if record.replacement_blocked_count >= replacement_blocked_limit:
        return RetryPosture.MANUAL_ATTENTION
    if record.tx_hash is None:
        return RetryPosture.SUBMIT_ALLOWED
    if record.submitted_at is None:
        return RetryPosture.OBSERVE_ONLY
    age_secs = (now - record.submitted_at).total_seconds()
    if age_secs < replacement_grace_secs:
        return RetryPosture.OBSERVE_ONLY
    return RetryPosture.REPLACEMENT_ALLOWED


class TransactionAttemptStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path).expanduser().resolve()

    def load(self) -> dict[str, TransactionAttemptRecord]:
        if not self._path.exists():
            return {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        return {
            key: _record_from_json(item)
            for key, item in payload.items()
        }

    def save(self, records: dict[str, TransactionAttemptRecord]) -> None:
        payload = {
            key: _record_to_json(record)
            for key, record in sorted(records.items())
        }
        _atomic_write_json(self._path, payload)


def _coerce_record(
    record: TransactionAttemptRecord | None,
    *,
    key: str,
) -> TransactionAttemptRecord:
    if record is None:
        return TransactionAttemptRecord(key=key)
    if record.key != key:
        raise ValueError(f"Attempt record key mismatch: expected {key}, got {record.key}")
    return record


def _record_to_json(record: TransactionAttemptRecord) -> dict[str, object]:
    payload = asdict(record)
    for field_name in ("submitted_at", "confirmed_at", "last_observed_at"):
        value = payload[field_name]
        payload[field_name] = None if value is None else value.isoformat()
    return payload


def _record_from_json(payload: dict[str, object]) -> TransactionAttemptRecord:
    return TransactionAttemptRecord(
        key=str(payload["key"]),
        tx_hash=_optional_str(payload.get("tx_hash")),
        submitted_at=_parse_datetime(payload.get("submitted_at")),
        confirmed_at=_parse_datetime(payload.get("confirmed_at")),
        last_observed_at=_parse_datetime(payload.get("last_observed_at")),
        last_error_kind=_optional_str(payload.get("last_error_kind")),
        last_error_message=_optional_str(payload.get("last_error_message")),
        replacement_blocked_count=int(payload.get("replacement_blocked_count", 0)),
    )


def _optional_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _parse_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    return _ensure_utc(datetime.fromisoformat(str(value)))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
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
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
