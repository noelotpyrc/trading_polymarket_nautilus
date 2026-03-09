"""Unit tests for live node helpers."""
import json
from unittest.mock import MagicMock, patch

import pytest

from live.node import (
    _parse_interval_secs,
    build_node,
    prepare_run,
    resolve_upcoming_windows,
    schedule_stop,
)


def _set_sandbox_env(monkeypatch):
    monkeypatch.setenv("POLYMARKET_TEST_PRIVATE_KEY", "pk")
    monkeypatch.setenv("POLYMARKET_TEST_API_KEY", "api-key")
    monkeypatch.setenv("POLYMARKET_TEST_API_SECRET", "api-secret")
    monkeypatch.setenv("POLYMARKET_TEST_API_PASSPHRASE", "passphrase")
    monkeypatch.setenv("POLYMARKET_TEST_WALLET_ADDRESS", "0xabc")


def _clear_env(monkeypatch):
    for key in (
        "POLYMARKET_TEST_PRIVATE_KEY",
        "POLYMARKET_TEST_API_KEY",
        "POLYMARKET_TEST_API_SECRET",
        "POLYMARKET_TEST_API_PASSPHRASE",
        "POLYMARKET_TEST_WALLET_ADDRESS",
        "PRIVATE_KEY",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_PASSPHRASE",
        "POLYMARKET_API_PASSPHRASE",
        "POLYMARKET_FUNDER",
        "WALLET_ADDRESS",
    ):
        monkeypatch.delenv(key, raising=False)


class TestParseIntervalSecs:
    def test_15m(self):
        assert _parse_interval_secs("btc-updown-15m") == 900

    def test_5m(self):
        assert _parse_interval_secs("btc-updown-5m") == 300

    def test_1h(self):
        assert _parse_interval_secs("btc-updown-1h") == 3600

    def test_4h(self):
        assert _parse_interval_secs("btc-updown-4h") == 14400

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_interval_secs("btc-updown")


class TestResolveUpcomingWindows:
    def _make_market(self, condition_id: str, yes_token_id: str) -> dict:
        return {
            "conditionId": condition_id,
            "clobTokenIds": json.dumps([yes_token_id, "no-token-id"]),
        }

    def _mock_response(self, markets: list) -> MagicMock:
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = markets
        resp.raise_for_status.return_value = None
        return resp

    @patch("live.node.requests.get")
    def test_instrument_id_format(self, mock_get):
        condition_id = "0xabc123"
        token_id = "111222333"
        mock_get.return_value = self._mock_response(
            [self._make_market(condition_id, token_id)]
        )

        windows = resolve_upcoming_windows("btc-updown-15m", hours_ahead=0)

        assert len(windows) == 1
        instrument_id, _ = windows[0]
        assert instrument_id == f"{condition_id}-{token_id}.POLYMARKET"

    @patch("live.node.requests.get")
    def test_window_end_ns_correct(self, mock_get):
        mock_get.return_value = self._mock_response(
            [self._make_market("cid", "tid")]
        )

        windows = resolve_upcoming_windows("btc-updown-15m", hours_ahead=0)

        assert len(windows) == 1
        _, window_end_ns = windows[0]
        assert window_end_ns > 1_600_000_000 * 1_000_000_000
        assert window_end_ns % (900 * 1_000_000_000) == 0

    @patch("live.node.requests.get")
    def test_skips_missing_markets(self, mock_get):
        mock_get.return_value = self._mock_response([])

        windows = resolve_upcoming_windows("btc-updown-15m", hours_ahead=0)

        assert windows == []

    @patch("live.node.requests.get")
    def test_skips_markets_without_condition_id(self, mock_get):
        mock_get.return_value = self._mock_response(
            [{"conditionId": "", "clobTokenIds": '["tid"]'}]
        )

        windows = resolve_upcoming_windows("btc-updown-15m", hours_ahead=0)

        assert windows == []

    @patch("live.node.requests.get")
    def test_multiple_windows_ordered(self, mock_get):
        def side_effect(url, params, timeout):
            ts = int(params["slug"].split("-")[-1])
            return self._mock_response([self._make_market(f"cid-{ts}", f"tid-{ts}")])

        mock_get.side_effect = side_effect

        windows = resolve_upcoming_windows("btc-updown-15m", hours_ahead=1)

        assert len(windows) == 5
        end_times = [w[1] for w in windows]
        assert end_times == sorted(end_times)

    @patch("live.node.requests.get")
    def test_request_error_skips_window(self, mock_get):
        mock_get.side_effect = Exception("connection refused")

        windows = resolve_upcoming_windows("btc-updown-15m", hours_ahead=0)

        assert windows == []


