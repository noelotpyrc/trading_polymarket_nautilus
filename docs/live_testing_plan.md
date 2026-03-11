# Live Testing Plan

How to validate the live Nautilus process before deploying with meaningful capital.

Polymarket has no testnet. Validation must progress from sandboxed live-data runs to tightly controlled live order rehearsals.

---

## Current Status

The current repo is through the sandbox gate:
- Live data feed smokes passed
- WS rollover behavior was verified
- Bounded sandbox runner validation passed
- Production-style runner profiles are implemented
- Binance historical warmup is implemented for the `btc_updown` runner via `warmup_days`
- Outcome-side selection is implemented for runner profiles and ad hoc runners
- Stage 5 health guards are implemented for Binance signal validity, warmup timeout, and PM order-lifecycle escalation
- Stage 6 guardrail fault-injection E2E coverage is implemented
- Daily restart with pre-loaded windows is the accepted operating model for now

What is **not** proven yet:
- Real Polymarket order submission and cancel behavior
- Real live fill handling and venue reconciliation
- End-to-end guardrail behavior under deterministic fault injection
- Multi-hour stability under production-style supervision

---

## Stage 1 — Sandbox Live Process

Connect to real live data feeds but replace Polymarket execution with Nautilus sandbox execution.

**Purpose**
- Prove the live process is correct without market risk
- Validate startup, subscriptions, signals, rollover, shutdown, and basic simulated order lifecycle
- Catch Nautilus integration issues before any live order is sent

**What it tests**
- Polymarket data adapter parsing real WS events
- Binance live kline stream -> signal pipeline latency
- Strategy signal computation and order trigger logic
- Instrument routing and window lifecycle
- Pending-order cleanup on rollover and stop

**What it does NOT test**
- Real CLOB order submission
- Real venue order state reconciliation
- Real fill semantics and venue-side balances

**How**
- Use bounded runners with `--run-secs`
- `--sandbox` swaps `PolymarketExecClientConfig` for `SandboxLiveExecClientFactory`
- Real `BinanceDataClientConfig` + `PolymarketDataClientConfig` stay unchanged

**Success criteria**
- Live automated tests are green
- Both live runner CLIs load cleanly with `--help`
- Sandbox runs finish without uncaught exceptions or broken quote subscriptions after rollover
- No runner gets stuck in a pending-entry state after reject, deny, cancel, or window exhaustion
- Window exhaustion produces a clean stop and explicit restart-needed log message

**Expected operational model**
- The node stops cleanly once pre-loaded windows are exhausted
- Daily restart is acceptable for this phase
- Missing the first window after restart is acceptable

---

## Stage 2 — Production Runner Profiles

Create deployment-ready runner profiles for real operating setups.

**Purpose**
- Turn test-oriented CLI usage into reproducible production process definitions
- Reduce operator error by replacing ad hoc flag combinations with fixed profiles
- Separate deployment wiring from strategy logic

**What we will implement**
- Checked-in runner profiles or profile-driven launch config for each intended prod process
- Stable settings for strategy, slug pattern, hours ahead, Binance route, risk knobs, and runtime policy
- Secrets remain in environment variables, not in profile files

**Success criteria**
- Each intended production process starts from one stable command or profile
- Operators do not need to manually assemble runtime flags
- Feed selection, market selection, and risk-relevant settings are explicit and versioned
- Sandbox and live mode differences remain deliberate and documented

**Current status**
- Implemented via checked-in TOML profiles in `live/profiles/catalog`
- Fixed entrypoints live in `live/runs/profiles`
- Generic loader/launcher lives in `live/runs/profile.py`

---

## Stage 3 — Binance Historical Warmup

Load historical Binance bars at live-strategy startup before relying on the live stream alone.

**Purpose**
- Remove the need to wait several live bars before the production strategy can compute signals
- Make live startup behavior match the intended production signal pipeline more closely
- Prove the Nautilus live node can mix historical Binance bootstrap with ongoing Binance live subscriptions

