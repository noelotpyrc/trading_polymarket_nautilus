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
resolution.py        # Polymarket market-resolution polling helpers
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

## Runner Profiles

- Profile files live in [live/profiles/catalog](/Users/noel/projects/trading_polymarket_nautilus/live/profiles/catalog).
- Secrets stay in `.env`; profile files only hold checked-in runtime choices.
- Current catalog:
  - `btc_updown_15m_live`
  - `btc_updown_15m_live_no`
  - `random_signal_15m_sandbox`
  - `random_signal_15m_sandbox_no`
  - `btc_updown_15m_sandbox`
  - `btc_updown_15m_sandbox_no`
- Each profile pins strategy, market slug pattern, hours ahead, mode, Binance route, selected outcome side, bounded runtime if any, and strategy-specific knobs.
- The checked-in `btc_updown` profiles currently set `warmup_days = 14`.
- Fixed profile entrypoints are the preferred operator surface.
- The only supported runtime override on a fixed profile is `--run-secs`:

```bash
python live/runs/profiles/btc_updown_15m_live.py --run-secs 300
python live/runs/profile.py btc_updown_15m_live --print-profile
```

## Operator Notes

- `--run-secs` is the bounded-session switch for smoke and sandbox runs. Leave it unset for an unbounded process.
- `--outcome-side yes|no` selects the first or second Polymarket outcome token for ad hoc runners.
- On BTC up/down markets, `outcome_side=yes` maps to `Up` and `outcome_side=no` maps to `Down`.
- The node will stop cleanly when it runs out of pre-loaded windows and will log that a restart is required.
- Daily restart is the intended operating model for this phase. There is no automatic cross-day market extension yet.
- If an ended-window residual cannot be flattened because liquidity disappears or the remaining size is below minimum order size, the node carries it to resolution and logs the outcome once Polymarket resolves the market.
- A requested process stop waits for any carried residuals to resolve before final node shutdown.
- Run the sandbox validation sequence in `docs/live_testing_plan.md` before considering any real-order rehearsal.
- The detailed health-guard policy lives in [docs/live_health_guard_policy.md](/Users/noel/projects/trading_polymarket_nautilus/docs/live_health_guard_policy.md).

## Soak Runs

Use [soak.py](/Users/noel/projects/trading_polymarket_nautilus/live/soak.py) for the longer multi-hour sandbox sessions after the side-aware Polymarket quote update lands. It runs one or more profiles sequentially, captures stdout/stderr, and writes per-run plus batch summaries under `logs/soak/`.

```bash
# 4h soak on one sandbox profile
python live/soak.py random_signal_15m_sandbox --run-secs 14400 --label stage7_4h

# 8h soak on two profiles, continue even if the first fails
python live/soak.py random_signal_15m_sandbox btc_updown_15m_sandbox --run-secs 28800 --label stage7_8h --keep-going
```

Safety defaults:
- sandbox profiles only unless `--allow-live` is passed
- bounded runtime required unless `--allow-unbounded` is passed

Artifacts:
- `runner.log` — combined stdout/stderr for the profile run
- `profile.json` — resolved profile settings used for the run
- `summary.json` — exit code, duration, log path, and status
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
5. External settlement / redemption automation
   - Purpose: handle post-resolution reconciliation outside Nautilus.
   - Success: resolved residuals can be reconciled and redeemed by a separate process.
