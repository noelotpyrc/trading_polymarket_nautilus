# Live Health Guard Policy

Decision reference for Stage 5 live-process safeguards. This document defines the runtime health states, what counts as stale or incomplete input, and how the node should react when signal or execution state is degraded.

---

## State Model

- `healthy`: normal trading allowed.
- `initializing`: expected startup state. No new entries yet, but the node is not considered degraded.
- `degraded_entry_blocked`: process stays up, but no new entries are allowed. Cancels, exits, rollover, and cleanup are still allowed.
- `stop_required`: process state is not trustworthy enough to continue. The node should do a controlled stop.
- `planned_stop`: expected clean stop such as bounded runtime or window exhaustion. Not a degraded state.

---

## Input Definitions

### Stale

An input is `stale` when it exists, but is too old to trust for new entry decisions.

Examples:
- latest Binance 1-minute signal bar is too old relative to wall clock
- latest valid two-sided Polymarket quote is too old for the active instrument

### Incomplete

An input is `incomplete` when the required structure is missing.

Examples:
- not enough Binance bars to compute the signal yet
- no valid two-sided Polymarket quote exists for the active instrument
- warmup request is still in flight

### Gap

A `gap` is a signal-series integrity failure, not just staleness.

For Binance 1-minute bars, a gap exists when the next accepted bar timestamp skips one or more expected 1-minute intervals. Once that happens, any signal whose lookback window overlaps the missing interval is invalid until the series is repaired or the gap ages out of the lookback window.

### Signal-Invalid

`signal-invalid` means the strategy must not trust the Binance bar series for new entries, even if live bars are arriving again.

This applies when:
- a Binance gap has been detected and not yet healed
- warmup has not yet produced a usable signal window

---

## Policy Matrix

| Condition | Definition | State | Action | Successful handling |
|---|---|---|---|---|
| Missing env vars / invalid resolved windows | Preflight failure before node start | `stop_required` | Abort startup immediately | Node never starts; reason is explicit |
| Warmup in flight | Historical request still running, or merged bars `< signal_lookback + 1` | `initializing` | Block entries, keep buffering live bars | No entries before warmup completes |
| Warmup timeout | Warmup still in flight after `300s` | `stop_required` | Log error and stop node | Clean stop with clear timeout reason |
| Warmup completed with `0` historical bars | Request finished but returned no history | `stop_required` | Stop node | No trading with missing intended history |
| Warmup completed with some history but still not enough bars | Merged bars still `< signal_lookback + 1` | `initializing` | Keep blocking entries until enough live bars arrive | First signal only happens after bar sufficiency |
| Binance bars stale | Latest expected BTC 1-minute bar has not arrived on time | `degraded_entry_blocked` | Block new entries | No entries while the signal feed is stale |
| Binance bar series gapped | One or more closed BTC 1-minute bars are missing from the accepted series | `degraded_entry_blocked` | Mark signal invalid and block new entries | No entries while the signal window is contaminated |
| Binance gap healing | Missing range is being repaired via historical backfill | `degraded_entry_blocked` | Buffer live bars, merge/sort/dedupe after backfill | Trading resumes only after the series is contiguous again |
| Persistent Binance outage | Binance bars remain stale or gapped for an extended period | `degraded_entry_blocked` | Keep node alive, block entries, log loudly, alert operator | Process remains safe without silent re-entry |
| Polymarket quote incomplete | No valid two-sided quote for the active instrument | `degraded_entry_blocked` | Block new entries | One-sided or missing quote never triggers entry |
| Polymarket quote stale | Last valid two-sided quote for the active instrument is older than `120s` | `degraded_entry_blocked` | Block new entries | No entries on stale Polymarket price |
| Entry order pending too long | Entry order is still pending after `90s` | `degraded_entry_blocked` | Cancel the order and keep blocking entries | Order resolves to fill/cancel/deny/reject |
| Entry order unresolved after cancel grace | Entry order still has no terminal state after `180s` | `stop_required` | Stop node and require manual reconciliation | Node exits with the order id and reason in logs |
| Late fill on old window / canceled entry | An obsolete order fills after rollover or after cancel was requested | `degraded_entry_blocked` | Flatten immediately and keep blocking entries until flat | Residual exposure is removed and current state is clean |
| Late-fill cleanup fails | Residual late-fill position is not flat after `60s` | `stop_required` | Stop node | No continued trading with hidden exposure |
| Window exhaustion | No next preloaded window exists | `planned_stop` | Stop node with restart-needed message | Clean stop; not treated as failure |
| Lifecycle invariant violation | Impossible internal strategy/order/window state | `stop_required` | Best-effort cancel/flatten, then stop | Trading halts immediately with a critical log |

---

## Binance Gap Handling

### Why gap handling is separate from simple staleness

If a live bar is only late, the node can recover immediately once the expected next bar arrives.

If a true Binance gap exists, the signal window is contaminated for the whole lookback horizon. Example: a 100-bar signal window with a missing recent 10-bar segment remains invalid until the gap is repaired or fully rolls out of the signal input.

### Recovery policy

- Preferred: request historical backfill for the missing range using Nautilus `request_bars(...)`, then merge/sort/dedupe with buffered live bars.
- Fallback: if backfill is unavailable or intentionally disabled, keep entries blocked until the gap has fully rolled out of the lookback window.

The policy for this project is:
- do not stop the node purely because Binance is stale or gapped
- do block new entries until the signal series is trustworthy again

---

## PM Order-Lifecycle Notes

### Entry order pending too long

This means the strategy submitted an entry order, but Nautilus never observed a timely terminal state.

Terminal states:
- filled
- canceled
- denied
- rejected
- expired

This is an uncertainty problem. The strategy may not know whether it has risk or not, so new entries must remain blocked until the order is resolved or the node is stopped.

### Late fill on old window / canceled entry

This means an order fills after the strategy has already moved on from the context where that order was valid.

Common cases:
- the strategy rolled from window A to window B, then a fill arrives for window A
- the strategy sent cancel for an entry order, but the venue still filled it before cancel took effect

This is an unintended exposure problem. The correct response is to flatten the unexpected position immediately and avoid new entries until flatness is confirmed.

### Flatten

`Flatten` means removing exposure so net position becomes zero on that instrument.

In this project, flattening means closing the unexpected YES or NO token position immediately.

---

## Stage 5 v1 Implementation Scope

Current implementation covers:
- warmup timeout handling
- Binance stale/gap detection with entry blocking in `btc_updown`
- explicit degraded-state logging in `btc_updown`
- pending-entry timeout and cancel escalation
- late-fill cleanup escalation

Out of scope for the first pass:
- automatic node stop purely from Binance staleness
- automatic node stop purely from Polymarket quote staleness
- production alerting integrations

Guardrail fault-injection E2E coverage for these policies now lives in [tests/live/test_guardrails_e2e.py](/Users/noel/projects/trading_polymarket_nautilus/tests/live/test_guardrails_e2e.py).
