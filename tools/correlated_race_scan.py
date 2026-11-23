"""Correlated-race stat-arb scan (SHADOW — log-only, places no orders).

Two races in the SAME contest are near-perfect complements: "Will the Democratic Party win the X
Governor" and "Will the Republican Party win the X Governor" between them cover ~all the probability
(a third-party win is the only leak). So their YES prices should sum to ~1. When they DON'T, that is a
model-free relative-value arb that needs NO external benchmark — the internal cousin of cross-market:

  * SUM > 1 (overpriced pair): SHORT BOTH (buy NO on D and NO on R). Payoff is >= 1 in every outcome —
    D wins, R wins, OR a third party wins — so profit = sum - 1 - cost is ROBUST to the third-party tail.
  * SUM < 1 (underpriced pair): BUY BOTH (buy YES on D and YES on R). Profit = 1 - sum - cost, BUT a
    third-party win makes both legs settle 0 and you lose the stake — so this side carries TAIL RISK and
    is only safe where a third party cannot realistically win.

This differs from the arb engine, which trades SIG's DECLARED relationships; this surfaces UNDECLARED
complement mispricings. Pairs are matched by (office, state) parsed from the market titles.

Run on the droplet:
    PYTHONPATH=/opt/sig-bot .venv/bin/python tools/correlated_race_scan.py [edge_threshold=0.01]
"""
from __future__ import annotations

import re
import sys

from sig.sig_client import SigClient

# "Will the Democratic Party win the Pennsylvania Governor race?" / "... the Georgia Senate?"
_PAT = re.compile(r"will the (democratic|republican) party win the (.+?)(?:\s+race)?\??$", re.I)


def _parse(title: str):
    m = _PAT.match((title or "").strip())
    if not m:
        return None
    party = "D" if m.group(1).lower().startswith("dem") else "R"
    contest = re.sub(r"\s+", " ", m.group(2).strip().lower())   # "pennsylvania governor"
    return party, contest


def _open_binary(sig: SigClient):
    out = []
    for m in sig.markets_all():
        if not isinstance(m, dict) or m.get("status") != "open":
            continue
        exs = m.get("exchanges") or []
        if len(exs) != 1:
            continue
        eid, p = exs[0].get("id"), exs[0].get("latestPrice")
        if eid is None or p is None:
            continue
        out.append({"eid": str(eid), "title": m.get("question") or m.get("title") or "?",
                    "latest": float(p)})
    return out


def _touch(sig: SigClient, ids):
    out = {}
    for i in range(0, len(ids), 90):
        for r in sig.exchange_prices(ids[i:i + 90]).get("data", []):
            b, a = r.get("bestBid"), r.get("bestAsk")
            if b is not None and a is not None:
                out[str(r.get("exchangeId"))] = (float(b), float(a), (float(b) + float(a)) / 2.0)
    return out


def main():
    thr = float(sys.argv[1]) if len(sys.argv) > 1 else 0.01
    sig = SigClient(key_env="SIG_API_KEY_READ", timeout=30.0)
    markets = _open_binary(sig)
    touch = _touch(sig, [m["eid"] for m in markets])

    # group the two party legs of each contest
    contests: dict[str, dict] = {}
    for m in markets:
        pc = _parse(m["title"])
        if pc is None:
            continue
        party, contest = pc
        contests.setdefault(contest, {})[party] = m

    print(f"=== correlated-race complement scan (SHADOW) — |sum-1| >= {thr:.3f} ===")
    print(f"{'contest':30s} {'pD_mid':>6s} {'pR_mid':>6s} {'sum':>6s} {'dev':>7s} "
          f"{'action':20s} {'edge':>6s} {'robust':>6s}")

    rows = []
    for contest, legs in contests.items():
        if "D" not in legs or "R" not in legs:
            continue
        td, tr = touch.get(legs["D"]["eid"]), touch.get(legs["R"]["eid"])
        if not td or not tr:
            continue
        pD_mid, pR_mid = td[2], tr[2]
        s_mid = pD_mid + pR_mid
        dev = s_mid - 1.0
        if abs(dev) < thr:
            continue
        if dev > 0:                                 # overpriced -> short both (buy NO on each)
            # executable edge uses the touch you'd PAY: short both pays (1-bid) each; profit = sum_bid - 1
            edge = (td[0] + tr[0]) - 1.0            # sum of bids - 1
            action, robust = "SHORT both (buy NO)", "yes"
        else:                                       # underpriced -> buy both (buy YES on each)
            edge = 1.0 - (td[1] + tr[1])            # 1 - sum of asks
            action, robust = "BUY both (buy YES)", "3p-risk"
        rows.append((contest, pD_mid, pR_mid, s_mid, dev, action, edge, robust))

    rows.sort(key=lambda r: -r[6])                  # best executable edge first
    n_exec = 0
    for contest, pD, pR, s, dev, action, edge, robust in rows:
        if edge > 0:
            n_exec += 1
        print("%-30s %6.3f %6.3f %6.3f %+7.3f %-20s %+6.3f %6s" % (
            contest[:30], pD, pR, s, dev, action, edge, robust))

    print(f"\n--- {len(rows)} complement pairs off {thr:.3f} from 1.0; {n_exec} with POSITIVE executable "
          f"edge after crossing the touch. SHADOW ONLY — no orders. ---")
    print("SHORT-both (sum>1) is robust to a third-party win; BUY-both (sum<1) loses the stake if a third "
          "party wins — fund only where that is ~impossible. Cross-check vs the arb engine's declared links.")


main()
