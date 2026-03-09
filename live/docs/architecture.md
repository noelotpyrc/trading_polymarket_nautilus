# Live Trading Architecture

## Overview

The live system runs a Nautilus `TradingNode` connected to two data sources:
- **Binance** — 1-minute BTC perpetual futures bars (signal input)
- **Polymarket** — quote ticks for the current window's YES token (execution target)

Execution is routed through a Polymarket CLOB exec client (live) or a `SandboxExecutionClient` (paper trading).

---

## Directory Structure

```
live/
├── node.py                  # Shared infrastructure for live nodes
├── config.py                # TradingNodeConfig builders (live / sandbox)
├── profiles/
│   └── catalog/             # Checked-in runner profile TOML files
├── runs/
│   ├── btc_updown.py        # Infrastructure test runner (warmup-based)
│   ├── profile.py           # Generic profile runner
│   ├── profiles/            # Fixed per-profile entrypoints
│   └── random_signal.py     # Infrastructure test runner (fast exercise)
├── strategies/
│   ├── btc_updown.py        # Infrastructure test strategy logic
│   └── random_signal.py     # Infrastructure test strategy logic
└── docs/
    ├── setup_and_first_trade.md
    └── architecture.md      # this file
```

---

## Entry Points

Run scripts are the entry points. `node.py` exposes shared infrastructure that the runner scripts import directly; strategy modules only contain strategy logic.

| Command | Description |
|---------|-------------|
| `python live/runs/profile.py --list` | List checked-in runner profiles |
| `python live/runs/profiles/btc_updown_15m_live.py` | Fixed live BTC momentum profile |
| `python live/runs/profiles/btc_updown_15m_sandbox.py` | Fixed warmup sandbox profile |
| `python live/runs/profiles/random_signal_15m_sandbox.py` | Fixed fast sandbox profile |
| `python live/runs/btc_updown.py --slug-pattern btc-updown-15m` | BTC momentum, live orders |
| `python live/runs/btc_updown.py --slug-pattern btc-updown-15m --sandbox` | BTC momentum, simulated orders |
| `python live/runs/random_signal.py --slug-pattern btc-updown-15m --sandbox` | Random signal, sandbox (testing) |

Common flags:

| Flag | Description |
|------|-------------|
| `--slug-pattern` | Market slug prefix, e.g. `btc-updown-15m` |
| `--hours-ahead N` | Pre-load N hours of windows at startup (default: 4) |
| `--run-secs N` | Auto-stop after N seconds for bounded sandbox/manual runs |
| `--sandbox` | Simulated execution — no real orders |
| `--binance-us` | Use Binance US endpoint (for US IPs) |

Fixed per-profile entrypoints intentionally do not expose the full ad hoc flag surface. The checked-in TOML file is the source of truth for market/feed/risk settings, with `--run-secs` as the only supported runtime override.

---

## Runner Profiles

Production-style deployment now uses checked-in profile files under [live/profiles/catalog](/Users/noel/projects/trading_polymarket_nautilus/live/profiles/catalog).

Each profile defines:
- strategy
- slug pattern
- hours ahead
- mode (`sandbox` or `live`)
- Binance feed route (`global` or `us`)
- optional bounded runtime
- strategy-specific config overrides

The generic profile runner can load a profile by name or path:

```bash
python live/runs/profile.py btc_updown_15m_live
python live/runs/profile.py btc_updown_15m_live --print-profile
```

Fixed wrapper scripts in `live/runs/profiles/` provide one stable command per intended process. This is the preferred operator surface.

---

## Shared Infrastructure (`node.py`)

### `resolve_upcoming_windows(slug_pattern, hours_ahead)`

Queries the Gamma API to find current + upcoming Polymarket windows matching the slug pattern. Returns an ordered list of `(pm_instrument_id, window_end_ns)` tuples.

- One API call per window (~17 calls for 4h of 15m windows)
- Instrument ID format: `{condition_id}-{token_id}.POLYMARKET`
- Window end time in nanoseconds (Nautilus clock format)

### `build_node(pm_instrument_ids, sandbox, binance_us)`

