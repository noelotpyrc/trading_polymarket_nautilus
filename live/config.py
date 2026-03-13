"""Individual client config builders for live trading.

Two credential sets in .env:
  - Production (PRIVATE_KEY, WALLET_ADDRESS, POLYMARKET_API_*): live mode
  - Test       (POLYMARKET_TEST_*): sandbox mode, zero-funds throwaway wallet

Each builder is independent — run scripts assemble TradingNodeConfig themselves
by picking exactly the clients they need.
"""
import os

from dotenv import load_dotenv
from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.polymarket import (
    PolymarketDataClientConfig,
    PolymarketExecClientConfig,
)
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.config import InstrumentProviderConfig

load_dotenv()


def binance_data_config(us: bool = False) -> BinanceDataClientConfig:
    """Binance USDT perpetual futures data feed."""
    return BinanceDataClientConfig(
        us=us,
        account_type=BinanceAccountType.USDT_FUTURES,
        instrument_provider=InstrumentProviderConfig(
            load_ids=frozenset(["BTCUSDT-PERP.BINANCE"]),
        ),
    )


def polymarket_data_config(
    pm_instrument_ids: list[str],
    sandbox: bool = False,
) -> PolymarketDataClientConfig:
    """Polymarket quote tick feed. Uses test credentials in sandbox mode."""
    if sandbox:
        private_key = os.environ["POLYMARKET_TEST_PRIVATE_KEY"]
        api_key     = os.environ["POLYMARKET_TEST_API_KEY"]
        api_secret  = os.environ["POLYMARKET_TEST_API_SECRET"]
        passphrase  = os.environ["POLYMARKET_TEST_API_PASSPHRASE"]
        funder      = os.environ["POLYMARKET_TEST_WALLET_ADDRESS"]
    else:
        private_key = os.environ["PRIVATE_KEY"]
        api_key     = os.environ["POLYMARKET_API_KEY"]
        api_secret  = os.environ["POLYMARKET_API_SECRET"]
        passphrase  = os.getenv("POLYMARKET_PASSPHRASE") or os.environ["POLYMARKET_API_PASSPHRASE"]
        funder      = os.getenv("POLYMARKET_FUNDER") or os.environ["WALLET_ADDRESS"]

    return PolymarketDataClientConfig(
        private_key=private_key,
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        funder=funder,
        drop_quotes_missing_side=False,
        instrument_config=PolymarketInstrumentProviderConfig(
            load_ids=frozenset(pm_instrument_ids),
        ),
    )


def polymarket_exec_config() -> PolymarketExecClientConfig:
    """Polymarket live execution — production credentials, real orders."""
    return PolymarketExecClientConfig(
        private_key=os.environ["PRIVATE_KEY"],
        api_key=os.environ["POLYMARKET_API_KEY"],
        api_secret=os.environ["POLYMARKET_API_SECRET"],
        passphrase=os.getenv("POLYMARKET_PASSPHRASE") or os.environ["POLYMARKET_API_PASSPHRASE"],
        funder=os.getenv("POLYMARKET_FUNDER") or os.environ["WALLET_ADDRESS"],
    )


def sandbox_exec_config(starting_usdc: float = 500.0) -> SandboxExecutionClientConfig:
    """Simulated execution — no real orders, starts with synthetic USDC.e balance."""
    return SandboxExecutionClientConfig(
        venue="POLYMARKET",
        account_type="CASH",
        base_currency="USDC.e",
        starting_balances=[f"{starting_usdc:.0f} USDC.e"],
    )
