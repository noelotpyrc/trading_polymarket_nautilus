"""
Record Polymarket WebSocket market feed to JSONL files.

Logic:
    1. Compute the slug for the currently-running window from UTC time
       e.g. now=14:37 UTC → window_start=14:30 → slug=btc-updown-15m-{ts}
    2. Look up that exact slug on gamma API to confirm it exists and get token_ids
    3. Subscribe to those token_ids on the WS and record live data

Usage:
    python historical/fetch/record_ws.py --slug-pattern btc-updown-15m

Output:
    data/ws_recordings/<slug>.jsonl
    Each line: {"ts": <unix_ms>, "msg": <raw_message>}

Message types recorded:
    book             - full L2 snapshot (on subscribe + after each trade)
    last_trade_price - a trade executed
    best_bid_ask     - top-of-book update (requires custom_feature_enabled)
"""
import argparse
import asyncio
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

DISCOVERY_INTERVAL = 15    # seconds between checks
RELEVANT_EVENTS    = {"book", "last_trade_price", "best_bid_ask"}


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
    duration_sec  = duration_min * 60
    now           = int(time.time())
    window_start  = (now // duration_sec) * duration_sec
    window_end    = window_start + duration_sec
    slug          = f"{slug_pattern}-{window_start}"
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

    slug, window_start, window_end = result
    now = int(time.time())
    secs_remaining = window_end - now

    print(f"[{_now()}][discovery] current window: {slug}  ({secs_remaining}s remaining)")

    if now >= window_end:
        print(f"[{_now()}][discovery] window already ended, will pick up next window shortly")
        return None

    try:
        resp = requests.get(GAMMA_URL, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        print(f"[{_now()}][discovery] gamma API error: {e}")
        return None

    if not markets:
        print(f"[{_now()}][discovery] market not found yet: {slug}")
        return None

    m = markets[0]
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
        self.slug_pattern    = slug_pattern
        self._ws             = None
        self._files:      dict[str, object] = {}   # condition_id -> file
        self._slugs:      dict[str, str]    = {}   # condition_id -> slug
        self._tokens:     list[str]         = []   # all subscribed token_ids
        self._msg_counts: dict[str, int]    = {}   # condition_id -> count
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def _open_market(self, market: dict) -> list[str]:
        """Open file for market, return new token_ids to subscribe."""
        cid = market["condition_id"]
        if cid in self._files:
            return []
        path = DATA_DIR / f"{market['slug']}.jsonl"
        self._files[cid]      = open(path, "a")
        self._slugs[cid]      = market["slug"]
        self._msg_counts[cid] = 0
        new_tokens = [t for t in market["token_ids"] if t not in self._tokens]
        self._tokens.extend(new_tokens)
        print(f"[{_now()}][recorder] opened {market['slug']}  → {path.name}")
        return new_tokens

    def _write(self, cid: str, msg: dict) -> None:
        f = self._files.get(cid)
        if not f:
            return
        f.write(json.dumps({"ts": int(time.time() * 1000), "msg": msg}) + "\n")
        f.flush()
        self._msg_counts[cid] += 1

    def _close_all(self) -> None:
        for f in self._files.values():
            try:
                f.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _subscribe(self, token_ids: list[str]) -> None:
        if not self._ws or not token_ids:
            return
        await self._ws.send(json.dumps({
            "type":                   "market",
            "assets_ids":             token_ids,
            "custom_feature_enabled": True,
        }))
        print(f"[{_now()}][ws] subscribed to {len(token_ids)} token(s)")

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return
        items = payload if isinstance(payload, list) else [payload]
        for msg in items:
            event_type = msg.get("event_type", "unknown")
            cid        = msg.get("market", "")
            slug       = self._slugs.get(cid, cid[:12] + "..." if cid else "?")

            if event_type not in RELEVANT_EVENTS:
                print(f"[{_now()}][ws] recv {event_type:<20} {slug}  [skipped]")
                continue

            if cid and cid in self._files:
                self._write(cid, msg)
                print(f"[{_now()}][ws] recv {event_type:<20} {slug}  [saved #{self._msg_counts[cid]}]")
            else:
                print(f"[{_now()}][ws] recv {event_type:<20} {slug}  [no file]")

    # ------------------------------------------------------------------
    # Discovery loop
    # ------------------------------------------------------------------

    async def _discovery_loop(self) -> None:
        while True:
            market = lookup_current_market(self.slug_pattern)
            if market:
                new_tokens = self._open_market(market)
                if new_tokens:
                    await self._subscribe(new_tokens)
            await asyncio.sleep(DISCOVERY_INTERVAL)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            if self._files:
                counts = {self._slugs[cid]: n for cid, n in self._msg_counts.items()}
                print(f"[{_now()}][heartbeat] tracking {len(self._files)} market(s) | msgs: {counts}")
            else:
                print(f"[{_now()}][heartbeat] waiting for active market window")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        print(f"[{_now()}][recorder] starting | pattern={self.slug_pattern!r} | poll={DISCOVERY_INTERVAL}s")
        try:
            while True:
                try:
                    async with websockets.connect(
                        WS_URL, ping_interval=10, ping_timeout=30, close_timeout=5,
                    ) as ws:
                        self._ws = ws
                        print(f"[{_now()}][ws] connected")

                        # Resubscribe known tokens on reconnect
                        if self._tokens:
                            await self._subscribe(self._tokens)

                        discovery = asyncio.create_task(self._discovery_loop())
                        heartbeat = asyncio.create_task(self._heartbeat_loop())

                        async for raw in ws:
                            await self._on_message(raw)

                        discovery.cancel()
                        heartbeat.cancel()

                except (websockets.ConnectionClosed, OSError) as e:
                    print(f"[{_now()}][ws] disconnected ({e}), reconnecting in 5s...")
                    await asyncio.sleep(5)
        finally:
            self._close_all()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Record Polymarket WS feed to JSONL")
    p.add_argument("--slug-pattern", required=True,
                   help="e.g. btc-updown-15m or btc-updown-5m")
    args = p.parse_args()
    asyncio.run(WsRecorder(args.slug_pattern).run())


if __name__ == "__main__":
    main()
