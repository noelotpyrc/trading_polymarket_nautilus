#!/usr/bin/env python3
"""Explore Polymarket WS subscription behavior through the Nautilus adapter.

Tests two window-roll patterns to identify the sandbox feed bug and verify the fix.

Phases (each --phase-secs seconds):
  Phase 1  — Subscribe A only (initial connection, verify baseline ticks)
  Phase 2  — BROKEN ROLL A→B: unsubscribe(A) first, subscribe(B) second
               → B should get NO ticks (WS disconnects, bug reproduced)
  Phase 3  — Observe: confirm B=0 (bug confirmed)
  Phase 4  — FIXED ROLL B→C: subscribe(C) FIRST, unsubscribe(B) second
               → C should get ticks (WS stays alive, fix verified)
  Phase 5  — Observe: confirm C>0 (fix works)
  Phase 6  — Unsubscribe C (cleanup)

Key question: does the fixed order (sub NEW before unsub OLD) prevent WS disconnect?

Markets: 3 long-lived 2028 election markets (won't expire mid-test).

Usage:
    python tests/live/explore_nautilus_ws.py
    python tests/live/explore_nautilus_ws.py --phase-secs 30
"""
import argparse
import os
import sys
import threading

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.adapters.polymarket import (
    PolymarketDataClientConfig,
    PolymarketLiveDataClientFactory,
)
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId

# Same 3 long-lived 2028 election markets as explore_polymarket_ws.py
# Format: condition_id-token_id.POLYMARKET
MARKETS = {
    "A": {
        "name": "Rand Paul 2028 YES",
        "instrument_id": (
            "0xd10bc768ede58b53ed400594240b0a0603134a32dab89ec823a18759cbc180ca"
            "-56369772478534954338683665819559528414197495274302917800610633957542171787417"
            ".POLYMARKET"
        ),
    },
    "B": {
        "name": "Matt Gaetz 2028 YES",
        "instrument_id": (
            "0xf2cea45ec282af4f302d2ab85ede73678cd692ebf8c3ab6d52bfa5e19f44c553"
            "-21684611233679565633387313395955873024927822291738934804105644401703825367322"
            ".POLYMARKET"
        ),
    },
    "C": {
        "name": "Eric Trump 2028 YES",
        "instrument_id": (
            "0xca68152902c3581ab42feb04921b8b1cd3c4e80d06f255ae077259fca0bba15c"
            "-57919459237478987490046512369779809927967587873586488155446781256559839871449"
            ".POLYMARKET"
        ),
    },
}


class NautilusWsExplorerConfig(ActorConfig, frozen=True):
    instrument_id_a: str
    instrument_id_b: str
    instrument_id_c: str
    phase_secs: int = 20


