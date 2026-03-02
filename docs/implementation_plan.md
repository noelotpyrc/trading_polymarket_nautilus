# Implementation Plan

End goal: a Nautilus-based trading system that backtests on Polymarket historical data and runs the same strategy live against the Polymarket CLOB.

---

## Target Market Universe

**BTC price up/down markets** — all time intervals: 5m, 15m, 1h, 4h (and others as they appear).

These are markets of the form "Bitcoin Up or Down — 15 Minutes": at expiry, if BTC price is higher than at open, YES resolves 1; otherwise NO resolves 1.

---

## Current State

| Component | Status |
|---|---|
| Wallet setup (generate, fund, approve — buy + sell) | Done |
| Live buy + sell via `trade.py` | Done |
| Gamma event + market metadata fetching | Done |
| 1-min price history fetching | Done |
| Price history cleaning (15m markets) | Done |
| Binance 1m OHLCV (backtest signal data) | Done — merged CSV on external drive (to Feb 2026) |
| Settlement data pipeline (Chainlink) | Not started |
| Signal generation spec (`vol_signal_spec.md`) | Done |
| Signal generation implementation | Not started |
| Strategy trading rules | Pending (user) |
| Nautilus backtest Phase 1 | Not started |
| Nautilus backtest Phase 2 | Not started |
| Nautilus live trading node | Not started |

---

## Settlement Data Sources

The resolution price source depends on market type:

| Market type | Resolution source | Settlement mechanism |
|---|---|---|
| "Bitcoin Up or Down" (5m, 15m, 1h, 4h, ...) | **Chainlink BTC/USD Data Stream** | Chainlink Automation (fully automated) |
| Classic threshold markets ("Will BTC be above $X on date Y?") | **Binance BTCUSDT 1-min High** | UMA Optimistic Oracle |

### Chainlink (reference only — not building a pipeline now)
- "Bitcoin Up or Down" markets settle against Chainlink BTC/USD Data Streams (pull-based, sub-second)
- Feed ID: `0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8`
- Historical access requires an enterprise Data Streams subscription — not free
- Legacy push feed on Polygon (`0xc907E116054Ad103354f2D350FD2514433D57F6f`) is ~60s heartbeat but a different product from Data Streams
- **Deferred**: Chainlink data pipeline will not be built in this phase

### Binance BTCUSDT (primary data source)
- Used as proxy for both **signal generation** and **settlement simulation**
- Correlation with Chainlink is near-perfect for 5m+ intervals — acceptable for backtesting and live trading
- Pipeline already built in `~/projects/trading_cex_data_feed`

---

## Signal Data Pipeline — Binance (existing)

**Historical data**: already downloaded at `/Volumes/Extreme SSD/trading_data/cex/ohlvc/binance_btcusdt_perp_1m/BTCUSDT-1m-merged.csv`
- 3.1M rows, 2020-01-01 → 2025-11-30 (merged); monthly zips extend through 2026-02
- Covers the full Polymarket BTC up/down market history (Chainlink integration started Sept 2025)
- No pipeline work needed for backtesting — load directly from CSV (merged) or zips

**For live trading**: NautilusTrader has a native `BinanceDataClientConfig` that streams klines via WebSocket — no separate feed process needed. The `TradingNode` subscribes directly.

---

## Polymarket Data Sources

#### 1. 1-min price snapshots — `/prices-history`
- `GET https://clob.polymarket.com/prices-history?market={token_id}&fidelity=1&startTs=...&endTs=...`
- Returns `[{t, p}]` — YES token mid price, one per minute
- **Use for**: Phase 1 backtest

#### 2. Historical tick trades — `data-api.polymarket.com/trades`
- `GET https://data-api.polymarket.com/trades?market={condition_id}&limit=10000&offset=0`
- Real fills: price, size, side, timestamp. Hard cap ~20k per market.
- **Use for**: Phase 2 backtest

#### 3. Deep historical trades — Goldsky subgraph
- GraphQL `orderFilledEvents` on the Goldsky orderbook subgraph. No offset cap.
- **Use for**: Phase 2 if data-api cap is hit

#### 4. Real-time tick trades — CLOB WebSocket
- `wss://ws-subscriptions-clob.polymarket.com/ws/market` → `last_trade_price` events
- Handled natively by NautilusTrader Polymarket adapter
- **Use for**: live strategy execution

---

## Nautilus Backtest

### Instrument
Each BTC up/down market = one `BinaryOption` instrument (YES token).

```python
from nautilus_trader.model.instruments import BinaryOption
```

### Data format — Phase 1 (bars)
1-min price snapshots → `Bar` objects (OHLC = close price, volume = 0).
Use `BarDataWrangler` to convert DataFrame → `Bar[]`.

### Data format — Phase 2 (real ticks)
Tick trades → `TradeTick` objects via `TradeTickDataWrangler`.

### BacktestEngine setup — two venues

```python
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig

engine = BacktestEngine(config=BacktestEngineConfig(...))

# BINANCE venue — signal data only, no orders
engine.add_venue(venue=Venue("BINANCE"), account_type=AccountType.MARGIN,
                 base_currency=USDT, starting_balances=[Money(0, USDT)])
engine.add_instrument(btcusdt_instrument)
engine.add_data(btcusdt_1m_bars)            # Binance 1m klines → signal

# POLYMARKET venue — orders + P&L tracked here
engine.add_venue(venue=Venue("POLYMARKET"), account_type=AccountType.CASH,
                 base_currency=USDC, starting_balances=[Money(500, USDC)])
engine.add_instrument(binary_option_instrument)
engine.add_data(yes_token_bars_or_ticks)    # Polymarket price → fill simulation

engine.add_strategy(strategy)
engine.run()
```

