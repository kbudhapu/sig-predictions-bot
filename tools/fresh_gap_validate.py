"""Score the FRESH-DISLOCATION REVERSION shadow (main_sig._fresh_gap_loop -> data/fresh_gap_shadow/events.jsonl).

Per trade, per share, held-side prices:  TAKER = taker_out - taker_in  (cross the touch both ways)
                                         MAKER = maker_out - maker_in  (join the touch both ways; fills NOT guaranteed)
Open trades are marked at the current SIG touch (prices table) with the taker exit (no survivorship).
Promote to a capital pilot only if TAKER per-share stays >= +0.5c with hit >= 60% over >= 30 closed trades.
Run LOCALLY on a snapshot (the droplet has ~1GB RAM):
  SIG_STATE_DB=<snap.db> FRESH_GAP_PATH=<events.jsonl> python tools/fresh_gap_validate.py
"""
import json
import os
import sqlite3
import statistics as st

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
PATH = os.environ.get("FRESH_GAP_PATH", "data/fresh_gap_shadow/events.jsonl")


def main() -> None:
    ev = [json.loads(line) for line in open(PATH, encoding="utf-8") if line.strip()]
    entries = {e["id"]: e for e in ev if e["event"] == "entry"}
    exits = {e["id"]: e for e in ev if e["event"] == "exit"}
    px = {}
    if os.path.exists(DB):
        c = sqlite3.connect(DB)
        px = {str(e): (b, a) for e, b, a in c.execute("select exchange_id, best_bid, best_ask from prices")
              if b is not None and a is not None}
    closed, opened = [], []
    for tid, e in entries.items():
        x = exits.get(tid)
        if x:
            closed.append(dict(ex=e["exchange_id"], reason=x["reason"], age=x["age_h"],
                               taker=x["prices"]["taker_out"] - e["prices"]["taker_in"],
                               maker=x["prices"]["maker_out"] - e["prices"]["maker_in"],
                               usd=e["shares"] * (x["prices"]["taker_out"] - e["prices"]["taker_in"])))
        elif e["exchange_id"] in px:
            b, a = px[e["exchange_id"]]
            out = b if e["side"] == "yes" else 1 - a
            opened.append(dict(ex=e["exchange_id"], taker=out - e["prices"]["taker_in"],
                               usd=e["shares"] * (out - e["prices"]["taker_in"])))
    span = (max(e["epoch"] for e in ev) - min(e["epoch"] for e in ev)) / 86400 if ev else 0
    print(f"events {len(ev)} over {span:.1f}d: entries {len(entries)}, closed {len(closed)}, open {len(opened)}")
    if closed:
        t = [c["taker"] * 100 for c in closed]
        m = [c["maker"] * 100 for c in closed]
        print(f"CLOSED: taker {st.mean(t):+.2f}c/share hit {sum(x > 0 for x in t) / len(t):.0%} | maker {st.mean(m):+.2f}c "
              f"| median hold {st.median(c['age'] for c in closed):.1f}h | $ at $2.5k/trade {sum(c['usd'] for c in closed):+,.0f}")
        reasons = {}
        for c in closed:
            reasons[c["reason"]] = reasons.get(c["reason"], 0) + 1
        print("  exits:", reasons)
    if opened:
        o = [x["taker"] * 100 for x in opened]
        print(f"OPEN (marked at taker exit now): {st.mean(o):+.2f}c/share, {sum(x > 0 for x in o)}/{len(o)} up, "
              f"$ {sum(x['usd'] for x in opened):+,.0f}")
    allv = [c["taker"] * 100 for c in closed] + [x["taker"] * 100 for x in opened]
    if allv:
        print(f"ALL (closed + open marked): {st.mean(allv):+.2f}c/share over {len(allv)} trades")


if __name__ == "__main__":
    main()
