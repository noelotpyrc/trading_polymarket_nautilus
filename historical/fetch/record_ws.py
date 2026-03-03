"""
Record Polymarket WebSocket `book` events to compact gzip JSONL.

Logic (one WS connection per window):
    1. Compute current window slug from UTC time
    2. Look up market on gamma API to get token_ids
    3. Connect WS, subscribe, record book events until window ends
    4. Close file (writing gzip EOS), wait for next window, repeat

Usage:
    python historical/fetch/record_ws.py --slug-pattern btc-updown-15m

Output:
    data/ws_recordings/<slug>.jsonl.gz  (gzip-compressed, ~1 MB per 15-min window)
    Line 0 (metadata): {"meta": {"slug": ..., "condition_id": ..., "assets": [token_id_0, token_id_1]}}
    Line N (book):     {"t": <unix_ms>, "a": <0|1>, "b": [[price, size], ...], "s": [[price, size], ...]}

See record_ws_full.py to record all event types (book + last_trade_price + best_bid_ask).
"""
import argparse
import asyncio
import gzip
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import websockets

PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
DATA_DIR  = PROJECT_ROOT / "data" / "ws_recordings"

DISCOVERY_INTERVAL = 15   # seconds between market-existence checks


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Window / slug computation
# ---------------------------------------------------------------------------

def _duration_from_pattern(slug_pattern: str) -> int | None:
    """Extract duration in minutes from pattern like 'btc-updown-15m' → 15."""
    m = re.search(r"(\d+)m", slug_pattern)
    return int(m.group(1)) if m else None


def _current_window_slug(slug_pattern: str) -> tuple[str, int, int] | None:
    """
    Compute (slug, window_start, window_end) for the currently-running window.
    Returns None if duration cannot be parsed.
    """
    duration_min = _duration_from_pattern(slug_pattern)
    if not duration_min:
        return None
    duration_sec = duration_min * 60
    now          = int(time.time())
    window_start = (now // duration_sec) * duration_sec
    window_end   = window_start + duration_sec
    slug         = f"{slug_pattern}-{window_start}"
    return slug, window_start, window_end


# ---------------------------------------------------------------------------
# Market lookup
# ---------------------------------------------------------------------------

def _parse_token_ids(raw: str) -> list[str]:
    try:
        return json.loads(raw)
    except Exception:
        return []


def lookup_current_market(slug_pattern: str) -> dict | None:
    """
    Look up the market for the currently-running window.
    Returns market dict or None if not found / not yet created.
    """
    result = _current_window_slug(slug_pattern)
    if not result:
        print(f"[{_now()}][discovery] cannot parse duration from '{slug_pattern}'")
        return None

    slug, _, window_end = result
    now            = int(time.time())
    secs_remaining = window_end - now

    print(f"[{_now()}][discovery] current window: {slug}  ({secs_remaining}s remaining)")

    if now >= window_end:
        print(f"[{_now()}][discovery] window already ended")
        return None

    try:
        resp = requests.get(GAMMA_URL, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        print(f"[{_now()}][discovery] gamma API error: {e}")
        return None

    if not markets:
        print(f"[{_now()}][discovery] not found: {slug}")
        return None

    m         = markets[0]
    token_ids = _parse_token_ids(m.get("clobTokenIds", "[]"))
    if not token_ids:
        print(f"[{_now()}][discovery] no token_ids for {slug}")
        return None

    print(f"[{_now()}][discovery] confirmed: {slug}  tokens={len(token_ids)}")
    return {
        "slug":         slug,
        "condition_id": m.get("conditionId", ""),
        "token_ids":    token_ids,
        "window_end":   window_end,
    }


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class WsRecorder:
    def __init__(self, slug_pattern: str):
        self.slug_pattern = slug_pattern
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def _record_window(self, market: dict) -> None:
        """
        Open a fresh WS connection for this window, subscribe, record until window ends.
        Reconnects within the window if the connection drops.
        """
        slug       = market["slug"]
        token_ids  = market["token_ids"]
        window_end = market["window_end"]
        asset_idx  = {t: i for i, t in enumerate(token_ids)}
        count      = 0

        path   = DATA_DIR / f"{slug}.jsonl.gz"
        is_new = not path.exists()

        with gzip.open(path, "at") as gz:
            if is_new:
                gz.write(json.dumps(
                    {"meta": {"slug": slug, "condition_id": market["condition_id"], "assets": token_ids}},
                    separators=(",", ":"),
                ) + "\n")

            print(f"[{_now()}][recording] {slug}  ({window_end - int(time.time())}s remaining)")

            while int(time.time()) < window_end:
                try:
                    async with websockets.connect(
                        WS_URL, ping_interval=10, ping_timeout=30, close_timeout=5,
                    ) as ws:
                        await ws.send(json.dumps({"type": "market", "assets_ids": token_ids}))
                        print(f"[{_now()}][ws] subscribed  {[t[:12] for t in token_ids]}")

                        async for raw in ws:
                            if int(time.time()) >= window_end:
                                break
                            try:
                                payload = json.loads(raw)
                            except Exception:
                                continue
                            items = payload if isinstance(payload, list) else [payload]
                            for msg in items:
                                if msg.get("event_type") != "book":
                                    continue
                                a = asset_idx.get(msg.get("asset_id", ""), -1)
                                if a == -1:
                                    continue   # message for a different/unknown token
                                bids = [[float(x["price"]), float(x["size"])] for x in msg.get("bids", [])]
                                asks = [[float(x["price"]), float(x["size"])] for x in msg.get("asks", [])]
                                gz.write(json.dumps(
                                    {"t": int(msg["timestamp"]), "a": a, "b": bids, "s": asks},
                                    separators=(",", ":"),
                                ) + "\n")
                                gz.flush()
                                count += 1
                                print(f"[{_now()}][ws] book  {slug}  #{count}")

                except (websockets.ConnectionClosed, OSError) as e:
                    if int(time.time()) < window_end:
                        print(f"[{_now()}][ws] disconnected ({e}), reconnecting in 3s...")
                        await asyncio.sleep(3)

        print(f"[{_now()}][recording] done  {slug}  ({count} records)")

    async def run(self) -> None:
        print(f"[{_now()}][recorder] starting | pattern={self.slug_pattern!r}")
        while True:
            market = lookup_current_market(self.slug_pattern)
            if not market:
                await asyncio.sleep(DISCOVERY_INTERVAL)
                continue

            await self._record_window(market)

            # Wait for next window to start (with a small buffer)
            gap = market["window_end"] - int(time.time()) + 2
            if gap > 0:
                print(f"[{_now()}][recorder] waiting {gap}s for next window...")
                await asyncio.sleep(gap)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Record Polymarket book snapshots to compact gzip JSONL")
    p.add_argument("--slug-pattern", required=True,
                   help="e.g. btc-updown-15m or btc-updown-5m")
    args = p.parse_args()
    asyncio.run(WsRecorder(args.slug_pattern).run())


if __name__ == "__main__":
    main()