### Fill model
- Phase 1: fill at bar close price. No slippage.
- Phase 2: fill at next tick after signal fires.

---

## Signal Generation

Spec: `docs/vol_signal_spec.md`

The signal pipeline runs entirely on Binance 1m OHLC data and produces `prob_yes_emp` at each bar — the empirical probability that `close[t+TTL] > strike_K`.

### Pipeline summary

```
1m OHLC (Binance)
  → Parkinson volatility features (windows: 10, 15, 30, 45, 60, 1440)
  → Walk-forward daily linear regression → pred_mar_{1,3,5}
  → Blended MAR → sigma_W (volatility over remaining TTL)
  → 15-min market clock → TTL + strike_K
  → Empirical z-pool (7-day rolling, no lookahead)
  → prob_yes_emp = P(close[t+TTL] > K)
```

### Key properties
- **Retrained daily**: one linear regression per horizon (N=1,3,5), train window = `[D-7d, D-N min)`
- **15-minute epochs**: `TTL ∈ [1..14]` counting down to expiry; setter bar at `TTL=15` sets next epoch's K
- **No normality assumption**: uses empirical ECDF of historical standardised returns for the tail probability
- **Anti-lookahead**: training cutoff, z-pool cutoff, and K-setter logic all use strictly past data

### Implementation

To be built as `historical/process/vol_signal.py` — standalone script that:
1. Loads Binance 1m CSV
2. Runs the full pipeline → outputs `prob_yes_emp` per bar as CSV
3. Validates on the Sept 2025–Feb 2026 window (the live Polymarket market period)

Then ported into the Nautilus strategy's `on_bar` handler for live use.

---

## Strategy

A Nautilus `Strategy` subclass. Same class runs in backtest and live.

```
live/strategies/
  btc_updown.py    # BTC up/down strategy (all intervals)
```

### Trading rules
To be written by user (separate rules doc). Will define thresholds on `prob_yes_emp` for entry/exit.

### Skeleton

```python
def on_start(self):
    # Binance klines for signal — no API key needed for public data
    self.subscribe_bars(BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"))
    # Polymarket YES token for market state
    self.subscribe_bars(BarType.from_str(f"{token_id}.POLYMARKET-1-MINUTE-LAST-EXTERNAL"))

def on_bar(self, bar: Bar):
    if bar.type.instrument_id.venue == BINANCE_VENUE:
        self._update_signal(bar)      # recompute prob_yes_emp
    else:
        self._check_entry_exit(bar)   # apply trading rules against prob_yes_emp
```

### Position sizing
- Minimum $5 per order (platform limit)
- Fixed-dollar or fixed-fractional

---

## Live Trading Node

```
live/
  node.py          # TradingNode entry point
  config.py        # TradingNodeConfig, client configs
  strategies/      # strategy classes
  setup/           # wallet/credentials scripts
  trade.py         # ad-hoc order placement (for manual testing)
```

### Multi-feed config

```python
TradingNodeConfig(
    data_clients={
        "BINANCE":     BinanceDataClientConfig(account_type=BinanceAccountType.SPOT, ...),
        "POLYMARKET":  PolymarketDataClientConfig(...),
    },
    exec_clients={
        "POLYMARKET":  PolymarketExecClientConfig(...),   # no Binance exec needed
    },
)
```

Binance data client requires no API key for public kline streams.
Order routing is automatic — `submit_order` routes to Polymarket because `instrument_id.venue == POLYMARKET`.

Config from `.env`: `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`, `PRIVATE_KEY`, `WALLET_ADDRESS`.

---

## Build Order

1. **Implement signal pipeline** — `historical/process/vol_signal.py`, validate on Sept 2025–Feb 2026 data
2. **Write trading rules** — user defines entry/exit thresholds on `prob_yes_emp`
3. **Implement strategy** — `live/strategies/btc_updown.py`, embeds signal logic in `on_bar`
4. **Nautilus backtest Phase 1** — `BacktestEngine` with 1-min YES token bars + Binance merged CSV
5. **Build tick data fetcher** — `historical/fetch/market_trades.py` (data-api + Goldsky fallback)
6. **Nautilus backtest Phase 2** — rerun with real `TradeTick` data, compare vs Phase 1
7. **Live node scaffold** — `node.py`, `config.py`, wire Binance WebSocket feed + Polymarket clients
8. **Shadow mode** — run node, log signals, no real orders
9. **Live deployment** — start node + sweep cadence

---

## Deferred

- **Chainlink data pipeline**: authoritative settlement prices for "Bitcoin Up or Down" markets come from Chainlink Data Streams, which requires an enterprise subscription. Binance 1m close is used as a proxy for now. Revisit if backtest results show material divergence between the two sources.

---

## Open Questions

- Entry timing: enter at market open, or after N minutes of price discovery?
- How to handle markets with incomplete price history (fewer points than expected)?
- Sweep cadence: before/after each session, or time-based?
- Phase 2 tick data: is the 20k cap a real constraint for short-duration BTC markets?