**Implementation status**
- Implemented for `BtcUpDownStrategy` with `warmup_days`
- Uses Nautilus live `request_bars(...)` at startup
- Buffers live Binance bars while the historical request is in flight, then merges/dedupes before enabling entries
- Covered by focused unit/profile tests and warmup benchmarks

**Success criteria**
- A live strategy receives the configured number of historical Binance bars before trading
- No entry is allowed before warmup is complete
- The first live signal after startup matches the expected signal from equivalent historical input
- Startup remains clean when Binance historical requests return no data or incomplete data

---

## Stage 4 — Outcome-Side Support (YES / NO)

Allow live runners to target the NO token as well as the YES token.

**Purpose**
- Remove the current YES-only restriction in the live process
- Support production strategies that want fixed-side deployment on either outcome
- Make outcome side an explicit deployment choice instead of a hard-coded assumption

**Implementation status**
- Implemented for checked-in runner profiles and ad hoc runners via `outcome_side`
- Gamma resolution now selects the first (`yes`) or second (`no`) outcome token per window
- Fixed NO profiles were added for sandbox and live operator commands
- Sandbox validation covered NO-side subscription, entry, exit, and rollover on a real live-data session

**Success criteria**
- A checked-in profile can start a live or sandbox process in `yes` mode or `no` mode
- The resolved instrument IDs match the selected outcome side for every pre-loaded window
- Sandbox validation proves the selected outcome side can subscribe, enter, exit, and roll windows correctly
- Docs and operator commands make the selected outcome side explicit

---

## Stage 5 — Health Guards / Fail-Safe Controls

Add health gating so the node stops trading when the process is alive but the inputs are not trustworthy.

Detailed runtime definitions and the action matrix live in [docs/live_health_guard_policy.md](live_health_guard_policy.md).

**Purpose**
- Prevent trading on stale or incomplete data
- Fail safe instead of silently running in a degraded state
- Make operational failure modes explicit in logs

**Implementation status**
- `btc_updown` now blocks signal-driven decisions on stale Binance bars and on Binance bar-series gaps until the gap ages out of the signal window
- `btc_updown` now stops cleanly when historical warmup times out or returns no historical bars
- Shared PM entry orders now cancel after the pending timeout and escalate to stop if they never resolve
- Shared PM late fills now trigger immediate flattening plus a cleanup timeout that stops the node if flatness is not restored
- Explicit runtime-health transitions are logged for `btc_updown`, and PM guardrail escalations log the order or instrument involved

**Success criteria**
- New entries are blocked when feed freshness conditions are violated
- Unsafe state leads to clean stop or explicit degraded-mode behavior
- Tests prove stale-input conditions do not produce accidental orders
- Operators can identify the failure cause from logs alone

---

## Stage 6 — Guardrail Fault-Injection E2E

Exercise the implemented safeguard paths end-to-end with deterministic synthetic failures instead of waiting for live data to hit rare edge cases.

**Purpose**
- Prove the guardrail policies behave correctly across the full runtime flow, not just inside unit-level helpers
- Validate that entry blocking, cancel escalation, flatten cleanup, and startup-stop behavior all hold when multiple components interact
- Close the confidence gap between Stage 5 logic and later long-running or live-order rehearsals

**Implementation status**
- Implemented in [tests/live/test_guardrails_e2e.py](tests/live/test_guardrails_e2e.py)
- Uses one BacktestEngine scenario for Binance gap block-and-recover
- Uses deterministic scenario harnesses for:
  - pending entry timeout -> cancel -> stop escalation
  - late fill -> flatten -> cleanup success/failure
  - warmup timeout -> stop

**Success criteria**
- Gap-contaminated Binance signal input cannot produce new entries until the gap is healed or ages out of the signal window
- A stuck pending entry order is canceled on schedule and escalates to stop if it never resolves
- A late fill creates immediate flatten behavior and stops if flatness is not restored
- Warmup timeout causes a clean stop with the expected reason

---

## Stage 7 — Longer Sandbox Soak Runs

Run the live process for hours, not minutes.

**Purpose**
- Prove stability over time instead of just correctness at startup
- Catch reconnect issues, timer drift, rollover accumulation, and noisy-feed edge cases
- Validate that the runner shape can survive normal session length

