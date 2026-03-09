"""Unit tests for live client config builders."""
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig

from live.config import polymarket_data_config
from live.config import sandbox_exec_config


class TestPolymarketDataConfig:
    def test_uses_polymarket_instrument_config(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_TEST_PRIVATE_KEY", "pk")
        monkeypatch.setenv("POLYMARKET_TEST_API_KEY", "api-key")
        monkeypatch.setenv("POLYMARKET_TEST_API_SECRET", "api-secret")
        monkeypatch.setenv("POLYMARKET_TEST_API_PASSPHRASE", "passphrase")
        monkeypatch.setenv("POLYMARKET_TEST_WALLET_ADDRESS", "0xabc")

        config = polymarket_data_config(["foo.POLYMARKET"], sandbox=True)

        assert config.private_key == "pk"
        assert config.api_key == "api-key"
        assert isinstance(config.instrument_config, PolymarketInstrumentProviderConfig)
        assert config.instrument_config.load_ids == frozenset(["foo.POLYMARKET"])
        assert config.instrument_config.use_gamma_markets is False


class TestSandboxExecConfig:
    def test_uses_usdc_e_balance_for_polymarket(self):
        config = sandbox_exec_config(starting_usdc=500.0)

        assert config.base_currency == "USDC.e"
        assert config.starting_balances == ["500 USDC.e"]
