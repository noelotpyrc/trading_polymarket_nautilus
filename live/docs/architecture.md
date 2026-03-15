# Live Trading Architecture

## Overview

The live system runs a Nautilus `TradingNode` connected to two data sources:
- **Binance** — 1-minute BTC perpetual futures bars (signal input)
- **Polymarket** — quote ticks for the current window's selected outcome token (execution target)

Execution is routed through a Polymarket CLOB exec client (live) or a `SandboxExecutionClient` (paper trading).

---

## Directory Structure

```
live/
├── node.py                  # Shared infrastructure for live nodes
├── config.py                # TradingNodeConfig builders (live / sandbox)
├── market_metadata.py       # Shared YES/NO token metadata registry
├── resolution.py            # Polymarket market-resolution polling helpers
├── wallet_truth.py          # Production wallet-truth snapshot/provider
├── sandbox_wallet.py        # Synthetic sandbox wallet state and provider
├── resolution_worker.py     # External wallet-based resolution worker logic
├── redemption.py            # Production redemption backend
├── run_resolution.py        # External resolution worker CLI
├── soak.py                  # Sequential bounded soak runner with durable logs
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
| `python live/runs/profiles/btc_updown_15m_live_no.py` | Fixed live BTC momentum NO-outcome profile |
| `python live/runs/profiles/btc_updown_15m_sandbox.py` | Fixed warmup sandbox profile |
| `python live/runs/profiles/btc_updown_15m_sandbox_no.py` | Fixed warmup sandbox NO-outcome profile |
| `python live/runs/profiles/random_signal_15m_resolution_sandbox.py` | Fixed deterministic residual-carry sandbox profile |
| `python live/runs/profiles/random_signal_15m_sandbox.py` | Fixed fast sandbox profile |
| `python live/runs/profiles/random_signal_15m_sandbox_no.py` | Fixed fast sandbox NO-outcome profile |
| `python live/runs/btc_updown.py --slug-pattern btc-updown-15m --outcome-side yes` | BTC momentum, live orders |
| `python live/runs/btc_updown.py --slug-pattern btc-updown-15m --outcome-side no --sandbox` | BTC momentum, simulated NO-outcome orders |
| `python live/runs/random_signal.py --slug-pattern btc-updown-15m --outcome-side no --sandbox` | Random signal, sandbox NO-outcome (testing) |

Common flags:

| Flag | Description |
|------|-------------|
| `--slug-pattern` | Market slug prefix, e.g. `btc-updown-15m` |
| `--hours-ahead N` | Pre-load N hours of windows at startup (default: 4) |
| `--run-secs N` | Auto-stop after N seconds for bounded sandbox/manual runs |
| `--outcome-side {yes,no}` | Select the first or second Polymarket outcome token |
| `--sandbox` | Simulated execution — no real orders |
| `--sandbox-wallet-state-path PATH` | Share a synthetic wallet-state file with the external resolution worker |
| `--sandbox-starting-usdc N` | Override the sandbox starting USDC.e balance |
| `--binance-us` | Use Binance US endpoint (for US IPs) |

Fixed per-profile entrypoints intentionally keep the checked-in TOML file as the source of truth for market/feed/risk settings, while the generic profile runner handles bounded runtime and sandbox-balance overrides.

---

## Runner Profiles

Production-style deployment now uses checked-in profile files under [live/profiles/catalog](/Users/noel/projects/trading_polymarket_nautilus/live/profiles/catalog).

Each profile defines:
- strategy
- slug pattern
- hours ahead
- mode (`sandbox` or `live`)
- Binance feed route (`global` or `us`)
- outcome side (`yes` or `no`)
- optional bounded runtime
- strategy-specific config overrides

The generic profile runner can load a profile by name or path:

```bash
python live/runs/profile.py btc_updown_15m_live
python live/runs/profile.py btc_updown_15m_live --print-profile
python live/runs/profile.py btc_updown_15m_sandbox --hours-ahead 8 --run-secs 28800
```

Fixed wrapper scripts in `live/runs/profiles/` provide one stable command per intended process. This is the preferred operator surface.

---

## Shared Infrastructure (`node.py`)

### `resolve_upcoming_windows(slug_pattern, hours_ahead, outcome_side)`

Queries the Gamma API to find current + upcoming Polymarket windows matching the slug pattern. Returns an ordered list of `(pm_instrument_id, window_end_ns)` tuples for the selected outcome side.

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

Returns an `argparse.ArgumentParser` with the standard flags (`--slug-pattern`, `--hours-ahead`, `--run-secs`, `--outcome-side`, `--sandbox`, `--binance-us`). All run scripts use this to keep CLI consistent.

### `live.runs.common.run_strategy(...)`

Shared launcher that runs preflight, builds the node, instantiates the selected strategy/config pair, attaches the strategy, schedules bounded stop if requested, and starts the node.

Both ad hoc runners and profile-driven runners use this path.

### `prepare_run(...)`

Shared runner preflight. Validates mode-specific env vars, resolves windows, rejects duplicates/non-monotonic schedules, prints startup summary, and warns when the first window is close to expiry.

### `prepare_run_metadata(...)`

Shared preflight variant that returns rich window metadata, including:
- condition id
- YES token id
- NO token id
- selected outcome label
- selected instrument id

This metadata is now reused by both the trading node and the external resolution worker.

### `schedule_stop(stop_target, run_secs)`

Arms a timer that calls the provided stop target after `run_secs`. In live runners this is a strategy-managed stop request, so bounded runs still go through cancel/cleanup/resolution handling before the node shuts down.

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
| `warmup_days` | `0` | Historical Binance warmup window loaded at startup |
| `outcome_side` | `yes` | First (`yes`) or second (`no`) Polymarket outcome token |

**Signal logic:**

Requires `signal_lookback + 1` bars to fire. With `warmup_days > 0`, the strategy requests historical Binance bars at startup, buffers live bars while the request is in flight, then merges/dedupes them before allowing entries.
- `closes[-1] > closes[0]` → bullish → BUY selected outcome token
- `closes[-1] < closes[0]` → bearish → exit if in position
- Enters once per window; exits on bearish signal

**Data subscriptions:**
- `subscribe_bars(btc_bar_type)` — Binance 1m bars
- `subscribe_quote_ticks(pm_instrument_id)` — PM quote ticks for side-aware quote state

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
| `outcome_side` | `yes` | First (`yes`) or second (`no`) Polymarket outcome token |

**Signal logic:**

On each bar: draw `random()`. If above `entry_threshold` and no open position → BUY. If above `exit_threshold` and in position → close. First qualifying bar fires immediately (typically bar 1 or 2).

**Data subscriptions:** same as BtcUpDownStrategy — intentionally identical to test the same feeds.

### Side-Aware Quote Handling

The live strategies no longer assume that every fresh Polymarket quote is a usable two-sided market.

- the Polymarket data client now keeps one-sided books visible by allowing synthetic quotes for missing sides
- BUY entry logic requires a fresh quote with positive ask size
- active-window SELL / flatten logic requires a fresh quote with positive bid size
- midpoint pricing is only used when both sides have positive size

This reduces dropped-quote spam at the source and keeps strategy decisions aligned with the real side-specific liquidity that exists.

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

When the old window cannot be fully flattened after rollover:
- active cleanup uses Polymarket-compatible IOC exits
- if a known residual remains on the ended window, the strategy stops retrying futile exits and carries that residual to market resolution
- current-window trading can continue while the old residual is tracked separately

---

## Residual Resolution Handling

The live process now distinguishes between:
- unknown order / exposure state, which is still stop-worthy
- known old-window residual exposure, which is carried to resolution

The carry-to-resolution flow is:

1. an ended-window position cannot be fully flattened
2. the strategy records that instrument as a carried residual
3. [resolution.py](/Users/noel/projects/trading_polymarket_nautilus/live/resolution.py) polls Polymarket market metadata by condition id
4. once the market is closed and a winning token is available, the strategy records the residual as a `WIN` or `LOSS`
5. post-resolution settlement / redemption remains outside this node; it is deferred to a separate external process

If a process stop is requested while carried residuals still exist, final node shutdown waits until those residuals resolve.

---

## External Resolution Worker

Stage 8 introduces a separate wallet-based resolution flow outside the Nautilus node.

### Components

- `market_metadata.py`
  - builds the allowlisted YES/NO token universe from preloaded windows
- `wallet_truth.py`
  - production wallet-truth snapshot/provider backed by Polymarket APIs
- `sandbox_wallet.py`
  - synthetic sandbox wallet state and matching wallet-truth provider
- `resolution_worker.py`
  - groups wallet-held positions by condition and settles resolved conditions
- `redemption.py`
  - production redemption backend, dry-run by default
- `run_resolution.py`
  - operator-facing CLI for one-shot or looped resolution scans

### Flow

1. The runner resolves window metadata and the node loads the selected Polymarket instruments.
2. In sandbox mode, fills update a shared `wallet_state.json` through `SandboxWalletStore`.
3. The node polls a wallet-truth provider on a timer, updates its account-state view from wallet truth, and reconciles carried residuals when wallet truth proves they were externally settled.
4. The external resolution worker loads the same allowlisted metadata and reads wallet truth:
   - sandbox: `SandboxWalletTruthProvider`
   - live: `ProdWalletTruthProvider`
5. For resolved conditions:
   - sandbox executor applies synthetic settlement to `wallet_state.json`
   - production executor either reports `ready_to_redeem` or submits `redeemPositions(...)`
6. The next node wallet-truth poll sees the updated balance/held-token state.

### Current Scope

- implemented:
  - shared metadata registry
  - production wallet-truth provider
  - sandbox wallet store + provider
  - external worker CLI
  - production redemption backend behind the worker interface
  - node-side carried-residual reconciliation from wallet truth
  - internal resolution downgraded to advisory-only status
- not yet implemented:
  - full Nautilus account-state reconciliation from externally redeemed balance
  - end-to-end live redemption rehearsal

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
3. Run the deterministic residual-carry sandbox check when validating the external resolution worker:
   - `python live/runs/profiles/random_signal_15m_resolution_sandbox.py`
4. Run the NO-outcome fast sandbox check when validating side selection:
   - `python live/runs/profiles/random_signal_15m_sandbox_no.py`
5. Run the slower warmup-based sandbox check:
   - `python live/runs/profiles/btc_updown_15m_sandbox.py`
6. Treat window exhaustion as a normal stop condition for this phase. Restart the node for the next session or next day.
7. Daily restart is acceptable even if the first window after restart is missed.
8. Low free collateral should block new entries, but should not stop the node while an open position, cleanup, or carried residual still exists.

### Soak Harness

- [soak.py](/Users/noel/projects/trading_polymarket_nautilus/live/soak.py) is the operator tool for the longer multi-hour sandbox sessions after the side-aware Polymarket quote update lands.
- It launches one or more profiles sequentially through the existing profile runner, captures stdout/stderr, and stores artifacts in `logs/soak/<timestamp>[_label]/`.
- It can override `hours_ahead` for a longer soak without editing the checked-in profile itself.
- Default safety policy:
  - sandbox profiles only
  - bounded runtime required
- Each profile run gets:
  - `runner.log`
  - `profile.json`
  - `summary.json`
- Each batch gets its own top-level `summary.json`.

## Next Milestones

The live-process hardening roadmap lives in [docs/live_testing_plan.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_testing_plan.md). The next work after the current sandbox gate is:

1. PM order reconciliation
   - Purpose: reconcile stale IOC remainders against real PM order truth.
   - Design: [docs/order_reconciliation_plan.md](/Users/noel/projects/trading_polymarket_nautilus/docs/order_reconciliation_plan.md)
   - Success: stale IOC remainders are either canceled for real, externally proven dead, or escalated.
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
        outcome_side=args.outcome_side,
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
