# Live Trading

Scripts for real-time trading on Polymarket. Uses authenticated APIs — requires a funded EOA wallet on Polygon.

For the detailed live hardening inventory and current committed vs local-only status, see [docs/live_hardening_status.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_hardening_status.md).

---

## One-time Setup

### 1. Install dependencies

```bash
pip install -e .
```

### 2a. Generate a test wallet *(sandbox mode — no funding needed)*

```bash
python live/setup/generate_wallet.py --test
python live/setup/init_trading.py --test
```

Creates a throwaway EOA and derives API credentials. Writes `POLYMARKET_TEST_*` vars to `.env`.
No USDC, no POL, no on-chain transactions required. Do this first before setting up the production wallet.

### 2b. Generate a production wallet *(live trading)*

```bash
python live/setup/generate_wallet.py
```

Creates a new EOA and writes `PRIVATE_KEY` and `WALLET_ADDRESS` to `.env`. Run once only.

### 3. Derive production API credentials *(no funding needed)*

```bash
python live/setup/init_trading.py
```

### 4. Fund your production wallet on Polygon

Send to your `WALLET_ADDRESS`:
- **USDC.e** — trading collateral (`0x2791bca1f2de4661ed88a30c99a7a9449aa84174`)
- **POL** — a small amount for gas (formerly MATIC)

### 5. Set USDC allowances *(requires POL for gas)*

```bash
python live/setup/set_allowances.py
```

Approves Polymarket contracts to spend your USDC. One-time on-chain transaction.

After this, `.env` will look like `.env.example` with all fields populated.

### Alternate env files

If you keep a funded live wallet in a separate env file, the main live entrypoints
now accept `--env-file` so you do not need to shell-source secrets or swap the
repo `.env` file manually.

Examples:

```bash
python live/setup/init_trading.py --env-file /abs/path/live_wallet.env
python live/setup/set_allowances.py --env-file /abs/path/live_wallet.env
python live/trade.py --env-file /abs/path/live_wallet.env --trades
python live/run_resolution.py btc_updown_15m_live --env-file /abs/path/live_wallet.env --once
python live/runs/profile.py btc_updown_15m_live --env-file /abs/path/live_wallet.env
python live/soak.py btc_updown_15m_live --allow-live --env-file /abs/path/live_wallet.env --run-secs 300
```

This keeps the selected wallet explicit at the command level instead of relying
on shell state.

---

## Authentication

Polymarket uses a two-tier auth system:

| Layer | What | When |
|---|---|---|
| L1 | Private key (EIP-712 signing) | Deriving API creds, signing orders |
| L2 | API key + secret + passphrase | All authenticated HTTP requests |

Both are handled automatically by `py-clob-client`.

---

## Structure

```
node.py              # Shared TradingNode helpers (window resolution + client wiring)
config.py            # Client config builders (Binance + Polymarket data/exec clients)
market_metadata.py   # Shared allowlisted YES/NO token metadata for trading + resolution
resolution.py        # Polymarket market-resolution polling helpers
wallet_truth.py      # Production wallet-truth snapshot/provider helpers
sandbox_wallet.py    # Synthetic sandbox wallet store + wallet-truth provider
resolution_worker.py # External wallet-based resolution worker primitives
redemption.py        # Production redemption backend (dry-run or execute)
run_resolution.py    # External resolution-worker CLI
rehearsal.py         # Stage 11 live order lifecycle rehearsal (resting order -> cancel)
fill_rehearsal.py    # Stage 12a live limit-fill rehearsal (entry -> limit exit or settlement)
redeem_oneoff.py     # One-off redemption helper for a resolved live market slug
profiles/
  catalog/           # Checked-in runner profile TOML files
runs/
  btc_updown.py      # Infrastructure test runner: momentum-based, slower warmup
  profile.py         # Generic profile runner (`--list`, print/override support)
  profiles/          # Fixed per-profile entrypoints
  random_signal.py   # Infrastructure test runner: fast stack exercise
soak.py             # Sequential bounded soak runner with durable logs/summaries
trade.py             # Ad-hoc order placement CLI (manual BUY/SELL for testing)
strategies/
  btc_updown.py      # Infrastructure test strategy logic
  random_signal.py   # Infrastructure test strategy logic
setup/
  generate_wallet.py   # Step 1: create EOA
  init_trading.py      # Step 2: derive API creds (no funding needed)
  set_allowances.py    # Step 3: approve USDC contracts (needs POL for gas)
  sweep.py             # Sweep excess USDC to safe wallet (run before/after sessions)
```