class TestPrepareRun:
    def test_rejects_missing_sandbox_env_vars(self, monkeypatch):
        _clear_env(monkeypatch)

        with pytest.raises(SystemExit, match="Missing required sandbox env vars"):
            prepare_run(
                slug_pattern="btc-updown-15m",
                hours_ahead=1,
                sandbox=True,
                binance_us=False,
                run_secs=None,
            )

    def test_rejects_missing_live_env_vars(self, monkeypatch):
        _clear_env(monkeypatch)

        with pytest.raises(SystemExit, match="Missing required live env vars"):
            prepare_run(
                slug_pattern="btc-updown-15m",
                hours_ahead=1,
                sandbox=False,
                binance_us=False,
                run_secs=None,
            )

    def test_rejects_non_positive_run_secs(self, monkeypatch):
        _set_sandbox_env(monkeypatch)

        with pytest.raises(SystemExit, match="--run-secs must be a positive integer"):
            prepare_run(
                slug_pattern="btc-updown-15m",
                hours_ahead=1,
                sandbox=True,
                binance_us=False,
                run_secs=0,
            )

    def test_rejects_duplicate_windows(self, monkeypatch):
        _set_sandbox_env(monkeypatch)
        monkeypatch.setattr(
            "live.node.resolve_upcoming_windows",
            lambda slug_pattern, hours_ahead: [("a.POLYMARKET", 1), ("a.POLYMARKET", 2)],
        )

        with pytest.raises(SystemExit, match="Resolved duplicate Polymarket instruments"):
            prepare_run(
                slug_pattern="btc-updown-15m",
                hours_ahead=1,
                sandbox=True,
                binance_us=False,
                run_secs=None,
            )

    def test_rejects_non_monotonic_windows(self, monkeypatch):
        _set_sandbox_env(monkeypatch)
        monkeypatch.setattr(
            "live.node.resolve_upcoming_windows",
            lambda slug_pattern, hours_ahead: [("a.POLYMARKET", 10), ("b.POLYMARKET", 5)],
        )

        with pytest.raises(SystemExit, match="not strictly increasing"):
            prepare_run(
                slug_pattern="btc-updown-15m",
                hours_ahead=1,
                sandbox=True,
                binance_us=False,
                run_secs=None,
            )

    def test_warns_when_first_window_near_expiry(self, monkeypatch, capsys):
        _set_sandbox_env(monkeypatch)
        monkeypatch.setattr(
            "live.node.resolve_upcoming_windows",
            lambda slug_pattern, hours_ahead: [("a.POLYMARKET", 60_000_000_000)],
        )
        monkeypatch.setattr("live.node.time.time_ns", lambda: 0)

        windows = prepare_run(
            slug_pattern="btc-updown-15m",
            hours_ahead=1,
            sandbox=True,
            binance_us=True,
            run_secs=180,
        )

        out = capsys.readouterr().out
        assert windows == [("a.POLYMARKET", 60_000_000_000)]
        assert "WARNING: First window ends in 60s" in out
        assert "Auto-stop after   : 180s" in out


class TestScheduleStop:
    def test_arms_timer(self, monkeypatch):
        calls = {}

        class FakeTimer:
            def __init__(self, interval, callback):
                calls["interval"] = interval
                calls["callback"] = callback
                self.daemon = False

            def start(self):
                calls["started"] = True

        node = MagicMock()
        monkeypatch.setattr("live.node.threading.Timer", FakeTimer)

        timer = schedule_stop(node, 30)

        assert timer is not None
        assert calls["interval"] == 30
        assert calls["callback"] == node.stop
        assert calls["started"] is True

    def test_returns_none_for_unbounded_runs(self):
        assert schedule_stop(MagicMock(), None) is None


class TestBuildNode:
    def test_uses_quote_qty_conversion_for_sandbox(self, monkeypatch):
        _set_sandbox_env(monkeypatch)
        captured = {}

        class FakeTradingNode:
            def __init__(self, config):
                captured["config"] = config

            def add_data_client_factory(self, *args):
                pass

            def add_exec_client_factory(self, *args):
                pass

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", FakeTradingNode)

        node = build_node(["foo.POLYMARKET"], sandbox=True, binance_us=False)

        assert isinstance(node, FakeTradingNode)
        assert captured["config"].exec_engine.reconciliation is False
        assert captured["config"].exec_engine.convert_quote_qty_to_base is True

    def test_preserves_quote_quantity_orders_for_live(self, monkeypatch):
        monkeypatch.setenv("PRIVATE_KEY", "pk")
        monkeypatch.setenv("POLYMARKET_API_KEY", "api-key")
        monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret")
        monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "passphrase")
        monkeypatch.setenv("WALLET_ADDRESS", "0xabc")
        captured = {}

        class FakeTradingNode:
            def __init__(self, config):
                captured["config"] = config

            def add_data_client_factory(self, *args):
                pass

            def add_exec_client_factory(self, *args):
                pass

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", FakeTradingNode)

        node = build_node(["foo.POLYMARKET"], sandbox=False, binance_us=False)

        assert isinstance(node, FakeTradingNode)
        assert captured["config"].exec_engine.reconciliation is True
        assert captured["config"].exec_engine.convert_quote_qty_to_base is False
