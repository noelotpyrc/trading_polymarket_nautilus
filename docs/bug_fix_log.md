# Bug Fix Log

Purpose: keep one durable reference for the recurring bug families we actually hit during the live-hardening and strategy dev cycle.

This file is different from:
- [live_hardening_status.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_hardening_status.md)
  - milestone/status inventory
- [live_testing_plan.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_testing_plan.md)
  - validation roadmap

This log is for the bugs that kept recurring under different surfaces, and the invariants we decided on to stop re-learning the same lesson.

This is the canonical bug-fix log for future development. If an issue is worth remembering, it belongs here.

---

## How To Use This Log

When a new bug appears, check:
- have we already seen this failure class?
- what invariant did we decide on?
- what test or runtime proof should be reused before patching again?

Each entry records:
- symptom
- root cause
- fix pattern
- proof
- current rule

---

## BF-01 — Window Exhaustion Did Not Stop the Process

- Symptom:
  - the strategy exhausted its preloaded windows and stopped, but the Nautilus node kept idling until `--run-secs` fired
- Root cause:
  - strategy stop and process stop were treated as separate concerns
- Fix pattern:
  - exhaustion must request a full node/process stop, not just a strategy stop
- Main files:
  - [node.py](/Users/noel/projects/trading_polymarket_nautilus/live/node.py)
  - [common.py](/Users/noel/projects/trading_polymarket_nautilus/live/runs/common.py)
  - [windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)
- Proof:
  - [test_node.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_node.py)
  - [test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)
  - bounded soak reruns
- Current rule:
  - window exhaustion is a process-level terminal condition

## BF-02 — Real Nautilus Timer Events Did Not Match the Assumed API

- Symptom:
  - guard/timer callbacks crashed because code assumed a synthetic event API
- Root cause:
  - the runtime assumed `TimeEvent.to_str()`, but real Nautilus timer events use `.name`
- Fix pattern:
  - bind timer logic to the real Nautilus event API, not test doubles
- Main files:
  - [windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)
- Proof:
  - [test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)
  - [test_guardrails_e2e.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_guardrails_e2e.py)
- Current rule:
  - never assume the production event API from synthetic tests alone

## BF-03 — One-Sided PM Books Were Hidden and Produced Bad Trading Assumptions

- Symptom:
  - overwhelming `Dropping QuoteTick` noise
  - midpoint-based logic treated one-sided books as if they were safely tradable
- Root cause:
  - one-sided quotes were being dropped, and strategy logic used generic quote freshness instead of side-specific tradability
- Fix pattern:
  - keep one-sided quotes visible
  - require side-aware execution checks:
    - BUY entry needs a fresh ask with positive size
    - SELL/flatten needs a fresh bid with positive size
    - midpoint only when both sides are truly present
- Main files:
  - [config.py](/Users/noel/projects/trading_polymarket_nautilus/live/config.py)
  - [windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)
  - [btc_updown.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/btc_updown.py)
  - [random_signal.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/random_signal.py)
- Proof:
  - [test_config.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_config.py)
  - [test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)
  - [test_guardrails_e2e.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_guardrails_e2e.py)
- Current rule:
  - quote freshness and side tradability are separate checks

## BF-04 — Known Residual Positions Were Treated Too Aggressively

- Symptom:
  - ended-window or below-min residual YES/NO positions could force an unnecessary stop
- Root cause:
  - the runtime treated all leftover exposure as fatal instead of distinguishing known carryable residuals from unknown exposure state
- Fix pattern:
  - carry known residuals to resolution
  - keep strict stop behavior only for unknown state
- Main files:
  - [windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)
  - [resolution.py](/Users/noel/projects/trading_polymarket_nautilus/live/resolution.py)
- Proof:
  - [test_windowed_strategy.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_windowed_strategy.py)
  - [test_guardrails_e2e.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_guardrails_e2e.py)
  - [test_resolution.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_resolution.py)
- Current rule:
  - known carryable residuals go to resolution, not to a panic stop

## BF-05 — Wallet Truth and Order Truth Solved Different Problems

- Symptom:
  - wallet truth could prove held YES/NO balances, but partially filled IOC remainders could still linger as ambiguous cached orders
- Root cause:
  - wallet truth answers “what do we hold?”
  - order truth answers “is this order remainder still live?”
- Fix pattern:
  - keep wallet truth and order truth as separate interfaces
  - reconcile suspicious IOC remainders against PM/CLOB order truth
