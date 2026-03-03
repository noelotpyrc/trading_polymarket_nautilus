"""
Record Polymarket WebSocket market feed (book deltas + trades) to JSONL files.

Usage:
    # Record all active markets matching a slug pattern (auto-discovers new epochs)
    python historical/fetch/record_ws.py --slug-pattern btc-updown-15m

    # Record specific token IDs
    python historical/fetch/record_ws.py --token-ids <id1> <id2>

Output:
    data/ws_recordings/<slug>/<token_id>.jsonl
    Each line: {"ts": <unix_ms>, "msg": <raw_message>}

Message types recorded:
    book             - full L2 snapshot (on subscribe + periodic)
    price_change     - L2 delta (individual level update)
    last_trade_price - a trade executed
    tick_size_change - tick size changed
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import requests
import websockets

PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
DATA_DIR = PROJECT_ROOT / "data" / "ws_recordings"
DISCOVERY_INTERVAL = 60  # seconds between gamma API polls


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def _parse_token_ids(raw: str) -> list[str]:
    """Parse clobTokenIds field which looks like '["id1","id2"]'."""
    try:
        return json.loads(raw)
    except Exception:
        return []


def find_active_markets(slug_pattern: str) -> list[dict]:
    """Return active markets whose slug contains slug_pattern."""
    try:
        resp = requests.get(
            GAMMA_URL,
            # Sort newest-first: short-lived markets have high IDs
            params={"active": "true", "limit": 100, "order": "id", "ascending": "false"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[discovery] gamma API error: {e}")
        return []

    results = []
    for m in resp.json():
        if slug_pattern not in m.get("slug", ""):
            continue
        token_ids = _parse_token_ids(m.get("clobTokenIds", "[]"))
        if not token_ids:
            continue
        results.append({
            "slug": m["slug"],
            "condition_id": m.get("conditionId", ""),
            "token_ids": token_ids,
            "end_date": m.get("endDate", ""),
        })
    return results


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class WsRecorder:
    def __init__(self, slug_pattern: str | None, explicit_tokens: list[tuple[str, str]]):
        """
        Parameters
        ----------
        slug_pattern      : discover markets whose slug contains this string
        explicit_tokens   : list of (token_id, label) to subscribe to directly
        """
        self.slug_pattern = slug_pattern
        self._ws: websockets.WebSocketClientProtocol | None = None

        # token_id -> open file handle
        self._files: dict[str, object] = {}
        # token_id -> slug label (for directory naming)
        self._labels: dict[str, str] = {}

        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Pre-register explicitly requested tokens
        for token_id, label in explicit_tokens:
            self._open_file(token_id, label)

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def _open_file(self, token_id: str, slug: str) -> None:
        if token_id in self._files:
            return
        out_dir = DATA_DIR / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{token_id}.jsonl"
        self._files[token_id] = open(path, "a")
        self._labels[token_id] = slug
        print(f"[recorder] tracking {slug} / {token_id[:20]}...  → {path}")

    def _write(self, token_id: str, msg: dict) -> None:
        f = self._files.get(token_id)
        if f is None:
            return
        f.write(json.dumps({"ts": int(time.time() * 1000), "msg": msg}) + "\n")
        f.flush()

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
        msg = {"type": "market", "assets_ids": token_ids}
        await self._ws.send(json.dumps(msg))
        print(f"[ws] subscribed to {len(token_ids)} token(s)")

    async def _resubscribe_all(self) -> None:
        await self._subscribe(list(self._files.keys()))

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def _route_asset_id(self, msg: dict) -> str | None:
        """Extract asset_id (token_id) from any message type."""
        # book / last_trade_price / tick_size_change have top-level asset_id
        if "asset_id" in msg:
            return msg["asset_id"]
        # price_change: asset_id is per price_change item
        changes = msg.get("price_changes", [])
        if changes and "asset_id" in changes[0]:
            return changes[0]["asset_id"]
        return None

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return

        # WS can send a single object or a list on initial snapshot
        items = payload if isinstance(payload, list) else [payload]
        for msg in items:
            asset_id = self._route_asset_id(msg)
            if asset_id and asset_id in self._files:
                self._write(asset_id, msg)

    # ------------------------------------------------------------------
    # Discovery loop
    # ------------------------------------------------------------------

    async def _discovery_loop(self) -> None:
        while True:
            if self.slug_pattern:
                markets = find_active_markets(self.slug_pattern)
                new_tokens = []
                for m in markets:
                    for token_id in m["token_ids"]:
                        if token_id not in self._files:
                            self._open_file(token_id, m["slug"])
                            new_tokens.append(token_id)
                if new_tokens:
                    await self._subscribe(new_tokens)
            await asyncio.sleep(DISCOVERY_INTERVAL)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        print(f"[recorder] starting | slug_pattern={self.slug_pattern!r}")
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
                        print(f"[ws] connected to {WS_URL}")

                        # Subscribe to already-known tokens on reconnect
                        await self._resubscribe_all()

                        # Start discovery in background
                        discovery = asyncio.create_task(self._discovery_loop())

                        async for raw in ws:
                            await self._on_message(raw)

                        discovery.cancel()

                except (websockets.ConnectionClosed, OSError) as e:
                    print(f"[ws] disconnected ({e}), reconnecting in 5s...")
                    await asyncio.sleep(5)
        finally:
            self._close_all()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record Polymarket WS feed to JSONL")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--slug-pattern", help="Slug substring to auto-discover (e.g. btc-updown-15m)")
    group.add_argument("--token-ids", nargs="+", help="Explicit token IDs to record")
    p.add_argument("--label", default="manual", help="Directory label when using --token-ids")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    explicit: list[tuple[str, str]] = []
    if args.token_ids:
        explicit = [(tid, args.label) for tid in args.token_ids]

    recorder = WsRecorder(
        slug_pattern=args.slug_pattern if not args.token_ids else None,
        explicit_tokens=explicit,
    )
    asyncio.run(recorder.run())


if __name__ == "__main__":
    main()
