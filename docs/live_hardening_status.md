# Live Hardening Status

Single reference for what has been fixed in the live Nautilus process, what is already committed, what is still only local, and what has been validated.

This exists because the roadmap in [docs/live_testing_plan.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_testing_plan.md) is about planned stages, while this file is about the actual fix inventory and current state of the codebase.

---

## Current Snapshot

- Last committed live hardening commit on `master`: `8b0a93d` (`Add live process guardrails and E2E tests`)
- There is a larger local-only live hardening batch after that commit
- Current local automated validation:
  - `.venv/bin/python -m pytest tests/live`
  - result: `119 passed`
- Still not re-proven after the very latest local quote-handling changes:
  - fresh multi-hour sandbox soak results
  - measured log-volume reduction on real live-data sessions

---

## Committed Milestones

### `6e264d1` — Harden live Nautilus process

Fixed / added:
- aligned the live code with the newer Nautilus Polymarket API
- split runnable live entrypoints into dedicated run scripts
- fixed Polymarket quote-quantity handling for BUY orders
- corrected stale/outdated live docs and smoke/manual scripts

Validated by:
- focused live tests
- `--help` validation on runner / smoke scripts

### `0bac2a0` — Add production runner profiles

Fixed / added:
- checked-in TOML runner profiles
- generic profile launcher
- fixed per-profile entrypoints
- shared runner launcher path

Validated by:
- focused profile / runner tests
- CLI `--list`, `--print-profile`, and `--help`

### `83105d3` — Add Binance live warmup for btc strategy

Fixed / added:
- live historical warmup via Nautilus `request_bars(...)` for `btc_updown`
- startup buffer/merge handoff from historical data to live bars
- warmup/handoff benchmarks and regression tests

Validated by:
- focused warmup tests
- live warmup benchmark / handoff check

### `daa56ac` — Add outcome-side live runner support

Fixed / added:
- YES / NO side selection in runners and profiles
- Gamma token selection by chosen outcome side
- NO-side profile and wrapper coverage

Validated by:
- focused tests
- real sandbox NO-side validation run

### `8b0a93d` — Add live process guardrails and E2E tests

Fixed / added:
- stale/gap Binance signal guards
- PM entry timeout / cancel / escalation
- warmup timeout handling
- deterministic guardrail fault-injection E2E tests

Validated by:
- full live suite at that point

---

## Local-Only Hardening Batch

Everything in this section exists locally after `8b0a93d`, but is not committed yet.

### 1. Process stop after the last preloaded window

Problem:
- the strategy could stop on window exhaustion while the Nautilus node kept idling until `--run-secs` fired

Fix:
- process stop is now strategy-managed
- exhaustion requests a full node stop, not just a strategy stop

Main files:
- [live/node.py](/Users/noel/projects/trading_polymarket_nautilus/live/node.py)
- [live/runs/common.py](/Users/noel/projects/trading_polymarket_nautilus/live/runs/common.py)
- [live/strategies/windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)

Validation:
- tests in [tests/live/test_node.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_node.py) and [tests/live/test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)
- real sandbox rerun confirmed prompt node stop after exhaustion

### 2. Live-compatible Polymarket exits

Problem:
- generic `close_position()` defaults were not appropriate for Polymarket live behavior
- shutdown / cleanup could depend on unsupported order settings

Fix:
- shared exit path now submits Polymarket-compatible market exits
- `IOC`
- `reduce_only=False`
- `quote_quantity=False`

Main file:
- [live/strategies/windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)

Validation:
- regression tests in [tests/live/test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)

### 3. Known residual ended-window positions carried to resolution

Problem:
- known old-window residual YES/NO positions were being treated too aggressively as stop-worthy even when the real issue was just disappearing liquidity

Fix:
- known ended-window residuals are now tracked and carried to resolution instead of forcing an unnecessary node stop
- known below-min residuals from partial fills are also now carried to resolution instead of stopping the node during the active window
- strict stop behavior is retained for unknown order / exposure state

Main files:
- [live/strategies/windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)
- [live/resolution.py](/Users/noel/projects/trading_polymarket_nautilus/live/resolution.py)