class NautilusWsExplorer(Actor):
    """Tests the sandbox roll pattern: unsubscribe(OLD) + subscribe(NEW) in same callback.

    This directly mirrors btc_updown._on_window_end, where both calls are dispatched
    as concurrent async tasks within a single synchronous callback invocation.
    """

    LABELS = ["A", "B", "C"]

    def __init__(self, config: NautilusWsExplorerConfig):
        super().__init__(config)
        self._instruments = [
            InstrumentId.from_str(config.instrument_id_a),
            InstrumentId.from_str(config.instrument_id_b),
            InstrumentId.from_str(config.instrument_id_c),
        ]
        self._phase_ns = config.phase_secs * 1_000_000_000
        self._tick_counts: dict[str, int] = {lbl: 0 for lbl in self.LABELS}
        self._phase = 0

    def _label(self, instrument_id: InstrumentId) -> str:
        for i, inst in enumerate(self._instruments):
            if inst == instrument_id:
                return self.LABELS[i]
        return "UNKNOWN"

    def _counts_str(self) -> str:
        return "  ".join(f"{lbl}={self._tick_counts[lbl]}" for lbl in self.LABELS)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def on_start(self):
        self.log.info("═" * 60)
        self.log.info("PHASE 1: Subscribe A only (initial connection)")
        self.log.info("═" * 60)
        self.subscribe_quote_ticks(self._instruments[0])
        self._set_next_phase_alert()

    def on_stop(self):
        self.log.info(f"Final tick counts: {self._counts_str()}")

    # ── Phase transitions ─────────────────────────────────────────────────

    def _set_next_phase_alert(self):
        self._phase += 1
        self.clock.set_time_alert_ns(
            name=f"phase_{self._phase}",
            alert_time_ns=self.clock.timestamp_ns() + self._phase_ns,
            callback=self._on_phase,
        )

    def _on_phase(self, event) -> None:
        self.log.info(f"  → ticks so far: {self._counts_str()}")
        self.log.info("═" * 60)

        if self._phase == 1:
            # BROKEN pattern (old btc_updown order): unsub OLD first, sub NEW second.
            # The WS client disconnects when subscription count hits 0 after removing A.
            # B's subscribe is sent to a disconnecting socket → B never gets ticks.
            self.log.info("PHASE 2: BROKEN ROLL A→B (unsub A first, sub B second)")
            self.log.info("  Expected: B=0 ticks (WS disconnects, subscribe sent to dead socket)")
            self.log.info("═" * 60)
            self.unsubscribe_quote_ticks(self._instruments[0])  # OLD first — WS will disconnect
            self.subscribe_quote_ticks(self._instruments[1])    # NEW second — too late
            self._set_next_phase_alert()

        elif self._phase == 2:
            self.log.info("PHASE 3: Observe — did B receive QuoteTicks after broken roll?")
            self.log.info(f"  B={self._tick_counts['B']} {'(bug confirmed ✓)' if self._tick_counts['B'] == 0 else '(unexpected — B got ticks)'}")
            self.log.info("═" * 60)
            self._set_next_phase_alert()

        elif self._phase == 3:
            # FIXED pattern (new btc_updown order): sub NEW first, unsub OLD second.
            # By the time unsubscribe(B) runs, C is already in _client_subscriptions.
            # subscription count stays > 0 → no disconnect → C gets live dynamic subscribe.
            # NOTE: after the broken roll, WS is disconnected (_clients[0]=None).
            # subscribe(C) will trigger _connect_client → fresh connection → _subscribe_all.
            # This tests that the fixed order works even when starting from disconnected state.
            self.log.info("PHASE 4: FIXED ROLL B→C (sub C first, unsub B second)")
            self.log.info("  Expected: C>0 ticks (WS preserved or reconnected cleanly)")
            self.log.info("═" * 60)
            self.subscribe_quote_ticks(self._instruments[2])    # NEW first
            self.unsubscribe_quote_ticks(self._instruments[1])  # OLD second
            self._set_next_phase_alert()

        elif self._phase == 4:
            self.log.info("PHASE 5: Observe — did C receive QuoteTicks after fixed roll?")
            self.log.info(f"  C={self._tick_counts['C']} {'(fix works ✓)' if self._tick_counts['C'] > 0 else '(still broken ✗)'}")
            self.log.info("═" * 60)
            self._set_next_phase_alert()

        elif self._phase == 5:
            self.log.info("PHASE 6: Unsubscribe C (cleanup)")
            self.log.info("═" * 60)
            self.unsubscribe_quote_ticks(self._instruments[2])
            self._set_next_phase_alert()

        elif self._phase == 6:
            self.log.info("All phases complete.")
            self.log.info(f"Final counts: {self._counts_str()}")
            self.log.info("─" * 60)
            b_zero = self._tick_counts["B"] == 0
            c_ok = self._tick_counts["C"] > 0
            self.log.info(f"  Broken roll A→B:  B=0 (bug reproduced): {'YES ✓' if b_zero else 'NO ✗'}")
            self.log.info(f"  Fixed  roll B→C:  C>0 (fix works):      {'YES ✓' if c_ok else 'NO ✗'}")

    # ── Data handler ──────────────────────────────────────────────────────

    def on_quote_tick(self, tick: QuoteTick) -> None:
        lbl = self._label(tick.instrument_id)
        self._tick_counts[lbl] += 1
        mid = (float(tick.bid_price) + float(tick.ask_price)) / 2
        self.log.info(
            f"QuoteTick [{lbl}] mid={mid:.4f}  counts={self._counts_str()}"
        )


def main(phase_secs: int) -> None:
    print("=== Nautilus WS Sandbox Roll Explorer ===\n")
    for lbl, m in MARKETS.items():
        print(f"  {lbl}: {m['name']}")
        print(f"     {m['instrument_id'][:60]}...")
    print(f"\nPhase observation window: {phase_secs}s  |  Total runtime: ~{phase_secs * 6 + 10}s")
    print("\nTest: unsubscribe(OLD) + subscribe(NEW) in same callback (mirrors _on_window_end)\n")

    try:
        private_key = os.environ["POLYMARKET_TEST_PRIVATE_KEY"]
        api_key = os.environ["POLYMARKET_TEST_API_KEY"]
        api_secret = os.environ["POLYMARKET_TEST_API_SECRET"]
        passphrase = os.environ["POLYMARKET_TEST_API_PASSPHRASE"]
        funder = os.environ["POLYMARKET_TEST_WALLET_ADDRESS"]
    except KeyError as e:
        sys.exit(f"Missing env var: {e}")

    instrument_ids = [m["instrument_id"] for m in MARKETS.values()]

    node_config = TradingNodeConfig(
        data_clients={
            "POLYMARKET": PolymarketDataClientConfig(
                private_key=private_key,
                api_key=api_key,
                api_secret=api_secret,
                passphrase=passphrase,
                funder=funder,
                instrument_config=PolymarketInstrumentProviderConfig(
                    load_ids=frozenset(instrument_ids),
                ),
            ),
        },
        exec_clients={},
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("POLYMARKET", PolymarketLiveDataClientFactory)

    explorer = NautilusWsExplorer(
        NautilusWsExplorerConfig(
            instrument_id_a=instrument_ids[0],
            instrument_id_b=instrument_ids[1],
            instrument_id_c=instrument_ids[2],
            phase_secs=phase_secs,
        )
    )
    node.trader.add_actor(explorer)
    node.build()

    total_secs = phase_secs * 6 + 10
    threading.Timer(total_secs, node.stop).start()
    print(f"Running for {total_secs}s...\n")
    node.run()

    print("\n=== Done ===")
    print("Key results to check in logs:")
    print("  Phase 2 (broken roll A→B): B=0?  → YES=bug confirmed")
    print("  Phase 4 (fixed  roll B→C): C>0?  → YES=fix works")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nautilus WS sandbox roll explorer")
    parser.add_argument("--phase-secs", type=int, default=20,
                        help="Seconds to observe per phase (default: 20)")
    args = parser.parse_args()
    main(args.phase_secs)