- Main files:
  - [wallet_truth.py](/Users/noel/projects/trading_polymarket_nautilus/live/wallet_truth.py)
  - [order_truth.py](/Users/noel/projects/trading_polymarket_nautilus/live/order_truth.py)
  - [sandbox_order.py](/Users/noel/projects/trading_polymarket_nautilus/live/sandbox_order.py)
  - [windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)
- Proof:
  - [test_wallet_truth.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_wallet_truth.py)
  - [test_order_truth.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_order_truth.py)
  - Stage 8/9 sandbox validation runs
- Current rule:
  - do not try to solve stale-order bugs with wallet truth alone

## BF-06 — Terminal-First Partial-Fill Handling Is Safer Than Event-Order Optimism

- Symptom:
  - a resting maker entry could partially fill, and downstream logic wanted to continue as if the first fill were the final size
- Root cause:
  - continuing execution before the entry leg is terminal causes race-prone assumptions about size
- Fix pattern:
  - cancel the remainder
  - wait for terminal/cancel completion
  - then continue with the actual filled size
- Main files:
  - [fill_rehearsal.py](/Users/noel/projects/trading_polymarket_nautilus/live/fill_rehearsal.py)
- Proof:
  - [test_fill_rehearsal.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_fill_rehearsal.py)
  - Stage 12a live fill rehearsal
- Current rule:
  - partial-fill handling should be terminal-first

## BF-07 — Logging and Artifacts Were Part of Correctness, Not a Nice-To-Have

- Symptom:
  - critical runtime behavior only appeared on stdout
  - worker terminal lines could be lost on shutdown
  - long runs were harder to reconstruct than they should have been
- Root cause:
  - durable logging was treated as an observability extra rather than part of the operator/control surface
- Fix pattern:
  - persist run logs and summaries
  - flush worker output explicitly
  - prefer one durable run directory per process/batch
- Main files:
  - [soak.py](/Users/noel/projects/trading_polymarket_nautilus/live/soak.py)
  - [run_resolution.py](/Users/noel/projects/trading_polymarket_nautilus/live/run_resolution.py)
- Proof:
  - [test_soak.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_soak.py)
  - [test_run_resolution.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_run_resolution.py)
- Current rule:
  - if a run cannot be reconstructed from artifacts, the run surface is incomplete

## BF-08 — Live Resolution Should Scan Wallet-Held PM Positions, Not Just the Fresh Window Horizon

- Symptom:
  - a restarted live resolution worker could miss older carried positions that were already `ready_to_redeem`
- Root cause:
  - live resolution used the current/upcoming window registry as a hard filter instead of scanning actual wallet-held PM positions
- Fix pattern:
  - in live mode, scan actual Polymarket wallet positions without registry restriction
  - keep registry preload as reference metadata only
  - keep sandbox registry-backed, because sandbox positions are synthetic
- Main files:
  - [wallet_truth.py](/Users/noel/projects/trading_polymarket_nautilus/live/wallet_truth.py)
  - [resolution_worker.py](/Users/noel/projects/trading_polymarket_nautilus/live/resolution_worker.py)
  - [run_resolution.py](/Users/noel/projects/trading_polymarket_nautilus/live/run_resolution.py)
- Proof:
  - [test_wallet_truth.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_wallet_truth.py)
  - [test_run_resolution.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_run_resolution.py)
- Current rule:
  - live resolution is wallet-wide within the Polymarket position universe

## BF-09 — Live and Sandbox Execution Semantics Are Not Perfectly Identical

- Symptom:
  - a live passive limit BUY could be rejected even though the sandbox path tolerated the same order shape
- Root cause:
  - sandbox execution is useful for lifecycle flow, but it does not prove every live venue validation rule
- Fix pattern:
  - do not treat sandbox as final proof for live adapter/order semantics
  - keep tiny live rehearsals for venue-specific validation
- Main files:
  - this is a cross-cutting operational rule, not one file
- Proof:
  - Stage 11/12 live rehearsals
  - bounded live rehearsals after sandbox passes
- Current rule:
  - sandbox proves flow; live rehearsal proves venue semantics

## BF-10 — Shared Node+Worker Orchestration Is Useful for Bounded Runs, but Operational Separation Still Matters

- Symptom:
  - bounded sandbox runs benefited from one-command node+worker orchestration
  - continuous/live operation still needed decoupled restart and failure handling
- Root cause:
  - operator convenience and production supervision are different needs
