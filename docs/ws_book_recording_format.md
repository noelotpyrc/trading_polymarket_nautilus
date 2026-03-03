# WS Book Recording Format

Recorded by `historical/fetch/record_ws.py`.

## File

```
data/ws_recordings/<slug>.jsonl.gz
```

- One file per market window (e.g. `btc-updown-15m-1772550900.jsonl.gz`)
- gzip-compressed JSONL — each line is a JSON object
- ~1 MB per 15-minute window (~12,000 book snapshots)

## Line 0 — Metadata

Written once when the file is first created.

```json
{
  "meta": {
    "slug":         "btc-updown-15m-1772550900",
    "condition_id": "0xa144...72f06",
    "assets":       ["<token_id_0>", "<token_id_1>"]
  }
}
```

| Field        | Description                                      |
|--------------|--------------------------------------------------|
| `slug`       | Market slug (encodes pattern + window start UTC) |
| `condition_id` | Polymarket condition ID (hex)                  |
| `assets`     | Ordered list of token IDs; index matches `a` field in book lines |

`assets[0]` is typically the YES token, `assets[1]` the NO token, but use the metadata to confirm — do not hard-code.

## Line 1+ — Book Snapshot

One line per trade, emitted for **each token** (so two lines per trade event — one for each side of the binary market).

```json
{
  "t": 1772550934529,
  "a": 0,
  "b": [[0.01, 19636.37], [0.02, 10000.03], ..., [0.59, 169.84]],
  "s": [[0.99, 19724.88], [0.98, 9039.72],  ..., [0.60, 356.50]]
}
```

| Field | Type            | Description                                              |
|-------|-----------------|----------------------------------------------------------|
| `t`   | int (ms)        | Server-side timestamp in Unix milliseconds               |
| `a`   | int (0 or 1)    | Asset index — maps to `meta.assets[a]`                   |
| `b`   | `[[float, float], ...]` | Bids as `[price, size]` pairs, ascending price  |
| `s`   | `[[float, float], ...]` | Asks as `[price, size]` pairs, descending price |

- Prices are multiples of `0.01` (tick size), ranging from `0.01` to `0.99`
- Sizes are in shares (USDC-equivalent units)
- `b` and `s` each have up to ~50 levels (full book depth)
- Best bid = `b[-1][0]`, best ask = `s[-1][0]`
- Mid price = `(b[-1][0] + s[-1][0]) / 2`

## Reading the File

```python
import gzip, json

path = "data/ws_recordings/btc-updown-15m-1772550900.jsonl.gz"

with gzip.open(path, "rt") as f:
    meta   = json.loads(f.readline())["meta"]
    assets = meta["assets"]   # assets[0], assets[1]

    for line in f:
        r        = json.loads(line)
        token_id = assets[r["a"]]
        best_bid = r["b"][-1][0]
        best_ask = r["s"][-1][0]
        mid      = (best_bid + best_ask) / 2
```

## Trigger Frequency

From the Polymarket WS docs:
> `book` — Emitted when first subscribed to a market and when there is a trade that affects the book.

So each trade fires two book lines (one per token). Observed rate: ~7 trades/s → ~14 book lines/s during active 15-minute windows.
