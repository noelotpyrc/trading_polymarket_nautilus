# Wallet Resolution Plan

This document captures the agreed design for post-trading wallet truth and
resolution handling. It exists separately from
[docs/live_testing_plan.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_testing_plan.md)
because the architecture and implementation steps are more detailed than a
roadmap stage summary.

## Scope

This plan covers two new capabilities:

1. A wallet-truth interface the Nautilus live node can query for relevant
   Polymarket-held state.
2. A separate resolution process that checks held YES/NO positions against
   market resolution and handles settlement/redemption outside the Nautilus
   trading node.

This plan does **not** introduce:

- a separate generic "account truth" service
- a standalone production wallet broadcaster service
- external open-order management
- settlement logic inside the Nautilus strategy/runtime

## Design Decisions

### 1. Preloaded window metadata defines the trading universe

The preloaded windows are configuration metadata, not node-specific runtime
state. They define the allowlisted market universe for both:

- wallet truth checks
- resolution checks

For each window we need:

- `condition_id`
- YES token id
- NO token id
- window timing metadata
- optional profile or strategy labels for operator visibility

This metadata should be reusable across:

- live trading node startup
- resolution worker scans
- sandbox synthetic wallet tests

### 2. Wallet truth is narrower than full account truth

For this system, "wallet truth" means only the wallet-held state that can
affect node operation:

- usable collateral balance
- held YES/NO token balances for the allowlisted universe
- later, redeemed cash after resolution

It does **not** include open-order truth.

Open orders stay inside Nautilus and the Polymarket execution adapter. That is
the correct boundary because order lifecycle is already owned by the trading
node.

### 3. Resolution must not trust node residual records

The resolution process must operate from wallet-held YES/NO positions, not from
node-emitted residual records. Residual records can still be useful for logs and
operations, but they are not the source of truth.

This protects us against cases like:

- node crash before residual state is persisted
- multiple nodes sharing one wallet over time
- residual positions that outlive a specific node process

### 4. Sandbox needs a synthetic wallet store

Production does not need a standalone wallet broadcaster module, but sandbox
still needs a synthetic wallet-state implementation because there is no real
wallet redemption flow.

That synthetic store is the stand-in for:

- held YES/NO positions
- cash balance
- synthetic settlement credits after resolution

## Component Model

### A. Metadata Registry

Shared config/data layer containing the allowlisted market universe.

Responsibilities:

- load preconfigured windows
- expose condition ids and YES/NO token ids
- support lookup by instrument id, token id, or condition id
- support active and historical windows needed for resolution scanning

This can initially be a library/module, not a service.

### B. Wallet Truth Provider

Node-facing interface for relevant wallet state.

Suggested shape:

```python
class WalletTruthProvider(Protocol):
    def snapshot(self) -> WalletTruthSnapshot: ...
```

Suggested snapshot contents:

- wallet/funder address
- collateral balance
- held positions keyed by token id or condition id
- optional per-position fields:
  - outcome side
  - size
  - redeemable flag
  - market resolved flag
  - last updated time

Important:

- no open orders here
- no attempt to replace Nautilus order/account internals

### C. Production Wallet Truth Provider

Uses Polymarket APIs directly, independent of a running Nautilus node.

Responsibilities:

- query relevant wallet-held positions for the allowlisted universe
- query relevant collateral balance
- normalize into `WalletTruthSnapshot`

Recommended inputs:

- metadata registry
- wallet/funder identity
- Polymarket credentials

Recommended data sources:

- positions endpoint for held YES/NO positions
- balance/allowance endpoint for collateral balance

### D. Sandbox Wallet Store

Synthetic wallet-state module for tests and sandbox integration.

Responsibilities:

- track synthetic cash balance
- track held YES/NO token balances
- apply synthetic fills
- apply synthetic settlement credits

This is intentionally not a Nautilus replacement. It is the truth source for
the sandbox wallet-truth provider.

### E. Sandbox Wallet Truth Provider

Reads from `SandboxWalletStore` and returns the same
`WalletTruthSnapshot` schema as production.

This keeps prod/sandbox parity at the interface boundary.

### F. Resolution Worker

Separate process, outside the Nautilus trading node.

Responsibilities:

- load metadata registry
- read wallet truth
- find held YES/NO positions in the allowlisted universe
- check market resolution status
- in prod:
  - redeem winning positions
  - record settlement results
- in sandbox:
  - apply synthetic settlement to `SandboxWalletStore`

Important:

- resolution worker does not own open-order state
- resolution worker does not depend on Nautilus cache

## Production Flow

