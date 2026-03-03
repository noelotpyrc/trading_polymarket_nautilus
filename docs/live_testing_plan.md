# Live Testing Plan

How to test the live trading infrastructure before deploying with real capital.

Polymarket has no testnet — all testing must be done either with mocked execution or with real but minimal orders.

---

## Stage 1 — Replay + Mock Exec (fully offline)

Feed recorded WS book data as the Polymarket data source and replace the exec client with a mock that logs orders without submitting them.

**What it tests**
- Strategy signal computation from Binance bars
- Order trigger logic (entry/exit thresholds on `prob_yes_emp`)
- Order sizing and instrument routing
- Position tracking updates

**What it does NOT test**
- Actual CLOB order submission
- Fill confirmation handling
- Polymarket adapter data parsing (adapter is bypassed)

**Data sources**
- Binance: recorded 1m bars or live Binance WS (no API key needed for public klines)
- Polymarket: `data/ws_recordings/*.jsonl.gz` replayed as `QuoteTick` events

**How**
- Build a replay data client that reads `.jsonl.gz` files and emits `QuoteTick` at the recorded timestamps
- Replace `PolymarketExecClientConfig` with a `MockExecClient` that logs order requests and simulates immediate fill confirmations
- Run fully reproducible — same scenario can be replayed multiple times

---

## Stage 2 — Shadow Mode (live data, mock exec)

Connect to real live data feeds but still block actual order submission.

**What it tests**
- Everything in Stage 1, plus:
- Polymarket data adapter parsing real WS events (`book`, `last_trade_price`)
- Binance live kline stream → signal pipeline latency
- Market discovery and instrument lifecycle (window transitions)

**What it does NOT test**
- Actual CLOB submission and fill handling

**How**
- Run full `TradingNode` with real `BinanceDataClientConfig` + `PolymarketDataClientConfig`
- Replace only `PolymarketExecClientConfig` with a `MockExecClient`
- Log every would-be order with timestamp, price, size, side

---

## Stage 3 — Real Minimum Orders

Run the full live node with real order submission, hard-capped at minimum size.

**What it tests**
- Everything, including CLOB order submission, fill confirmation, position tracking

**Risk controls**
- Hard cap: $5–$10 per order, $20 total exposure
- Only one position open at a time
- Add a `max_notional` guard in the strategy before `submit_order`

**Exit criterion**
- At least one full round-trip (entry + exit + settlement) confirmed working
- Position P&L reconciles with CLOB trade history

---

## Sequence

```
Stage 1 (offline replay)
  → confirm signal fires at right moments, orders sized correctly
Stage 2 (shadow mode)
  → confirm live data adapters work, no parsing errors, window transitions clean
Stage 3 ($5 real orders)
  → confirm CLOB submission, fill handling, position lifecycle end-to-end
  → then increase size
```

---

## WS Recordings

`data/ws_recordings/*.jsonl.gz` — compact gzip JSONL, one file per market window.

Used in Stage 1 as a reproducible Polymarket data source. See `docs/ws_book_recording_format.md` for format details.

Recorder: `historical/fetch/record_ws.py --slug-pattern btc-updown-5m`

Keep at least a few days of recordings before building the Stage 1 replay client, to have enough windows for meaningful testing.