Builds and returns a `TradingNode` with data and exec clients attached. Strategy run scripts add the strategy, then call `node.build()` / `node.run()`.

Clients registered:
- `BINANCE` — `BinanceLiveDataClientFactory`
- `POLYMARKET` — `PolymarketLiveDataClientFactory`
- `POLYMARKET` exec — `SandboxLiveExecClientFactory` (sandbox) or `PolymarketLiveExecClientFactory` (live)

### `make_arg_parser(description)`

Returns an `argparse.ArgumentParser` with the standard flags (`--slug-pattern`, `--hours-ahead`, `--run-secs`, `--sandbox`, `--binance-us`). All run scripts use this to keep CLI consistent.

### `live.runs.common.run_strategy(...)`

Shared launcher that runs preflight, builds the node, instantiates the selected strategy/config pair, attaches the strategy, schedules bounded stop if requested, and starts the node.

Both ad hoc runners and profile-driven runners use this path.

### `prepare_run(...)`

Shared runner preflight. Validates mode-specific env vars, resolves windows, rejects duplicates/non-monotonic schedules, prints startup summary, and warns when the first window is close to expiry.

### `schedule_stop(node, run_secs)`

Arms a timer that calls `node.stop()` after `run_secs`. Used for bounded sandbox/manual validation sessions.

---

## Strategies

### BtcUpDownStrategy (`btc_updown.py`)

Infrastructure test strategy. Signal based on BTC 1-minute bar momentum.

**Config:**

| Field | Default | Description |
|-------|---------|-------------|
| `btc_bar_type` | `BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL` | Bar type for signal |
| `signal_lookback` | `5` | Number of bars for momentum window |
| `trade_amount_usdc` | `5.0` | Order size in USDC |

**Signal logic:**

Requires `signal_lookback + 1` bars to fire (~6 minutes warmup on 1m bars).
- `closes[-1] > closes[0]` → bullish → BUY YES
- `closes[-1] < closes[0]` → bearish → exit if in position
- Enters once per window; exits on bearish signal

**Data subscriptions:**
- `subscribe_bars(btc_bar_type)` — Binance 1m bars
- `subscribe_quote_ticks(pm_instrument_id)` — PM quote ticks for mid price

---

### RandomSignalStrategy (`random_signal.py`)

Infrastructure test strategy. Fires a random signal on each Binance bar — no warmup required.

**Purpose:** Exercises the full stack (Binance feed, PM feed, strategy, exec, window roll) within 1-2 minutes instead of 5-6 minutes.

**Config:**

| Field | Default | Description |
|-------|---------|-------------|
| `btc_bar_type` | `BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL` | Bar type (same as production — tests Binance feed) |
| `entry_threshold` | `0.5` | Enter if `random() > threshold` (~50% hit rate per bar) |
| `exit_threshold` | `0.7` | Exit if `random() > threshold` (~30% hit rate per bar) |
| `trade_amount_usdc` | `5.0` | Order size in USDC |

**Signal logic:**

On each bar: draw `random()`. If above `entry_threshold` and no open position → BUY. If above `exit_threshold` and in position → close. First qualifying bar fires immediately (typically bar 1 or 2).

**Data subscriptions:** same as BtcUpDownStrategy — intentionally identical to test the same feeds.

These strategies exist to validate the live process, not to represent the eventual production trading logic.

---

## Window Roll

Both strategies share the same window roll pattern. When a window expires:

1. Save `old_instrument_id`
2. Advance `_window_idx`, update `_pm_instrument_id` and `_window_end_ns`
3. **`subscribe_quote_ticks(NEW)`** — subscribe new instrument first
4. **`unsubscribe_quote_ticks(OLD)`** — unsubscribe old instrument second
5. Set next window time alert

**Order is critical.** The Polymarket WS client disconnects when subscription count drops to zero. Subscribing NEW before unsubscribing OLD keeps the count above zero, preserving the live connection and ensuring NEW receives a dynamic subscribe on the active socket.

Reversed order (unsubscribe first) causes the WS to disconnect before the new subscribe is sent — the new instrument never receives quote ticks. This was the root cause of `PM mid=n/a` observed in sandbox testing (confirmed by `tests/live/explore_nautilus_ws.py`).

