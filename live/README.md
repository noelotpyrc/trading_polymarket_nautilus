# Live Trading

Scripts for real-time trading on Polymarket. Uses authenticated APIs — requires a funded EOA wallet on Polygon.

---

## One-time Setup

### 1. Install dependencies

```bash
pip install -e .
```

### 2. Generate a wallet

```bash
python live/setup/generate_wallet.py
```

Creates a new EOA and writes `PRIVATE_KEY` and `WALLET_ADDRESS` to `.env`. Run once only.

### 3. Derive API credentials *(no funding needed)*

```bash
python live/setup/init_trading.py
```

Derives L2 API credentials from your private key and saves them to `.env`. Pure cryptographic signing — no on-chain transaction, no gas required.

### 4. Fund your wallet on Polygon

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
setup/
  generate_wallet.py   # Step 1: create EOA
  init_trading.py      # Step 2: derive API creds (no funding needed)
  set_allowances.py    # Step 3: approve USDC contracts (needs POL for gas)
  sweep.py             # Sweep excess USDC to safe wallet (run before/after sessions)
```
