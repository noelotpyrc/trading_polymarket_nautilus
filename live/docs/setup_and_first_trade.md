# Setup & First Trade — Notes

Documenting what was discovered getting from zero to a live filled order on Polymarket.

---

## Environment

- Python 3.12, managed with `uv`
- Key libraries: `py-clob-client`, `web3`, `python-dotenv`
- Wallet: EOA (signature type 0), generated via `eth_account`
- Network: Polygon mainnet (chain ID 137)

---

## Setup Gotchas

### 1. `update_balance_allowance` is NOT an on-chain approval

`ClobClient.update_balance_allowance()` is just a GET request that tells Polymarket's API to refresh its record of your balance. It does **not** issue an ERC20 `approve()` transaction.

The actual on-chain approval must be done manually using `web3.py`:

```python
usdc.functions.approve(exchange_contract, MAX_UINT256).build_transaction(...)
```

Must approve all three Polymarket exchange contracts:

| Contract | Address |
|---|---|
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Neg Risk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |
| Neg Risk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |

After the on-chain approvals, call `update_balance_allowance` to sync Polymarket's API record.

### 2. Polygon requires POA middleware in web3.py

Polygon is a POA chain. Without injecting the middleware, `build_transaction` crashes on `extraData` length validation:

```python
from web3.middleware import ExtraDataToPOAMiddleware
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
```

### 3. `polygon-rpc.com` was unreachable

The commonly referenced RPC endpoint `https://polygon-rpc.com` failed to connect. Working alternatives:

- `https://polygon-bor-rpc.publicnode.com` ✅ (currently used)
- `https://1rpc.io/matic` ✅

### 4. API credential env var naming

`py-clob-client` uses `api_passphrase`, but `PolymarketExecClientConfig` (nautilus) sources from `POLYMARKET_PASSPHRASE` (no `API_` prefix). Keep this in mind if switching to the nautilus trading node later.

---

## Order Placement Gotchas

### 1. `post_order` must receive `OrderType.FOK` for market orders

`create_market_order` creates a FOK (Fill or Kill) order by default, but `post_order` defaults to `GTC`. Mismatch causes the precision error below. Always pass explicitly:

```python
resp = client.post_order(order, OrderType.FOK)
```

### 2. `tick_size` must be a string, not a float

`PartialCreateOrderOptions(tick_size=0.01)` raises `KeyError`. Must be:

```python
PartialCreateOrderOptions(tick_size='0.01')
```

Valid values: `'0.1'`, `'0.01'`, `'0.001'`, `'0.0001'`

### 3. Platform-wide $5 minimum order size

All Polymarket markets have `orderMinSize: 5`. Orders below $5 USDC are rejected by the API.

### 4. Market lookup: event slug ≠ market slug

The Polymarket URL slug (e.g. `bitcoin-above-on-march-1`) is an **event** slug containing multiple markets. To get individual market slugs and token IDs:

```python
import requests
resp = requests.get('https://gamma-api.polymarket.com/events', params={'slug': 'bitcoin-above-on-march-1'})
markets = resp.json()[0]['markets']
# Each market has: slug, conditionId, clobTokenIds, outcomePrices
```

`clobTokenIds` is a JSON string with `[yes_token_id, no_token_id]`.

---

## Working Market Buy Order (minimal)

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, PartialCreateOrderOptions, OrderType

client = ClobClient(host='https://clob.polymarket.com', key=PRIVATE_KEY, chain_id=137, signature_type=0)
client.set_api_creds(ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE))

order = client.create_market_order(
    MarketOrderArgs(token_id=YES_TOKEN_ID, amount=5.0, side='BUY'),
    options=PartialCreateOrderOptions(tick_size='0.01'),
)
resp = client.post_order(order, OrderType.FOK)
# resp = {'success': True, 'status': 'matched', 'orderID': '0x...', 'takingAmount': '...', 'makingAmount': '...'}
```

---

## First Live Trade

- **Market**: `bitcoin-above-66k-on-march-1`
- **Side**: BUY YES
- **Amount**: $5 USDC
- **Price**: ~$0.76 (73.5% implied probability at time of order)
- **Filled**: ~6.58 YES shares
- **Status**: `matched` (FOK, fully filled)

---

## Nautilus vs py-clob-client for Execution

Nautilus (`PolymarketLiveExecClientFactory`) requires a full `TradingNode` with strategies wired up — it's a production trading framework, not a standalone order client. Internally it still uses `ClobClient` from `py-clob-client`.

For ad-hoc scripts and feasibility testing, `py-clob-client` directly is the right tool. Nautilus is the right path for production strategy execution.
