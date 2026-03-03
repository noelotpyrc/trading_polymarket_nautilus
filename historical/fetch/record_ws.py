"""
Record Polymarket WebSocket market feed to JSONL files.

Only records during the active 15-minute trading window.
One file per market slug.

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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import websockets

PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
DATA_DIR = PROJECT_ROOT / "data" / "ws_recordings"

DISCOVERY_INTERVAL = 60     # seconds between gamma API polls
SUBSCRIBE_BUFFER_SECS = 120 # subscribe this many seconds before window start
KEEP_BUFFER_SECS = 60       # keep recording this many seconds after window end
RELEVANT_EVENTS = {"book", "last_trade_price", "best_bid_ask"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_token_ids(raw: str) -> list[str]:
    try:
        return json.loads(raw)
    except Exception:
        return []


def _parse_slug_timing(slug: str) -> tuple[int, int] | None:
    """Parse 'btc-updown-15m-1772593200' → (trading_start_ts, duration_min)."""
    parts = slug.split("-")
    try:
        ts = int(parts[-1])
        for p in parts:
            if p.endswith("m") and p[:-1].isdigit():
                return (ts, int(p[:-1]))
    except (ValueError, IndexError):
        pass
    return None


def _is_in_trading_window(slug: str) -> bool:
    """True if now falls within the market's trading window (with buffer)."""
    timing = _parse_slug_timing(slug)
    if not timing:
        return False
    trading_start, duration_min = timing
    trading_end = trading_start + duration_min * 60
    now = int(time.time())
    return (trading_start - SUBSCRIBE_BUFFER_SECS) <= now <= (trading_end + KEEP_BUFFER_SECS)


