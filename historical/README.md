# Historical Data

Scripts for fetching and processing Polymarket historical data. All scripts use public APIs — no authentication required.

---

## Pipeline Overview

```
fetch/gamma_events.py
  └─> events JSON
        └─> process/event_tags.py            (filter by tag)
        └─> fetch/market_price_history.py
              └─> price history JSON
                    └─> process/clean_price_history_15min_btcupdown.py
                          └─> analysis-ready CSV

backtest/run.py
  └─> loads Binance 1m CSV + fetches PM price history live
        └─> BacktestEngine (two venues: BINANCE signal + POLYMARKET execution)
              └─> P&L summary

fetch/record_ws.py
  └─> records live Polymarket WS book events
        └─> data/ws_recordings/<slug>.jsonl.gz (one file per window)
```

---

## fetch/

### `gamma_events.py`
Fetches Polymarket events from the Gamma API (`https://gamma-api.polymarket.com/events`) with pagination and date-window slicing.

```bash
python historical/fetch/gamma_events.py \
  --start-date 2024-01-01 --end-date 2024-12-31 \
  --closed true --volume-min 10000 \
  --order volume --descending \
  --output-dir ./data --format json
```

Key options:
| Flag | Description |
|---|---|
| `--start-date`, `--end-date` | Date window (YYYY-MM-DD) |
| `--slice-by` | Slice by `start` or `end` date (default: `start`) |
| `--chunk-days` | Days per API slice to avoid pagination limits (default: 14) |
| `--closed`, `--active`, `--archived` | Filter by market status |
| `--volume-min`, `--volume-max` | Filter by volume |
| `--tag`, `--tag-id`, `--tag-slug` | Filter by tag |
| `--order` | Sort key (e.g. `volume`) |
| `--format` | Output format: `json`, `csv`, `parquet` |
| `--output-dir` | Output directory |

---

### `market_price_history.py`
Fetches per-minute price history for all markets in an events JSON file using the NautilusTrader Polymarket adapter. Parses timestamps from market slugs (e.g. `btc-updown-15m-1770594300`) to determine fetch windows.

```bash
python historical/fetch/market_price_history.py \
  --input data/bitcoin_up_or_down_15m.json \
  --output data/btc_15m_price_history.json \
  --buffer-before 1 --buffer-after-min 10 \
  --fidelity 1
```

Key options:
| Flag | Description |
|---|---|
| `--input` | Events JSON file (output of `gamma_events.py`) |
| `--output` | Output JSON file |
| `--buffer-before` | Hours before market start to fetch (default: 1) |
| `--buffer-after-min` | Minutes after market end to fetch (default: 10) |
| `--fidelity` | Price resolution in minutes (default: 1) |
| `--max-markets` | Limit number of markets (useful for testing) |
| `--start-date` | Only fetch markets on or after this date |
| `--delay` | Seconds between requests (default: 0.1) |

---

### `markets.py`
Fetches closed/archived markets from the Polymarket CLOB API via NautilusTrader and saves them to `closed_markets.json`. Useful for exploring available market metadata.

```bash
python historical/fetch/markets.py
```

---

## process/

### `clean_price_history_15min_btcupdown.py`
Cleans raw price history JSON into an analysis-ready CSV. Filters to markets with exactly 15 in-window data points (one per minute for 15m markets) and assigns minute indices.

```bash
python historical/process/clean_price_history_15min_btcupdown.py \
  data/btc_15m_price_history.json \
  data/btc_15m_clean.csv
```

Output columns: `market_timestamp`, `datetime`, `minute` (0–14), `t`, `snapshot_datetime`, `p`

---

### `event_tags.py`
Utility for exploring and filtering events by tag.

```bash
# List all tags sorted by event count
python historical/process/event_tags.py list-tags \
  --input data/gamma_events.json

# Search tags by keyword
python historical/process/event_tags.py search-tags \
  --input data/gamma_events.json --keyword crypto

# Filter events by tag and save
python historical/process/event_tags.py filter \
  --input data/gamma_events.json --tags crypto,bitcoin \
  --output data/crypto_events.json
```

---

## backtest/

### `run.py`
End-to-end Nautilus backtest combining Binance 1m signal data with Polymarket YES token price history. Fetches PM price history live from the CLOB on each run.

```bash
python historical/backtest/run.py \
  --binance-csv "/Volumes/Extreme SSD/trading_data/cex/ohlvc/binance_btcusdt_perp_1m/BTCUSDT-1m-merged.csv" \
  --market-slug btc-updown-15m-1770594300 \
  --start 2025-09-01 --end 2025-10-01
```

| Flag | Description |
|---|---|
| `--binance-csv` | Path to merged Binance 1m OHLCV CSV |
| `--market-slug` | Polymarket market slug (must include timestamp suffix) |
| `--start`, `--end` | Date range (YYYY-MM-DD) |
| `--capital` | Starting USDC balance (default: 500.0) |

---

## fetch/ (additional)

### `record_ws.py`
Records live Polymarket WebSocket book snapshots to gzip-compressed JSONL. Runs continuously, one file per market window.

```bash
python historical/fetch/record_ws.py --slug-pattern btc-updown-15m
```

Output: `data/ws_recordings/<slug>.jsonl.gz` — see `docs/ws_book_recording_format.md` for format.

---

## explore/

### `nautilus_loader_test.py`
One-off script for testing the NautilusTrader `PolymarketDataLoader` with a single market slug. Not part of the main pipeline.

```bash
python historical/explore/nautilus_loader_test.py
```
