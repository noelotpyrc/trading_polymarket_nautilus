from datetime import datetime, timezone

from live.transaction_attempts import (
    RetryPosture,
    TransactionAttemptRecord,
    TransactionAttemptStore,
    derive_retry_posture,
    observe_confirmed,
    observe_error,
    observe_submit,
)


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, 30, hour, minute, tzinfo=timezone.utc)


def test_missing_attempt_defaults_to_submit_allowed():
    posture = derive_retry_posture(None, now=_utc(12))

    assert posture == RetryPosture.SUBMIT_ALLOWED


def test_submitted_attempt_starts_in_observe_only_then_allows_replacement():
    record = observe_submit(
        None,
        key="cond-1",
        tx_hash="0xabc",
        observed_at=_utc(12, 0),
    )

    assert derive_retry_posture(record, now=_utc(12, 4)) == RetryPosture.OBSERVE_ONLY
    assert derive_retry_posture(record, now=_utc(12, 6)) == RetryPosture.REPLACEMENT_ALLOWED


def test_confirmed_attempt_is_done():
    record = observe_submit(
        None,
        key="cond-1",
        tx_hash="0xabc",
        observed_at=_utc(12, 0),
    )
    record = observe_confirmed(
        record,
        key="cond-1",
        confirmed_at=_utc(12, 2),
    )

    assert derive_retry_posture(record, now=_utc(12, 10)) == RetryPosture.DONE


def test_error_without_tx_hash_keeps_submit_allowed():
    record = observe_error(
        None,
        key="cond-1",
        observed_at=_utc(12, 0),
        error_kind="transport",
        error_message="rpc dropped",
    )

    assert record.tx_hash is None
    assert derive_retry_posture(record, now=_utc(12, 10)) == RetryPosture.SUBMIT_ALLOWED


def test_error_after_tx_hash_keeps_observe_only_until_grace_expires():
    record = observe_submit(
        None,
        key="cond-1",
        tx_hash="0xabc",
        observed_at=_utc(12, 0),
    )
    record = observe_error(
        record,
        key="cond-1",
        observed_at=_utc(12, 1),
        error_kind="confirmation",
        error_message="receipt timeout",
    )

    assert derive_retry_posture(record, now=_utc(12, 3)) == RetryPosture.OBSERVE_ONLY
    assert derive_retry_posture(record, now=_utc(12, 6)) == RetryPosture.REPLACEMENT_ALLOWED


def test_replacement_blocked_limit_escalates_to_manual_attention():
    record: TransactionAttemptRecord | None = observe_submit(
        None,
        key="cond-1",
        tx_hash="0xabc",
        observed_at=_utc(12, 0),
    )
    for minute in (6, 7, 8):
        record = observe_error(
            record,
            key="cond-1",
            observed_at=_utc(12, minute),
            error_kind="replacement_blocked",
            error_message="replacement transaction underpriced",
            replacement_blocked=True,
        )

    assert record is not None
    assert record.replacement_blocked_count == 3
    assert derive_retry_posture(record, now=_utc(12, 9)) == RetryPosture.MANUAL_ATTENTION


def test_store_roundtrip_preserves_attempt_fields(tmp_path):
    store = TransactionAttemptStore(tmp_path / "attempts.json")
    record = observe_submit(
        None,
        key="cond-1",
        tx_hash="0xabc",
        observed_at=_utc(12, 0),
    )
    record = observe_error(
        record,
        key="cond-1",
        observed_at=_utc(12, 1),
        error_kind="transport",
        error_message="rpc dropped",
    )
    store.save(
        {
            "cond-1": record,
            "cond-2": observe_confirmed(
                observe_submit(
                    None,
                    key="cond-2",
                    tx_hash="0xdef",
                    observed_at=_utc(12, 0),
                ),
                key="cond-2",
                confirmed_at=_utc(12, 2),
            ),
        }
    )

    loaded = store.load()

    assert set(loaded) == {"cond-1", "cond-2"}
    assert loaded["cond-1"].tx_hash == "0xabc"
    assert loaded["cond-1"].last_error_kind == "transport"
    assert loaded["cond-1"].last_error_message == "rpc dropped"
    assert loaded["cond-1"].submitted_at == _utc(12, 0)
    assert loaded["cond-2"].confirmed_at == _utc(12, 2)