---

## Config (`config.py`)

| Builder | Credentials | Exec client |
|---------|-------------|-------------|
| `binance_data_config` | None | `BinanceDataClientConfig` |
| `polymarket_data_config` | Production or test env vars, depending on `sandbox` | `PolymarketDataClientConfig` |
| `polymarket_exec_config` | Production env vars (`PRIVATE_KEY`, `WALLET_ADDRESS`, `POLYMARKET_API_*`) | `PolymarketExecClientConfig` |
| `sandbox_exec_config` | None | `SandboxExecutionClientConfig` (500 USDC.e starting balance) |

Sandbox mode disables reconciliation (`LiveExecEngineConfig(reconciliation=False)`) to avoid a startup crash from the sandbox exec client returning empty account reports.

---

## Operator Runbook

1. Run the feed smoke tests first:
   - `python tests/live/smoke_binance_feed.py --secs 90`
   - `python tests/live/smoke_polymarket_feed.py --secs 60`
   - `python tests/live/explore_nautilus_ws.py --phase-secs 20`
2. Run the fast bounded sandbox check:
   - `python live/runs/profiles/random_signal_15m_sandbox.py`
3. Run the slower warmup-based sandbox check:
   - `python live/runs/profiles/btc_updown_15m_sandbox.py`
4. Treat window exhaustion as a normal stop condition for this phase. Restart the node for the next session or next day.
5. Daily restart is acceptable even if the first window after restart is missed.

## Next Milestones

The live-process hardening roadmap lives in [docs/live_testing_plan.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_testing_plan.md). The next work after the current sandbox gate is:

1. Health guards / fail-safe controls
   - Purpose: stop or block trading when feeds are stale or process state is unsafe.
   - Success: stale or degraded inputs do not produce accidental orders.
2. Longer sandbox soak runs
   - Purpose: prove multi-hour stability instead of startup correctness only.
   - Success: repeated rollovers and long runtimes finish cleanly.
3. Live order lifecycle rehearsal
   - Purpose: prove real submit/open/cancel behavior without intended fill risk.
   - Success: a tiny non-marketable live limit order opens, cancels, and leaves no residue.
4. Minimum-size live fill rehearsal
   - Purpose: prove real live fills and venue reconciliation end-to-end.
   - Success: one minimum-size live round trip reconciles cleanly.
5. Observability tightening
   - Purpose: make long-running live processes operable.
   - Success: operators can diagnose failures from logs and runbook alone.

---

## Adding a New Strategy

1. Create `live/strategies/your_strategy.py`
2. Define `YourStrategyConfig(StrategyConfig)` and `YourStrategy(Strategy)`
3. Implement window roll using the **subscribe NEW before unsubscribe OLD** pattern
4. Add an ad hoc run script in `live/runs/your_strategy.py`:

```python
if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from live.node import build_node, make_arg_parser, prepare_run, schedule_stop

    parser = make_arg_parser("Your strategy description")
    args = parser.parse_args()

    windows = prepare_run(
        slug_pattern=args.slug_pattern,
        hours_ahead=args.hours_ahead,
        sandbox=args.sandbox,
        binance_us=args.binance_us,
        run_secs=args.run_secs,
    )
    pm_ids = [w[0] for w in windows]
    end_times = [w[1] for w in windows]

    node = build_node(pm_ids, sandbox=args.sandbox, binance_us=args.binance_us)
    node.trader.add_strategy(YourStrategy(YourStrategyConfig(
        pm_instrument_ids=tuple(pm_ids),
        window_end_times_ns=tuple(end_times),
    )))
    node.build()
    schedule_stop(node, args.run_secs)
    node.run()
```

5. Add a checked-in profile file in `live/profiles/catalog/your_profile.toml` with the market/feed/runtime choices you want operators to use.
6. Add an optional fixed wrapper in `live/runs/profiles/your_profile.py` if this should become a stable operator command.
7. Run ad hoc with `python live/runs/your_strategy.py --slug-pattern btc-updown-15m --sandbox`, or run the fixed profile entrypoint once it exists.
