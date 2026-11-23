"""Phase-1 verification: the PORTED idempotency core works standalone in sig-bot (mock broker only —
no live order). Proves deterministic key, query-before-submit dedup, idempotent fills, reconcile-trips."""
import sys, os, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from infra.execution.idempotent_execution import (
    OrderIntent, BrokerOrder, IdempotentSubmitter, PositionTracker, reconcile_boot,
    deterministic_client_order_id, SIG_KEY_PREFIX)
from infra.escalation import RiskEscalationEngine, RiskEscalationLevel
from infra.entry_authority import EntryContext, entry_allowed, EntryDecision
P = []; F = []
def ck(n, c, d=""): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n + (" :: " + d if d else ""))


class MockBroker:
    """In-memory ExecutionBroker; submit() records exactly what it was asked, keyed by coid."""
    def __init__(self): self.orders = {}; self.calls = 0; self._lock = threading.Lock()
    def submit(self, intent):
        with self._lock:
            self.calls += 1
            o = BrokerOrder(client_order_id=intent.client_order_id, symbol=intent.symbol,
                            side=intent.side, qty=intent.qty, status="new", order_id=f"srv{self.calls}")
            self.orders[intent.client_order_id] = o
            return o
    def get_by_client_order_id(self, coid): return self.orders.get(coid)
    def open_orders(self): return list(self.orders.values())
    def positions(self): return {}


def main():
    # 1. deterministic + SIG-format key
    k1 = deterministic_client_order_id("legA", "2026-10-01T00:00:00", "buy", 7)
    k2 = deterministic_client_order_id("legA", "2026-10-01T00:00:00", "buy", 7)
    ck("deterministic key stable across calls", k1 == k2)
    ck("key uses SIG prefix (not mbappe)", k1.startswith(SIG_KEY_PREFIX + "-") and "mbappe" not in k1, k1)

    # 2. query-before-submit dedup: a storm of identical intents -> exactly ONE broker order
    br = MockBroker(); sub = IdempotentSubmitter(br)
    intent = OrderIntent(leg_id="legA", symbol="MKT1", side="buy", qty=10, signal_ts="2026-10-01T00:00:00", epoch=7)
    results = []
    def fire(): results.append(sub.submit(intent))
    threads = [threading.Thread(target=fire) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    noops = sum(1 for r in results if r.was_noop)
    ck("dedup storm -> exactly ONE real broker submit", br.calls == 1, f"broker.calls={br.calls}")
    ck("dedup storm -> 11 of 12 were NO-OPs", noops == 11, f"noops={noops}")

    # 3. entry_guard blocks (kill outranks) -> no order sent
    br2 = MockBroker(); sub2 = IdempotentSubmitter(br2)
    halt_ctx = EntryContext(action="BUY", risk_halted=True)
    try:
        sub2.submit(OrderIntent("legB", "MKT2", "buy", 5, "2026-10-01T00:00:01", 7),
                    entry_guard=lambda: entry_allowed(halt_ctx))
        ck("entry_guard block raises + sends nothing", False)
    except Exception as e:
        ck("entry_guard block raises + sends nothing", br2.calls == 0 and "risk_halt" in str(e), f"calls={br2.calls}")

    # 4. idempotent fill application: applying the same fill twice does not double-count
    pt = PositionTracker()
    filled = BrokerOrder(client_order_id=k1, symbol="MKT1", side="buy", qty=10, status="filled",
                         filled_qty=10, filled_avg_price=0.42)
    pt.apply(filled); pt.apply(filled)
    ck("idempotent fill: double-apply does not double-count", abs(pt.position("MKT1") - 10.0) < 1e-9, str(pt.position("MKT1")))

    # 5. reconcile-boot trips ENTRY_GATE_HALT on ANY mismatch, never silently adopts
    esc = RiskEscalationEngine()
    rep = reconcile_boot({"MKT1": 10.0}, {"MKT1": 7.0}, escalation=esc)
    ck("reconcile trips on qty mismatch", rep.tripped and not rep.clean)
    ck("reconcile -> ENTRY_GATE_HALT (never silently adopts broker)", esc.snapshot().level == RiskEscalationLevel.ENTRY_GATE_HALT)
    rep2 = reconcile_boot({"MKT1": 10.0}, {"MKT1": 10.0}, escalation=RiskEscalationEngine())
    ck("reconcile clean when matched", rep2.clean and not rep2.tripped)

    print(f"\n==== IDEMPOTENCY PORT: PASS {len(P)}/{len(P)+len(F)} ====")
    if F: print("FAILED:", F); sys.exit(1)


if __name__ == "__main__":
    main()