def find_trading_markets(slug_pattern: str) -> list[dict]:
    """Return markets whose slug matches pattern AND are currently in trading window."""
    try:
        resp = requests.get(
            GAMMA_URL,
            params={"active": "true", "limit": 100, "order": "id", "ascending": "false"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[{_now()}][discovery] gamma API error: {e}")
        return []

    now = int(time.time())
    candidates = [m for m in resp.json() if slug_pattern in m.get("slug", "")]
    print(f"[{_now()}][discovery] found {len(candidates)} slug matches for '{slug_pattern}'")

    results = []
    for m in candidates:
        slug = m.get("slug", "")
        timing = _parse_slug_timing(slug)
        if not timing:
            print(f"[{_now()}][discovery]   {slug}: cannot parse timing, skipping")
            continue
        trading_start, duration_min = timing
        trading_end = trading_start + duration_min * 60
        secs_to_start = trading_start - now
        secs_to_end   = trading_end - now
        in_window = (trading_start - SUBSCRIBE_BUFFER_SECS) <= now <= (trading_end + KEEP_BUFFER_SECS)

        if in_window:
            status = f"IN WINDOW (ends in {int(secs_to_end)}s)"
        elif secs_to_start > 0:
            status = f"starts in {int(secs_to_start)}s"
        else:
            status = f"expired {int(-secs_to_end)}s ago"

        print(f"[{_now()}][discovery]   {slug}: {status}")

        if not in_window:
            continue
        token_ids = _parse_token_ids(m.get("clobTokenIds", "[]"))
        if not token_ids:
            print(f"[{_now()}][discovery]   {slug}: no token_ids, skipping")
            continue
        results.append({
            "slug": slug,
            "condition_id": m.get("conditionId", ""),
            "token_ids": token_ids,
        })
    return results


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class WsRecorder:
    def __init__(self, slug_pattern: str):
        self.slug_pattern = slug_pattern
        self._ws: websockets.WebSocketClientProtocol | None = None

        # condition_id -> open file handle
        self._files: dict[str, object] = {}
        # condition_id -> slug (for display/filename)
        self._slugs: dict[str, str] = {}
        # all token_ids subscribed (needed for WS resubscription)
        self._tokens: list[str] = []
        # condition_id -> messages written count
        self._msg_counts: dict[str, int] = {}

        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def _open_market(self, slug: str, condition_id: str, token_ids: list[str]) -> list[str]:
        """Register a market, open its file, return new token_ids to subscribe."""
        if condition_id in self._files:
            return []
        path = DATA_DIR / f"{slug}.jsonl"
        self._files[condition_id] = open(path, "a")
        self._slugs[condition_id] = slug
        self._msg_counts[condition_id] = 0
        new_tokens = [t for t in token_ids if t not in self._tokens]
        self._tokens.extend(new_tokens)
        print(f"[{_now()}][recorder] tracking {slug}  → {path.name}")
        return new_tokens

    def _write(self, condition_id: str, msg: dict) -> None:
        f = self._files.get(condition_id)
        if f is None:
            return
        f.write(json.dumps({"ts": int(time.time() * 1000), "msg": msg}) + "\n")
        f.flush()
        self._msg_counts[condition_id] += 1

    def _close_all(self) -> None:
        for f in self._files.values():
            try:
                f.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # WebSocket helpers
    # ------------------------------------------------------------------

    async def _subscribe(self, token_ids: list[str]) -> None:
        if self._ws is None or not token_ids:
            return
        msg = {
            "type": "market",
            "assets_ids": token_ids,
            "custom_feature_enabled": True,
        }
        await self._ws.send(json.dumps(msg))
        print(f"[{_now()}][ws] subscribed to {len(token_ids)} token(s)")

    async def _resubscribe_all(self) -> None:
        if self._tokens:
            await self._subscribe(self._tokens)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return

        items = payload if isinstance(payload, list) else [payload]
        for msg in items:
            event_type = msg.get("event_type", "unknown")
            condition_id = msg.get("market", "")
            slug = self._slugs.get(condition_id, condition_id[:12] + "..." if condition_id else "?")

            if event_type not in RELEVANT_EVENTS:
                print(f"[{_now()}][ws] recv {event_type:<20} {slug}  [skipped]")
                continue

            if condition_id and condition_id in self._files:
                self._write(condition_id, msg)
                count = self._msg_counts[condition_id]
                print(f"[{_now()}][ws] recv {event_type:<20} {slug}  [saved #{count}]")
            else:
                print(f"[{_now()}][ws] recv {event_type:<20} {slug}  [no file]")

    # ------------------------------------------------------------------
    # Discovery loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            if self._files:
                counts = {self._slugs[cid]: n for cid, n in self._msg_counts.items()}
                print(f"[{_now()}][heartbeat] tracking {len(self._files)} market(s) | msgs: {counts}")
            else:
                print(f"[{_now()}][heartbeat] waiting — no markets in trading window yet")

    async def _discovery_loop(self) -> None:
        while True:
            markets = find_trading_markets(self.slug_pattern)
            new_tokens = []
            for m in markets:
                new = self._open_market(m["slug"], m["condition_id"], m["token_ids"])
                new_tokens.extend(new)
            if new_tokens:
                await self._subscribe(new_tokens)
            await asyncio.sleep(DISCOVERY_INTERVAL)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        print(f"[{_now()}][recorder] starting | slug_pattern={self.slug_pattern!r}")
        print(f"[{_now()}][recorder] subscribe buffer={SUBSCRIBE_BUFFER_SECS}s | keep buffer={KEEP_BUFFER_SECS}s | poll interval={DISCOVERY_INTERVAL}s")
        try:
            while True:
                try:
                    async with websockets.connect(
                        WS_URL,
                        ping_interval=10,
                        ping_timeout=30,
                        close_timeout=5,
                    ) as ws:
                        self._ws = ws
                        print(f"[{_now()}][ws] connected to {WS_URL}")

                        await self._resubscribe_all()
                        discovery  = asyncio.create_task(self._discovery_loop())
                        heartbeat  = asyncio.create_task(self._heartbeat_loop())

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
                   help="Slug substring to match (e.g. btc-updown-15m)")
    args = p.parse_args()
    asyncio.run(WsRecorder(args.slug_pattern).run())


if __name__ == "__main__":
    main()