1. The Nautilus node loads preconfigured windows and trades them normally.
2. The production wallet-truth provider reads relevant held positions and
   collateral balance from Polymarket-facing APIs for the configured wallet.
3. The node polls the provider on a timer and uses the returned snapshot only
   for wallet-relevant operational state.
4. The separate resolution worker also uses the same metadata registry and the
   same wallet identity.
5. When a held YES/NO token becomes resolved and redeemable, the worker redeems
   it.
6. The next wallet-truth snapshot reflects the new collateral state.
7. The node can then incorporate that updated wallet truth into its operational
   balance view.

## Sandbox Flow

1. The Nautilus sandbox node trades normally against synthetic execution.
2. Sandbox fills update `SandboxWalletStore`.
3. The sandbox wallet-truth provider returns snapshots from that store.
4. The resolution worker reads the same synthetic wallet truth.
5. When a held sandbox YES/NO token becomes resolved:
   - winning positions create synthetic cash credit
   - losing positions settle to zero
6. `SandboxWalletStore` is updated.
7. The next node poll sees the updated synthetic wallet truth.

This validates the process boundaries and state transitions without claiming to
validate real onchain redemption.

## Node Integration

The Nautilus node already owns:

- open-order lifecycle
- strategy order/position logic
- Polymarket execution interaction

The new node-side work should stay narrow:

- add a timer-driven wallet-truth polling hook
- map `WalletTruthSnapshot` into the small subset of state the node needs
- do not move open-order management out of Nautilus

Recommended first use cases for node consumption:

- visibility into externally updated collateral balance
- awareness of held YES/NO positions that are no longer part of active trading
- future reconciliation after external redemption

## Implementation Phases

### Phase 1: Shared Types and Registry

Implement:

- metadata registry module
- wallet-truth snapshot schema
- provider interface

Success criteria:

- both prod and sandbox code can load the same metadata schema
- one snapshot type is shared by all consumers

### Phase 2: Sandbox Wallet Store

Implement:

- `SandboxWalletStore`
- tests for fills, held balances, and synthetic settlement

Success criteria:

- sandbox wallet state can represent:
  - collateral
  - YES/NO holdings
  - resolution credit/debit outcomes

### Phase 3: Sandbox Wallet Truth Provider + Node Hook

Implement:

- sandbox provider
- timer-driven node polling hook
- node handling of wallet-truth updates

Success criteria:

- sandbox node can observe synthetic wallet truth updates
- resolved sandbox winners can become reusable synthetic balance through the
  wallet-truth path

### Phase 4: Production Wallet Truth Provider (Read-Only)

Implement:

- prod provider using Polymarket-facing APIs
- read-only integration tests and CLI checks

Success criteria:

- provider can read relevant collateral and held YES/NO positions for the
  configured wallet
- results are filtered to the metadata allowlist

### Phase 5: Resolution Worker (Sandbox First)

Implement:

- resolution worker loop
- sandbox settlement path writing into `SandboxWalletStore`
- durable logs/records of settlement decisions

Success criteria:

- carried sandbox positions can be detected, resolved, and settled into
  synthetic wallet cash without manual intervention

### Phase 6: Resolution Worker (Production Redemption)

Implement:

- production resolution polling
- redemption workflow
- durable settlement records

Success criteria:

- winning carried positions can be redeemed outside Nautilus
- subsequent wallet-truth snapshots reflect the redeemed balance

## Testing Strategy

### Unit Tests

- metadata registry parsing and lookup
- wallet snapshot normalization
- sandbox wallet store balance transitions
- resolution decision logic

### Sandbox Integration Tests

- partial fill creates carried residual
- resolution worker sees held position in synthetic wallet
- winning outcome credits synthetic collateral
- node sees updated synthetic wallet truth

### Production Read-Only Checks

- provider can read wallet-held positions for the configured allowlist
- provider can read collateral balance
- resolution worker can detect resolved vs unresolved positions without redeeming

### Production Live Validation

- one small carried residual is redeemed by the external resolution worker
- next wallet-truth poll reflects the updated balance
- node remains consistent after the external balance change

## Deferred Work

This plan intentionally defers:

- generic cross-process monitoring/broadcast infrastructure
- open-order truth outside Nautilus
- arbitrary wallet-wide CTF discovery beyond the configured market universe
- redesign of the Nautilus execution adapter itself

## Open Questions To Resolve During Implementation

1. Exact node-side mapping from `WalletTruthSnapshot` into Nautilus-facing
   balance updates.
2. Whether production provider should also surface `redeemable` status directly
   or leave that entirely to the resolution worker.
3. How much historical market metadata should be retained so the resolution
   worker can continue handling older windows after node restarts.