## Running the live node

### Fixed profile entrypoints

```bash
# List available profiles
python live/runs/profile.py --list

# Fast sandbox validation — preferred first full-stack check
python live/runs/profiles/random_signal_15m_sandbox.py

# Fast sandbox validation on the NO outcome
python live/runs/profiles/random_signal_15m_sandbox_no.py

# Deterministic sandbox residual-carry validation for the external resolution worker
python live/runs/profiles/random_signal_15m_resolution_sandbox.py

# Slower sandbox validation — exercises the 14-day Binance warmup path
python live/runs/profiles/btc_updown_15m_sandbox.py

# Slower sandbox validation on the NO outcome
python live/runs/profiles/btc_updown_15m_sandbox_no.py

# Unbounded live run — includes 14-day Binance warmup, only after the sandbox gate is complete
python live/runs/profiles/btc_updown_15m_live.py

# Unbounded live NO-outcome run
python live/runs/profiles/btc_updown_15m_live_no.py
```

### Ad hoc runners

```bash
python live/runs/random_signal.py --slug-pattern btc-updown-15m --hours-ahead 1 --outcome-side no --sandbox --run-secs 180
python live/runs/btc_updown.py --slug-pattern btc-updown-15m --hours-ahead 2 --outcome-side no --sandbox --run-secs 600
python live/runs/btc_updown.py --slug-pattern btc-updown-15m --outcome-side yes
```

Both strategies are infrastructure test strategies for validating the Nautilus live process. They are not the intended production trading logic.

At startup the node validates credentials, resolves upcoming market windows from Gamma, pre-loads their instruments, and trades until the pre-loaded schedule is exhausted. The expected behavior for this phase is to restart the node each day; missing the first window after restart is acceptable.

If an old Polymarket window cannot be fully flattened after it ends, the node now carries that known residual YES/NO position to resolution instead of forcing an unnecessary stop. Post-resolution settlement / redemption is intentionally deferred to a separate external process.

The live Polymarket feed now keeps one-sided books visible by allowing synthetic quotes from Nautilus. Strategy logic is side-aware:
- BUY entry requires a fresh ask with positive size
- active-window SELL / flatten decisions require a fresh bid with positive size
- midpoint pricing is only used when both sides have positive size

## External Resolution Worker

Stage 8 now has a first-pass external worker surface for wallet-based resolution handling:

```bash
# Dry-run one sandbox scan against a shared synthetic wallet-state file
python live/run_resolution.py btc_updown_15m_sandbox --hours-ahead 8 --once --sandbox-wallet-state-path logs/soak/.../wallet_state.json

# Dry-run one live scan against the production wallet allowlist
python live/run_resolution.py btc_updown_15m_live --once

# In live mode, actual redemption remains opt-in
python live/run_resolution.py btc_updown_15m_live --once --execute-redemptions
```

Current behavior:
- sandbox mode reads/writes a shared `wallet_state.json`
- live mode reads wallet truth from Polymarket APIs
- live redemptions default to dry-run summaries unless `--execute-redemptions` is passed
- internal node resolution remains advisory only; wallet truth is the authoritative settlement signal
- open-order truth still stays inside the Nautilus node

## Stage 11 Live Rehearsal

Use [rehearsal.py](/Users/noel/projects/trading_polymarket_nautilus/live/rehearsal.py) for the first live control-plane check before any intended fill risk. It submits one tiny post-only resting BUY, confirms it opens, cancels it, then confirms no token balance remains.

```bash
python live/rehearsal.py \
  --env-file /abs/path/live_wallet.env \
  --event bitcoin-above-on-march-1 \
  --market-index 0
```

Or search interactively:

```bash
python live/rehearsal.py \
  --env-file /abs/path/live_wallet.env \
  --search "bitcoin above"
```

Default safety posture:
- price is pinned to the minimum valid tick, not near the touch
- the script refuses markets already trading too close to the price floor
- order type is `GTC` with `post_only=True`
- rehearsal notional defaults to `5.10` USDC
- operator confirmation is required unless `--yes` is passed

Usage note:
- use Stage 11 only on simple binary markets
- avoid neg-risk / multi-outcome market families for this rehearsal
- on those complex markets, the event lookup may still be correct while the raw token-level CLOB book the script reads does not line up with the frontend market view
- if the script shows a degenerate book like `0.01 / 0.99`, treat that market as unsuitable and pick another event/market

