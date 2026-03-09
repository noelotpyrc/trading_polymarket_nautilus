#!/usr/bin/env python3
"""Explore Polymarket WebSocket subscription behavior directly.

Connects to the Polymarket CLOB market WS (no Nautilus) and runs a series
of sub/unsub experiments to understand the actual protocol:

  Phase 1  — Initial connect with A only
  Phase 2  — Dynamic subscribe B  (WITH  custom_feature_enabled: true)
  Phase 3  — Dynamic subscribe C  (WITHOUT custom_feature_enabled)
  Phase 4  — Unsubscribe A  (B + C remain)
  Phase 5  — Unsubscribe B  (C alone remains)
  Phase 6  — Unsubscribe C  (0 remain — does WS close?)

Markets: 3 long-lived 2028 US election markets (expiry 2028-11-07).

Usage:
    python tests/live/explore_polymarket_ws.py
    python tests/live/explore_polymarket_ws.py --phase-secs 30
"""
import argparse
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone

import websockets

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Long-lived markets — expiry 2028-11-07 (safe from expiring mid-test)
MARKETS = {
    "A": {
        "name": "Rand Paul 2028 YES",
        "token_id": "56369772478534954338683665819559528414197495274302917800610633957542171787417",
    },
    "B": {
        "name": "Matt Gaetz 2028 YES",
        "token_id": "21684611233679565633387313395955873024927822291738934804105644401703825367322",
    },
    "C": {
        "name": "Eric Trump 2028 YES",
        "token_id": "57919459237478987490046512369779809927967587873586488155446781256559839871449",
    },
}

TOKEN_TO_LABEL = {m["token_id"]: label for label, m in MARKETS.items()}

# Shared state updated by receiver coroutine
tick_counts: dict[str, int] = defaultdict(int)
ws_closed = False


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


async def _receive_loop(ws) -> None:
    """Background task: parse all incoming messages and print them."""
    global ws_closed
    try:
        async for raw in ws:
            data = json.loads(raw)
            # Polymarket sends either a single dict or a list of dicts
            msgs = data if isinstance(data, list) else [data]
            for msg in msgs:
                # Skip heartbeats — they're noise
                if msg.get("event_type") == "heartbeat" or msg.get("type") == "heartbeat":
                    continue
                asset_id = msg.get("asset_id", "")
                label = TOKEN_TO_LABEL.get(asset_id) or (
                    f"UNKNOWN({asset_id[:12]})" if asset_id else "NO_ASSET_ID"
                )
                tick_counts[label] += 1
                # Print a compact summary: label, event type, price if present
                event = msg.get("event_type", msg.get("type", "?"))
                if label == "NO_ASSET_ID":
                    # Print full message to understand structure
                    print(f"  [{_ts()}] {label:30s} event={event} raw={json.dumps(msg)}")
                else:
                    price = msg.get("price", "")
                    side = msg.get("side", "")
                    size = msg.get("size", "")
                    extra = f" {side} p={price} sz={size}" if price else ""
                    print(f"  [{_ts()}] {label:30s} event={event}{extra}")
    except websockets.exceptions.ConnectionClosedOK:
        print(f"\n  [{_ts()}] *** WS closed cleanly (server side) ***")
        ws_closed = True
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"\n  [{_ts()}] *** WS closed with error: {e} ***")
        ws_closed = True
    except Exception as e:
        print(f"\n  [{_ts()}] *** Receiver error: {e} ***")
        ws_closed = True


async def _send(ws, msg: dict) -> None:
    raw = json.dumps(msg)
    print(f"\n>>> SEND [{_ts()}]: {raw}")
    await ws.send(raw)


async def _observe(secs: int, label: str) -> dict[str, int]:
    """Sleep and return per-market tick delta for the period."""
    print(f"    ... observing {secs}s for: {label}")
    before = dict(tick_counts)
    await asyncio.sleep(secs)
    delta = {k: tick_counts.get(k, 0) - before.get(k, 0) for k in set(tick_counts) | set(before)}
    delta = {k: v for k, v in delta.items() if v != 0}
    summary = dict(delta) if delta else "NONE"
    ws_state = "CLOSED" if ws_closed else "alive"
    print(f"    ticks in last {secs}s = {summary}  |  WS={ws_state}")
    return delta


