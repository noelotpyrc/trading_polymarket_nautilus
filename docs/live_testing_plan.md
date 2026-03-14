# Live Testing Plan

How to validate the live Nautilus process before deploying with meaningful capital.

Polymarket has no testnet. Validation must progress from sandboxed live-data runs to tightly controlled live order rehearsals.

For the detailed fix inventory and current committed vs local-only status, see [docs/live_hardening_status.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_hardening_status.md).

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
- Polymarket market-entry orders now use IOC, and known residual entry-order remainders are canceled when the related position is cleaned up
- Stage 7 side-aware Polymarket quote handling is implemented for the live strategies and data config
- Ended-window residual positions now carry to Polymarket resolution instead of forcing an immediate stop
- Daily restart with pre-loaded windows is the accepted operating model for now

What is **not** proven yet:
- Real Polymarket order submission and cancel behavior
- Real live fill handling and venue reconciliation
- Multi-hour stability under production-style supervision
- Log-volume reduction from one-sided Polymarket books under a fresh multi-hour soak run
- Resolution polling against live closed-market data
- Automated post-resolution settlement / redemption handling, which is intentionally deferred to a separate external process

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
- Shared PM late fills now trigger immediate flattening; if a known residual remains on an ended window, it carries to resolution instead of forcing a stop
- Strategy-managed process stop now waits for carried residuals to resolve before final node shutdown
- Explicit runtime-health transitions are logged for `btc_updown`, and PM guardrail escalations log the order or instrument involved

**Success criteria**
- New entries are blocked when feed freshness conditions are violated
- Unsafe state leads to clean stop or explicit degraded-mode behavior
- Tests prove stale-input conditions do not produce accidental orders
- Known old-window residuals remain tracked to resolution instead of causing unnecessary node stops
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
  - late fill -> flatten -> cleanup success / carry-to-resolution
  - warmup timeout -> stop

**Success criteria**
- Gap-contaminated Binance signal input cannot produce new entries until the gap is healed or ages out of the signal window
- A stuck pending entry order is canceled on schedule and escalates to stop if it never resolves
- A late fill creates immediate flatten behavior, and ended-window residuals move into resolution tracking instead of forcing a stop
- Warmup timeout causes a clean stop with the expected reason

---

## Stage 7 — Side-Aware Polymarket Quote Handling

Move from dropping one-sided Polymarket quotes to consuming them explicitly and safely.

**Purpose**
- Model the real Polymarket book state more faithfully instead of hiding one-sided markets
- Reduce `Dropping QuoteTick` log spam at the root rather than only suppressing it
- Make strategy entry/exit gating depend on the actual side-specific liquidity that exists

**What we will implement**
- Set `drop_quotes_missing_side=False` for the Polymarket data client
- Track quote prices and sizes explicitly in the live strategies
- Define side-aware tradability:
  - `buy_tradable`: fresh quote and positive ask size
  - `sell_tradable`: fresh quote and positive bid size
  - `two_sided`: both sides have positive size
- Only use midpoint pricing when the quote is genuinely two-sided

**Implementation status**
- Implemented in `live/config.py` and the shared `WindowedPolymarketStrategy`
- BUY entry decisions now require a fresh quote with positive ask size
- Active-window SELL / flatten decisions from strategy logic now require a fresh quote with positive bid size
- Heartbeat / signal logs now show side-specific quote state instead of assuming a usable midpoint
- Focused unit and integration coverage was updated for one-sided quote handling

**Success criteria**
- One-sided Polymarket books no longer generate overwhelming dropped-quote warnings
- BUY entry logic never treats a synthetic or zero-sized ask as executable liquidity
- SELL / flatten logic never treats a synthetic or zero-sized bid as executable liquidity
- Tests prove the strategy behaves correctly when the market transitions between one-sided and two-sided states

---

## Stage 8 — External Resolution Settlement / Redemption

Automate post-resolution settlement tracking and token redemption outside the Nautilus live trading node.

Detailed design and implementation plan:
- [docs/wallet_resolution_plan.md](/Users/noel/projects/trading_polymarket_nautilus/docs/wallet_resolution_plan.md)

**Purpose**
- Separate active trading concerns from post-resolution operational settlement
- Handle Polymarket resolution/redemption with a workflow that can inspect wallet-held YES/NO positions and collateral directly
- Avoid forcing the live trading node to own long-tail redemption logic

**What we will implement**
- A separate process, not built on Nautilus strategy/runtime primitives
- Resolution-state polling plus a node-facing wallet-truth interface
- Automated or operator-assisted redemption workflow for resolved YES/NO positions

**Success criteria**
- Resolved carried positions can be reconciled and redeemed without manual log spelunking
- The external process can confirm final wallet state after redemption
- The live trading node can stay focused on trading-window execution and residual tracking only

---

## Stage 9 — Longer Sandbox Soak Runs

Run the live process for hours, not minutes.

**Purpose**
- Prove stability over time instead of just correctness at startup
- Catch reconnect issues, timer drift, rollover accumulation, and noisy-feed edge cases
- Validate that the runner shape can survive normal session length

**What we will implement**
- Multi-hour sandbox runs for the intended runner profiles
- Log review for reconnects, rollover continuity, pending-order cleanup, and shutdown
- Tooling support exists in [live/soak.py](/Users/noel/projects/trading_polymarket_nautilus/live/soak.py) to run bounded profiles sequentially and persist logs plus summaries under `logs/soak/`

**Success criteria**
- Multi-hour sandbox runs complete without uncaught exceptions
- Quote subscriptions survive rollover repeatedly
- No stuck pending-entry state, runaway log storm, or degraded process behavior
- Shutdown remains clean after long runtimes

---

## Stage 10 — Live Order Lifecycle Rehearsal (No Intended Fill)

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

## Stage 11 — Minimum-Size Live Fill Rehearsal

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

## Stage 12 — Observability Tightening

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
  -> run deterministic guardrail fault-injection E2E scenarios
Stage 7
  -> make Polymarket quote handling side-aware and reduce dropped-quote log spam
Stage 8
  -> build external wallet-truth + resolution/redemption flow before live trading
Stage 9
  -> run multi-hour sandbox soak sessions on the production profiles
Stage 10
  -> submit a tiny non-marketable live limit order and cancel it
Stage 11
  -> execute one minimum-size live fill-and-flatten rehearsal
Stage 12
  -> tighten log retention and operator-facing observability
```

---

## WS Recordings

`data/ws_recordings/*.jsonl.gz` are used for **execution simulation during backtesting**, not for live infrastructure testing. See `docs/ws_book_recording_format.md` for format details.