**What we will implement**
- Multi-hour sandbox runs for the intended runner profiles
- Log review for reconnects, rollover continuity, pending-order cleanup, and shutdown

**Success criteria**
- Multi-hour sandbox runs complete without uncaught exceptions
- Quote subscriptions survive rollover repeatedly
- No stuck pending-entry state, runaway log storm, or degraded process behavior
- Shutdown remains clean after long runtimes

---

## Stage 8 — Live Order Lifecycle Rehearsal (No Intended Fill)

Submit a tiny live order that is intended to rest, then cancel it.

**Purpose**
- Prove the live control plane before taking fill risk
- Validate live auth, order submission, open-order state, cancel, and cleanup
- Confirm Nautilus state matches venue state for a real live order

**What we will implement**
- Use one supervised live process
- Submit a very small non-marketable limit order on a healthy market
- Prefer `post_only=True` if supported by the order path
- Wait for open confirmation, then cancel quickly

**Success criteria**
- The live order is accepted by Polymarket
- Nautilus sees the order as open
- Cancel succeeds cleanly
- No fill occurs
- No residual open order or position remains afterward
- Venue state matches Nautilus state after cleanup

---

## Stage 9 — Minimum-Size Live Fill Rehearsal

Execute the smallest practical live position, then flatten it.

**Purpose**
- Prove the live execution path end-to-end, including fills
- Validate real account balances, fees, position lifecycle, and flatten behavior
- Close the gap that sandbox cannot prove

**What we will implement**
- Hard-cap live size to the minimum practical amount
- Run one supervised process with one position at a time
- Enter and exit a live position under strict exposure limits

**Risk controls**
- Hard cap: minimum practical notional only
- Only one live position open at a time
- Strategy-side max-notional guard before `submit_order`

**Success criteria**
- At least one full round trip completes live
- Entry, exit, and final position state all reconcile with Polymarket
- Fees and balances line up with venue history
- No unexplained divergence remains between Nautilus state and venue state

---

## Stage 10 — Observability Tightening

Make the system operable once multiple long-running nodes exist.

**Purpose**
- Make debugging and supervision practical
- Preserve enough context to answer what happened without replaying a session manually
- Support production operation instead of single-session experimentation

**What we will implement**
- Durable per-run log locations
- Clear lifecycle markers in logs
- Short operator runbook for restart, failure modes, and expected actions

**Success criteria**
- Every run produces durable logs with enough context to diagnose failures
- Operators can answer what happened, when, and what action is needed from logs alone
- Restart and recovery expectations are documented and consistent with actual behavior

---

## Recommended Sequence

```
Feed smokes
  -> confirm Binance bars, Polymarket quotes, and WS rollover behavior
Stage 1a (fast sandbox, 180s)
  -> python tests/live/smoke_binance_feed.py --secs 90
  -> python tests/live/smoke_polymarket_feed.py --secs 60
  -> python tests/live/explore_nautilus_ws.py --phase-secs 20
  -> python live/runs/random_signal.py --slug-pattern btc-updown-15m --hours-ahead 1 --sandbox --run-secs 180
Stage 1b (warmup sandbox, 600s)
  -> python live/runs/btc_updown.py --slug-pattern btc-updown-15m --hours-ahead 2 --sandbox --run-secs 600
Stage 2
  -> use fixed profile entrypoints for repeatable sandbox/live sessions
Stage 3
  -> validate the btc_updown warmup runner/profile path
Stage 4
  -> validate fixed YES/NO outcome-side profiles on live data
Stage 5
  -> add stale-feed / fail-safe controls
Stage 6
  -> run multi-hour sandbox soak sessions on the production profiles
Stage 7
  -> submit a tiny non-marketable live limit order and cancel it
Stage 8
  -> execute one minimum-size live fill-and-flatten rehearsal
Stage 9
  -> tighten log retention and operator-facing observability
```

---

## WS Recordings

`data/ws_recordings/*.jsonl.gz` are used for **execution simulation during backtesting**, not for live infrastructure testing. See `docs/ws_book_recording_format.md` for format details.