Validation:
- tests in [tests/live/test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)
- tests in [tests/live/test_guardrails_e2e.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_guardrails_e2e.py)
- tests in [tests/live/test_resolution.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_resolution.py)

Important scope note:
- automated post-resolution settlement / redemption is still out of scope here
- that is planned later in a separate external process, not inside Nautilus

### 4. Timer callback compatibility fix

Problem:
- a real sandbox soak exposed that the strategy assumed `TimeEvent.to_str()`, but real Nautilus `TimeEvent` objects use `.name`

Fix:
- guard timers now resolve using real Nautilus event naming

Main file:
- [live/strategies/windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)

Validation:
- updated tests in [tests/live/test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py) and [tests/live/test_guardrails_e2e.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_guardrails_e2e.py)
- short real sandbox rerun of `random_signal` confirmed the exhaustion path no longer crashes

### 5. Soak harness

Problem:
- longer sandbox sessions needed durable logs and summaries instead of ad hoc terminal output

Fix:
- added a bounded soak runner that persists logs, profile snapshots, and summaries

Main files:
- [live/soak.py](/Users/noel/projects/trading_polymarket_nautilus/live/soak.py)
- [tests/live/test_soak.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_soak.py)

Validation:
- soak harness tests

### 6. Residual entry-order cleanup

Problem:
- a partially filled opening BUY could leave a non-terminal entry order behind even after the resulting position went flat
- that is a real order-lifecycle issue, even if it is not residual position exposure

Fix:
- entry market orders now use `IOC`
- tracked entry orders are retained by client order id
- when the related instrument is cleaned up or its position closes, any still-open tracked entry remainder is explicitly canceled

Main file:
- [live/strategies/windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)

Validation:
- new regression coverage in [tests/live/test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)

### 7. Side-aware Polymarket quote handling

Problem:
- one-sided Polymarket books were being dropped by the adapter, producing overwhelming log noise
- strategy logic also treated “fresh quote exists” too simplistically

Fix:
- Polymarket data config now sets `drop_quotes_missing_side=False`
- one-sided books remain visible as synthetic quotes
- strategy quote handling is now side-aware:
  - BUY entry requires a fresh ask with positive size
  - active-window SELL / flatten logic requires a fresh bid with positive size
  - midpoint is only used when both sides have positive size

Main files:
- [live/config.py](/Users/noel/projects/trading_polymarket_nautilus/live/config.py)
- [live/strategies/windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)
- [live/strategies/btc_updown.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/btc_updown.py)
- [live/strategies/random_signal.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/random_signal.py)

Validation:
- tests in [tests/live/test_config.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_config.py)
- tests in [tests/live/test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)
- tests in [tests/live/test_guardrails_e2e.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_guardrails_e2e.py)
- current full local live suite: `119 passed`

Remaining proof needed:
- fresh real sandbox soak to measure the actual warning-volume drop and confirm there is no new long-run regression

---

## What Is Fully Proven vs Still Pending

### Proven locally

- live tests: `.venv/bin/python -m pytest tests/live` -> `119 passed`
- runner/profile infrastructure
- Binance warmup path
- YES / NO side selection
- guardrails and deterministic E2E scenarios
- process stop on last window
- residual position carry-to-resolution logic
- residual entry-order cleanup logic
- side-aware quote handling at unit / integration / E2E level

### Not yet reproven on fresh long real sandbox runs

- multi-hour soak behavior after:
  - residual entry-order cleanup
  - side-aware quote handling

### Not yet proven live

- real submit / open / cancel behavior on Polymarket
- real fill / reconciliation behavior
- live closed-market resolution polling

### Explicitly deferred

- automated settlement / redemption after market resolution
- this is planned as a separate external process, not a Nautilus live-node responsibility

---

## Recommended Immediate Next Step

Before any live order rehearsal, rerun the multi-hour sandbox soaks against the current local batch:

- `python live/soak.py random_signal_15m_sandbox --run-secs 14400 --label stage8_random_4h`
- `python live/soak.py btc_updown_15m_sandbox --run-secs 14400 --label stage8_btc_4h`

Review for:
- prompt node stop after exhaustion
- no residual partially-filled entry-order warning at shutdown
- lower one-sided-book warning volume
- no new regressions in rollover or cleanup

If those are clean, this whole local-only batch is ready to commit together.
