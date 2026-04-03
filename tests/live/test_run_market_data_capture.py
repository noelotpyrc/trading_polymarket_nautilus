from __future__ import annotations

import pytest

from live.market_data_capture import default_binance_stream_url
from live.run_market_data_capture import _resolve_artifact_paths


def test_resolve_artifact_paths_requires_output() -> None:
    with pytest.raises(SystemExit, match="Provide --artifacts-dir or --samples-path"):
        _resolve_artifact_paths(artifacts_dir=None, samples_path=None)


def test_resolve_artifact_paths_uses_artifacts_dir_defaults(tmp_path) -> None:
    paths = _resolve_artifact_paths(
        artifacts_dir=str(tmp_path / "capture"),
        samples_path=None,
    )

    assert paths["log_path"] == str((tmp_path / "capture" / "market_data.log").resolve())
    assert paths["samples_path"] == str((tmp_path / "capture" / "market_data_samples.jsonl").resolve())


def test_default_binance_stream_url_global() -> None:
    assert (
        default_binance_stream_url("BTCUSDT", us=False)
        == "wss://fstream.binance.com/ws/btcusdt@bookTicker"
    )


def test_default_binance_stream_url_us_requires_override() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        default_binance_stream_url("BTCUSDT", us=True)