async def main(phase_secs: int) -> None:
    print("=== Polymarket WS Subscription Explorer ===\n")
    for label, m in MARKETS.items():
        print(f"  {label}: {m['name']}")
        print(f"     token: {m['token_id'][:32]}...")
    print(f"\nPhase observation window: {phase_secs}s each\n")

    A = MARKETS["A"]["token_id"]
    B = MARKETS["B"]["token_id"]
    C = MARKETS["C"]["token_id"]

    async with websockets.connect(WS_URL) as ws:
        receiver = asyncio.create_task(_receive_loop(ws))

        # ── Phase 1: Initial connect with A ──────────────────────────────
        print("\n" + "═" * 60)
        print("PHASE 1: Initial connect — subscribe A only (initial msg)")
        print("═" * 60)
        await _send(ws, {"type": "market", "assets_ids": [A]})
        await _observe(phase_secs, "A ticks?")

        # ── Phase 2: Dynamic subscribe B WITH custom_feature_enabled ──────
        print("\n" + "═" * 60)
        print("PHASE 2: Dynamic subscribe B  (WITH custom_feature_enabled: true)")
        print("═" * 60)
        await _send(ws, {
            "assets_ids": [B],
            "operation": "subscribe",
            "custom_feature_enabled": True,
        })
        await _observe(phase_secs, "B ticks?  A still flowing?")

        # ── Phase 3: Dynamic subscribe C WITHOUT custom_feature_enabled ───
        print("\n" + "═" * 60)
        print("PHASE 3: Dynamic subscribe C  (WITHOUT custom_feature_enabled)")
        print("═" * 60)
        await _send(ws, {"assets_ids": [C], "operation": "subscribe"})
        await _observe(phase_secs, "C ticks?  A+B still flowing?")

        # ── Phase 4: Unsubscribe A (B + C remain) ─────────────────────────
        print("\n" + "═" * 60)
        print("PHASE 4: Unsubscribe A  (B + C remain)")
        print("═" * 60)
        await _send(ws, {"assets_ids": [A], "operation": "unsubscribe"})
        await _observe(phase_secs, "A stopped?  B+C still flowing?  WS alive?")

        # ── Phase 5: Unsubscribe B (C alone remains) ──────────────────────
        print("\n" + "═" * 60)
        print("PHASE 5: Unsubscribe B  (C alone remains)")
        print("═" * 60)
        await _send(ws, {"assets_ids": [B], "operation": "unsubscribe"})
        await _observe(phase_secs, "WS alive with just C?")

        # ── Phase 6: Unsubscribe C (0 subscriptions remain) ───────────────
        print("\n" + "═" * 60)
        print("PHASE 6: Unsubscribe C  (0 subscriptions remain)")
        print("═" * 60)
        await _send(ws, {"assets_ids": [C], "operation": "unsubscribe"})
        await _observe(15, "Does WS close immediately?")

        receiver.cancel()
        try:
            await receiver
        except asyncio.CancelledError:
            pass

    print("\n" + "═" * 60)
    print("SUMMARY")
    print("═" * 60)
    print(f"Total ticks by label: {dict(tick_counts)}")
    print(f"WS closed by server: {ws_closed}")
    print("""
Key questions answered:
  - Phase 1 ticks?          → initial subscribe works
  - Phase 2 B ticks?        → dynamic subscribe WITH flag works
  - Phase 3 C ticks?        → dynamic subscribe WITHOUT flag works (or broken)
  - Phase 4 A stopped?      → unsubscribe removes individual market
  - Phase 4 WS alive?       → unsubscribing 1-of-N keeps connection
  - Phase 6 WS closed?      → Polymarket closes WS on 0 subscriptions
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket WS subscription explorer")
    parser.add_argument("--phase-secs", type=int, default=20,
                        help="Seconds to observe per phase (default: 20)")
    args = parser.parse_args()
    asyncio.run(main(args.phase_secs))