- Fix pattern:
  - support one-command orchestration for bounded/manual runs
  - keep node and resolution worker as separate logical processes in continuous live operation
- Main files:
  - [soak.py](/Users/noel/projects/trading_polymarket_nautilus/live/soak.py)
- Proof:
  - [test_soak.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_soak.py)
  - sandbox and bounded-live orchestration runs
- Current rule:
  - shared orchestration is for rehearsal/operator convenience, not a replacement for decoupled production supervision

## BF-11 — Warmup DB Must Be Treated as Model State, Not a Cache

- Symptom:
  - startup signals could be mathematically consistent but still wrong because the warmup history feeding the model was stale or gappy
- Root cause:
  - pre-score-date OHLCV warmup was treated like a reusable cache instead of a required part of runtime model state
  - startup backfill only repaired same-day history, not missing pre-score-date history
- Fix pattern:
  - validate the local warmup DB against the artifact/source cutoff
  - require minute-contiguous warmup history
  - refresh once, then fail closed if still invalid
- Main files:
  - strategy-specific runtime warmup loaders
  - startup validation around artifact-backed signal inputs
- Proof:
  - corrected signal-only rerun after the stale-cache failure
- Current rule:
  - model warmup inputs are required state, not opportunistic cache data

## BF-12 — Live Feed Timestamps Need Normalization Before Window-Key Lookup

- Symptom:
  - window-anchor lookup could fail after rollover even though the bars were present
- Root cause:
  - live Binance timestamps were not exact minute-boundary values, so exact timestamp matching broke anchor lookup
- Fix pattern:
  - normalize live timestamps to the intended minute/window boundary before using them as keys
- Main files:
  - strategy-level window-anchor lookup code
- Proof:
  - corrected rollover logs after the anchor fix
- Current rule:
  - never trust exact live-feed timestamps for window-key equality without normalization

## BF-13 — Generic Quote Freshness Is Not Enough for Execution-Side Logic

- Symptom:
  - a generic quote-health flag could say everything was fine while the execution side still lacked usable liquidity
- Root cause:
  - quote freshness and side tradability were being conflated
- Fix pattern:
  - keep a generic quote-health signal for observability
  - keep separate side-specific execution guards for actual entry and exit
- Main files:
  - [windowed.py](/Users/noel/projects/trading_polymarket_nautilus/live/strategies/windowed.py)
  - strategy-specific execution guard surfaces
- Proof:
  - signal-only and sandbox validation after explicit `pm_guard` logging was added
- Current rule:
  - quote freshness and side tradability are different concepts and should stay different

## BF-14 — Settlement-Reconciled Positions Must Count as Completed Lifecycles

- Symptom:
  - a bounded rehearsal could keep trading after a carried residual had already been settled and reconciled
- Root cause:
  - only normal live position closes were counted toward lifecycle limits
  - externally settled carries were not
- Fix pattern:
  - use one shared lifecycle-completion path for both:
    - normal position close
    - wallet-truth settlement reconciliation
- Main files:
  - strategy-specific lifecycle/rehearsal control code
- Proof:
  - bounded sandbox rerun after the lifecycle-count fix
- Current rule:
  - a lifecycle is complete when exposure is operationally finished, not only when Nautilus emits `PositionClosed`

## BF-15 — Stop-Loss Policy Needed Better Evidence Before More Logic

- Symptom:
  - stop-loss behavior could look suspicious, but the available logs were too thin to tell whether the trigger was genuinely bad or just poorly explained
- Root cause:
  - stop events were being judged from coarse heartbeats instead of structured quote-by-quote evidence
- Fix pattern:
  - add structured diagnostic telemetry first
  - include:
    - full triggering quote snapshot
    - recent quote history
    - explicit stop event records
  - only then revise the policy
- Main files:
  - [profile.py](/Users/noel/projects/trading_polymarket_nautilus/live/runs/profile.py)
  - [soak.py](/Users/noel/projects/trading_polymarket_nautilus/live/soak.py)
  - strategy-specific event emission code
- Proof:
  - structured stop-diagnostic sandbox runs
  - [test_soak.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_soak.py)
- Current rule:
  - if the policy is unclear, improve telemetry before adding more stop logic

---

## Repeated Lessons

1. Terminal-first leg management is safer than event-order optimism.
2. Wallet truth and order truth should stay separate.
3. Side-aware PM quote handling is mandatory.
4. Warmup/state inputs are part of the model, not convenience data.
5. Durable logs are part of correctness.
6. Sandbox is necessary but not sufficient for live venue semantics.
