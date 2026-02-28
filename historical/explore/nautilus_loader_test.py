import asyncio

from nautilus_trader.adapters.polymarket import PolymarketDataLoader
import pandas as pd



async def main():
    # Create loader from market slug (recommended)
    loader = await PolymarketDataLoader.from_market_slug("btc-updown-15m-1770594300")

    # Loader is ready to use with instrument and token_id set
    print(loader.instrument)
    print(loader.token_id)

    # Define time range
    end = pd.Timestamp.now(tz="UTC")
    start = end - pd.Timedelta(hours=48)

    # Fetch and parse trade ticks (1-minute fidelity)
    price_history = await loader.fetch_price_history(
    token_id=loader.token_id,
    start_time_ms=int(start.timestamp() * 1000),
    end_time_ms=int(end.timestamp() * 1000),
    fidelity=1,  # 1 = 1 minute resolution
)


    print(price_history)

asyncio.run(main())