Validated result:
- on March 17, 2026, the rehearsal passed on `bitcoin-up-or-down-on-march-18-2026`
- observed lifecycle was submit -> `LIVE` -> cancel -> `CANCELED`
- `size_matched = 0` and conditional balance returned to `0.000000`

The purpose of this stage is only:
- live auth
- real PM order submission
- open-order observation
- live cancel confirmation

It does not validate the Nautilus live node yet, and it is not a live fill rehearsal.

## Stage 12a Live Fill Rehearsal

Use [fill_rehearsal.py](/Users/noel/projects/trading_polymarket_nautilus/live/fill_rehearsal.py) for the first live filled-order rehearsal before attempting a Nautilus-managed live fill.

It uses direct PM client calls on `btc-updown-15m` windows and persists per-run artifacts under `logs/fill_rehearsal/`.

Default policy:
- watches upcoming `btc-updown-15m` windows but trades only the current active one
- enters only in the last `60s`
- requires chosen-side best bid `> 0.90`
- uses passive best-bid limit entry with bounded `10s` reprices
- if any entry attempt partially fills, entry is latched complete for that window and no further BUYs are submitted
- first tries a profitable passive live limit exit
- falls back to settlement if profitable live exit is impossible or if the remaining token balance is below PM `min_order_size`
- optional in-process settlement waiting and redemption

Example `limit_exit`-capable run:

```bash
python live/fill_rehearsal.py \
  --env-file /abs/path/live_wallet.env \
  --outcome-side yes \
  --hours-ahead 2 \
  --label stage12a_debug4
```

Example settlement-targeted run:

```bash
python live/fill_rehearsal.py \
  --env-file /abs/path/live_wallet.env \
  --outcome-side yes \
  --hours-ahead 2 \
  --profit-buffer-usd 0.10 \
  --wait-for-settlement \
  --redeem-on-settlement \
  --label stage12a_settlement2
```

Validated results:
- `limit_exit` branch passed on March 19, 2026:
  - [stage12a_debug4 summary](/Users/noel/projects/trading_polymarket_nautilus/logs/fill_rehearsal/20260318T235609Z_stage12a_debug4/summary.json)
- `settlement + redeem` branch passed on March 19, 2026:
  - [stage12a_settlement2 summary](/Users/noel/projects/trading_polymarket_nautilus/logs/fill_rehearsal/20260319T182233Z_stage12a_settlement2/summary.json)

If you need to redeem a specific resolved market manually, use [redeem_oneoff.py](/Users/noel/projects/trading_polymarket_nautilus/live/redeem_oneoff.py):

```bash
python live/redeem_oneoff.py \
  --env-file /abs/path/live_wallet.env \
  --market-slug btc-updown-15m-1773869400 \
  --execute \
  --yes
```

## Runner Profiles

- Profile files live in [live/profiles/catalog](/Users/noel/projects/trading_polymarket_nautilus/live/profiles/catalog).
- Secrets stay in `.env` by default, or in an explicit alternate file passed via `--env-file`; profile files only hold checked-in runtime choices.
- Current catalog:
  - `btc_updown_15m_live`
  - `btc_updown_15m_live_no`
  - `random_signal_15m_order_reconciliation_sandbox`
  - `random_signal_15m_resolution_sandbox`
  - `random_signal_15m_sandbox`
  - `random_signal_15m_sandbox_no`
  - `btc_updown_15m_sandbox`
  - `btc_updown_15m_sandbox_no`
- Each profile pins strategy, market slug pattern, hours ahead, mode, Binance route, selected outcome side, bounded runtime if any, and strategy-specific knobs.
- The checked-in `btc_updown` profiles currently set `warmup_days = 14`.
- Fixed profile entrypoints are the preferred operator surface.
- The generic profile runner supports bounded runtime plus window-horizon overrides:

```bash
python live/runs/profiles/btc_updown_15m_live.py --run-secs 300
python live/runs/profile.py btc_updown_15m_live --print-profile
python live/runs/profile.py btc_updown_15m_sandbox --hours-ahead 8 --run-secs 28800
python live/runs/profile.py random_signal_15m_order_reconciliation_sandbox --print-profile
python live/runs/profile.py random_signal_15m_resolution_sandbox --hours-ahead 2 --sandbox-wallet-state-path logs/stage8/random_wallet_state.json
python live/runs/profile.py random_signal_15m_resolution_sandbox --sandbox-starting-usdc 25
```

