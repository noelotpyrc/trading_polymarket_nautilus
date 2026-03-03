# Polymarket Historical Data Availability

## Summary

There is **no publicly available historical order book or trade tick data** from Polymarket's API.
The price history endpoint is the only historical data they expose.

---

## Endpoints Explored

### `GET /book?token_id=...`
- Returns a full L2 snapshot: all price levels with sizes for bids and asks
- **Current state only** — no time parameter, no historical replay
- Used by the live Nautilus adapter via WebSocket for streaming order book deltas
- Example response shape:
  ```json
  {
    "asset_id": "...",
    "timestamp": "1772500923804",
    "bids": [{"price": "0.50", "size": "1200.00"}, ...],
    "asks": [{"price": "0.51", "size": "800.00"}, ...]
  }
  ```

### `GET /data/trades`
- Requires **Level 2 auth** (API key + signing)
- Returns **your own trades only** — not public market trade tape
- Not useful for backtesting

### `GET /live-activity/events/{condition_id}`
- Returns 404 for most markets
- Appears to only work for specific "live event" style markets, not standard binary options

### WebSocket `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Streams live order book deltas + trade ticks in real time
- What the Nautilus Polymarket live data adapter subscribes to
- **No historical replay** — live feed only

---

## Third-Party Data Vendors

### Tardis
- NautilusTrader has a built-in Tardis adapter
- **Does not cover Polymarket**

---

## Options for Higher-Quality Backtest Data

### 1. Record WebSocket going forward (best option)
Connect to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribe to target
markets, and persist order book deltas + trades to disk. After accumulating history,
replay via NautilusTrader's backtest engine using `OrderBookDelta` and `TradeTick` data.
- Pros: real data, exact fills, realistic slippage
- Cons: requires running infrastructure, no past data on day 1

### 2. Reconstruct from on-chain data
Polymarket runs on Polygon blockchain. Every fill is an on-chain transaction and is
publicly queryable. In principle you can reconstruct a full trade tape from Polygon history.
- Pros: full historical depth going back to Polymarket's launch
- Cons: significant engineering effort to parse and map on-chain events to trades

### 3. Accept current price history bars (current approach)
Use `GET /prices-history` (via `PolymarketDataLoader`) which returns sampled price points
converted to 1-minute OHLCV bars.
- Pros: zero effort, already implemented
- Cons: sampled midpoints, no spread, no depth, no volume — fills are approximate

---

## Current Approach

`historical/backtest/run.py` uses `PolymarketDataLoader.from_market_slug()` which calls
`/prices-history` and converts the result to `Bar` objects. The backtest engine fills
market orders against these bars using `bar_execution=True`.

For a 15-minute binary market with a total lifetime of 15 minutes, 1-minute bar resolution
covers the full market window with ~15 bars — reasonable for signal validation, but fills
are not representative of real execution quality.
