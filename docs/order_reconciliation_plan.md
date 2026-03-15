# PM Order Reconciliation Plan

This document captures the planned follow-on stage after Stage 8 wallet
resolution closed `P1b`. Its focus is `P1a`: reconciling
Nautilus order state against real Polymarket order truth so stale partially
filled IOC remainders do not linger as ambiguous cache residue.

## Scope

This stage adds one new capability:

1. A node-facing Polymarket order-reconciliation path that can confirm whether a
   partially filled order remainder is still live on PM, and react accordingly.

This plan covers both:

- production reconciliation against real PM/CLOB order truth
- synthetic sandbox reconciliation for deterministic validation

This plan does **not** replace:

- the Stage 8 wallet-truth interface
- the external resolution worker
- Nautilus-owned active order submission / cancel flow

## Why This Stage Exists

Stage 8 closed `P1b`:

- carried residual positions can be observed in wallet truth
- the external worker can settle them outside Nautilus

But `P1a` is a different problem:

- an `IOC` order partially fills
- Nautilus never receives a terminal event for the unfilled remainder
- the order stays cached as `PARTIALLY_FILLED`

That is unsafe to resolve with local heuristics alone, especially for entry
orders, because the remainder could still be open on PM and still reserve cash
or fill later.

So `P1a` requires external PM order truth, not just wallet truth.

## Design Decisions

### 1. PM order truth is the authority for stale order remainders

When a local IOC remainder looks stale, the node must determine whether that
order is still live on PM.

The key questions are:

- is the order still open on PM?
- if so, can we cancel it?
- if not, is local exposure consistent with PM position / wallet truth?

Only after that check should the node treat the local cached order as
reconciled.

### 2. This is node-side reconciliation, not a separate service

Unlike Stage 8 resolution, this does not need a new external worker process.

Why:

- the node already owns live order lifecycle
- the node already owns cancel authority
- the node is the consumer of the local stale order cache problem

So this stage should add a node-facing provider or adapter hook, not a separate
daemon.

### 3. Wallet truth remains separate

Wallet truth still answers:

- what YES/NO tokens are held
- what collateral is held

PM order reconciliation answers:

- is this order still open on PM?
- is this local cached order record stale or still live?

These should stay separate, though the reconciliation step may use wallet truth
as supporting evidence when checking exposure consistency.

### 4. Sandbox needs synthetic order truth

To validate this deterministically in sandbox, we need a synthetic order-truth
path analogous to the synthetic wallet path from Stage 8.

That sandbox component should simulate:

- still-open orders
- dead IOC remainders
- cancel success
- missing/not-found order cases

## Component Model

### A. Order Truth Provider

Node-facing interface for order reconciliation.

Suggested shape:

```python
class OrderTruthProvider(Protocol):
    def order_status(self, client_order_id: str) -> OrderTruthStatus: ...
```

Suggested normalized statuses:

- `open`
- `closed`
- `canceled`
- `expired`
- `not_found`
- `unknown`

Optional fields:

- venue order id
- remaining quantity if still open
- last updated time

### B. Production PM Order Truth Provider

Uses Polymarket / CLOB order APIs to determine whether a local order remainder is
still live.

Responsibilities:

- query open orders or order status for the node’s wallet
- map PM/CLOB status into a normalized `OrderTruthStatus`
- support lookup by client order id and/or venue order id

### C. Sandbox Order Store

Synthetic order-truth module for sandbox validation.

Responsibilities:

- record synthetic order lifecycle states
- expose whether an order is still open or dead
- simulate cancel success / missing order / already gone remainder

This should be the order-side equivalent of `SandboxWalletStore`.

### D. Node Reconciliation Hook

Shared strategy/runtime hook for suspicious non-terminal orders.

Responsibilities:

- identify IOC remainders that should have become terminal
- query order truth
- if still open:
  - cancel the order for real
  - continue polling until PM confirms it is closed
- if no longer open and exposure is consistent:
  - mark the local order as externally reconciled / dead
- if order truth and exposure disagree:
  - stop the node for manual reconciliation

## Production Flow

1. The node notices a suspicious non-terminal IOC remainder.
2. It queries PM order truth for that order.
3. If the order is still open:
   - the node submits cancel
   - confirms the order is no longer open
4. If the order is not open:
   - the node checks that exposure is still consistent with local position /
     wallet truth
   - then marks the local cached remainder as reconciled
5. If PM order truth, wallet truth, and local state disagree:
   - stop for manual reconciliation

## Sandbox Flow

1. The sandbox node creates partially filled synthetic IOC orders.
2. Synthetic order truth records whether the remainder is:
   - still open
   - canceled
   - dead / not found
3. The node queries the sandbox provider through the same reconciliation
   interface.
4. Tests validate the same decision rules as production without requiring live
   PM order APIs.

## Stage Entry Criteria

Stage 8 `P1b` is closed, so this is now the next active implementation stage.

That means:

- externally settled carried positions reconcile back into node-operational state
- settled carried positions no longer remain as ambiguous residual positions in
  Nautilus cache at shutdown

Only after that should we take on `P1a`.

## Implementation Phases

### Phase 1: Shared Types

Implement:

- `OrderTruthStatus`
- `OrderTruthProvider`
- normalization helpers

Success criteria:

- production and sandbox providers share one normalized interface

### Phase 2: Sandbox Order Truth

Implement:

- sandbox order store
- sandbox provider
- deterministic tests for:
  - open remainder
  - dead remainder
  - cancel success
  - not-found order

Success criteria:

- node reconciliation logic can be tested without PM network calls

### Phase 3: Production PM Order Truth Provider

Implement:

- PM/CLOB order-status reads
- mapping from PM order state to normalized provider status

Success criteria:

- provider can tell whether a client/venue order is still open on PM

### Phase 4: Node Reconciliation Hook

Implement:

- suspicious-order detection
- external order-truth lookup
- real cancel-if-open logic
- local reconcile-if-dead logic
- stop-on-mismatch path

Success criteria:

- stale IOC remainders are either:
  - canceled for real
  - locally reconciled with external proof
  - or escalated to stop

### Phase 5: Validation

Validate:

- sandbox deterministic IOC remainder scenarios
- multi-window sandbox run with forced stale remainder cases
- live dry-run PM order-status visibility
- later, one controlled live cancel confirmation rehearsal

Success criteria:

- no ambiguous stale IOC residual warnings remain for externally reconciled
  orders
- entry-order remainders cannot silently remain live on PM

## Open Questions

1. Which PM/CLOB API path is the most reliable source for point-in-time order
   truth: open-order scans, direct order lookup, or both?
2. Whether local Nautilus reconciliation should mutate cached order objects
   directly or maintain a parallel "externally reconciled dead order" layer for
   shutdown and risk logic.
3. Whether exit-order and entry-order reconciliation should share the exact same
   policy, or whether entry orders should remain stricter because they can
   recreate exposure.
