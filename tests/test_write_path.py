"""Phase-3 OFFLINE verification of the SIG write-GATE + retry policy (mock transport — NO live HTTP).

Covers the still-live sig/write_gate.py pieces: the retry-policy classifier (submit_with_policy →
NO_RETRY on a 4xx) and the OutboundRateGate (30-writes/min cap, fast-key urgency reservation,
pick_write_key).

Retired 2026-10-02: the broker write-path assertions that used to live here (order-body construction,
status remap, idempotent dedup, 429/409/502 handling, breaker) tested the SUPERSEDED sig/sig_broker.py
and were removed with it — the canonical broker (sig/broker.py) covers the same behaviours in
tests/test_broker.py, so nothing was lost. This file no longer imports the retired broker.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sig.write_gate import (
    OutboundRateGate, SigGateConfig, submit_with_policy, HttpResult, RetryAction, SigWriteError,
)
P = []; F = []
def ck(n, c, d=""): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n + (" :: " + d if d else ""))


def main():
    S = lambda _: None   # no real sleeps
    # 1. policy layer classifies a 4xx as NO_RETRY (raises at the submit_with_policy level)
    try:
        submit_with_policy(lambda: HttpResult(400, "BAD"), sleep_fn=S)
        ck("policy: 400 -> SigWriteError NO_RETRY", False)
    except SigWriteError as e:
        ck("policy: 400 -> SigWriteError NO_RETRY", e.status == 400 and e.action == RetryAction.NO_RETRY)
    # 2. write-gate: 30 writes/min cap (here 3), urgent reserves the fast lane
    g = OutboundRateGate(SigGateConfig(writes_per_min=3, fast_key_reserved_writes_per_min=2))
    r = [g.allow("write", "write")[0] for _ in range(4)]
    ck("write-gate caps at 30/min (here 3): 3 ok then blocked", r == [True, True, True, False], str(r))
    # routine on fast limited to (3-2)=1; urgent can still use the reserved budget
    ck("fast key routine limited to non-reserved slice",
       g.allow("fast", "write", urgent=False)[0] is True and g.allow("fast", "write", urgent=False)[0] is False)
    ck("fast key urgent uses reserved budget", g.allow("fast", "write", urgent=True)[0] is True)
    ck("pick_write_key: urgent prefers fast, routine prefers write",
       OutboundRateGate(SigGateConfig()).pick_write_key(urgent=True) == "fast"
       and OutboundRateGate(SigGateConfig()).pick_write_key(urgent=False) == "write")
    print(f"\n==== WRITE GATE + POLICY (offline): PASS {len(P)}/{len(P)+len(F)} ====")
    if F:
        print("FAILED:", F); sys.exit(1)


if __name__ == "__main__":
    main()
