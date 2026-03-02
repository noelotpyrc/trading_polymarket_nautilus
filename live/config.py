"""TradingNodeConfig builder for live BTC up/down trading."""
import os

from dotenv import load_dotenv
from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.polymarket import (
    PolymarketDataClientConfig,
    PolymarketExecClientConfig,
)
from nautilus_trader.config import InstrumentProviderConfig, TradingNodeConfig

load_dotenv()


def build_node_config(pm_instrument_ids: list[str]) -> TradingNodeConfig:
    """Build TradingNodeConfig reading credentials from .env."""
    private_key = os.environ["PRIVATE_KEY"]
    api_key = os.environ["POLYMARKET_API_KEY"]
    api_secret = os.environ["POLYMARKET_API_SECRET"]
    # NautilusTrader adapter reads POLYMARKET_PASSPHRASE (no API_ prefix);
    # fall back to POLYMARKET_API_PASSPHRASE if the user's .env uses that name.
    passphrase = os.getenv("POLYMARKET_PASSPHRASE") or os.environ["POLYMARKET_API_PASSPHRASE"]

    return TradingNodeConfig(
        data_clients={
            "BINANCE": BinanceDataClientConfig(
                account_type=BinanceAccountType.SPOT,
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset(["BTCUSDT.BINANCE"]),
                ),
            ),
            "POLYMARKET": PolymarketDataClientConfig(
                private_key=private_key,
                api_key=api_key,
                api_secret=api_secret,
                passphrase=passphrase,
            ),
        },
        exec_clients={
            "POLYMARKET": PolymarketExecClientConfig(
                private_key=private_key,
                api_key=api_key,
                api_secret=api_secret,
                passphrase=passphrase,
            ),
        },
    )