## Operator Notes

- `--run-secs` is the bounded-session switch for smoke and sandbox runs. Leave it unset for an unbounded process.
- `--outcome-side yes|no` selects the first or second Polymarket outcome token for ad hoc runners.
- On BTC up/down markets, `outcome_side=yes` maps to `Up` and `outcome_side=no` maps to `Down`.
- The node will stop cleanly when it runs out of pre-loaded windows and will log that a restart is required.
- Daily restart is the intended operating model for this phase. There is no automatic cross-day market extension yet.
- If an ended-window residual cannot be flattened because liquidity disappears or the remaining size is below minimum order size, the node carries it to resolution and logs the outcome once Polymarket resolves the market.
- Low free collateral now blocks new entries immediately, but the node only stops for low balance once it is flat and otherwise idle.
- A requested process stop waits for any carried residuals to resolve before final node shutdown.
- External wallet-truth reconciliation, not the node’s internal resolution poll, is what clears carried residuals authoritatively.
- Run the sandbox validation sequence in `docs/live_testing_plan.md` before considering any real-order rehearsal.
- The detailed health-guard policy lives in [docs/live_health_guard_policy.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_health_guard_policy.md).
- The recurring `convert_quote_qty_to_base` warning is currently sandbox-only.
  It comes from Nautilus converting quote-denominated Polymarket market BUY
  notional into base shares for the sandbox matching engine. Live Polymarket
  behavior is unchanged and still uses quote-denominated market BUYs.
- The recurring `Instrument tick size changed` warning is currently treated as
  a Polymarket metadata-change notification surfaced by the Nautilus adapter.
  In the current market-order setup it is mostly log noise unless it begins to
  coincide with price-precision rejects or quote/book reconstruction failures.

## Soak Runs

Use [soak.py](/Users/noel/projects/trading_polymarket_nautilus/live/soak.py) for the longer multi-hour sandbox sessions after the side-aware Polymarket quote update lands. It runs one or more profiles sequentially, captures stdout/stderr, and writes per-run plus batch summaries under `logs/soak/`.

```bash
# 4h soak on one sandbox profile
python live/soak.py random_signal_15m_sandbox --run-secs 14400 --label stage7_4h

# 8h soak on one sandbox profile while overriding the profile preload horizon
python live/soak.py btc_updown_15m_sandbox --hours-ahead 8 --run-secs 28800 --label stage7_btc_8h

# 8h soak on two profiles, continue even if the first fails
python live/soak.py random_signal_15m_sandbox btc_updown_15m_sandbox --run-secs 28800 --label stage7_8h --keep-going

# One-command Stage 8 deterministic residual + worker validation
python live/soak.py random_signal_15m_resolution_sandbox --with-resolution-worker --label stage8_resolution_smoke

# One-command Stage 9 stale-IOC reconciliation validation
python live/soak.py random_signal_15m_order_reconciliation_sandbox --with-resolution-worker --label stage9_order_truth_smoke
```

Safety defaults:
- sandbox profiles only unless `--allow-live` is passed
- bounded runtime required unless `--allow-unbounded` is passed

Artifacts:
- `runner.log` — combined stdout/stderr for the profile run
- `worker.log` — companion resolution-worker output when `--with-resolution-worker` is used
- `profile.json` — resolved profile settings used for the run
- `summary.json` — exit code, duration, log path, and status
- `wallet_state.json` — synthetic sandbox wallet truth for Stage 8 resolution tests
- batch-level `summary.json` — overall batch status across all profiles

## Next Steps

The detailed roadmap lives in [docs/live_testing_plan.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_testing_plan.md). The next implementation stages are:

1. Longer sandbox soak runs
   - Purpose: prove multi-hour stability.
   - Success: repeated rollovers and long runtimes remain clean.
2. Live order lifecycle rehearsal
   - Purpose: prove live submit/open/cancel behavior with no intended fill.
   - Success: a tiny non-marketable live order opens and cancels cleanly.
3. Minimum-size live fill rehearsal
   - Purpose: prove the live execution path end-to-end.
   - Success: one minimum-size live round trip reconciles with Polymarket.
4. Observability tightening
   - Purpose: make the live system operable at session and multi-node scale.
   - Success: logs and runbook are enough to diagnose failures without code inspection.
