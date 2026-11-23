"""Score the POLY-LED LAG shadow (main_sig._poly_lead_loop -> data/poly_lead_shadow/events.jsonl), per lookback.

Per trade, per share, held-side prices: TAKER = taker_out - taker_in (honest yardstick), MAKER = join both ways
(fills NOT guaranteed). Open trades are marked at the current SIG touch (prices table) with the taker exit.
Research bar to promote into the pilot: TAKER >= +0.5c/share, hit >= 55%, >= 20 trades in a lookback.
  SIG_STATE_DB=<db> POLY_LEAD_PATH=<events.jsonl> python tools/poly_lead_validate.py
"""
import json
import os
import sqlite3
import statistics as st

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
PATH = os.environ.get("POLY_LEAD_PATH", "data/poly_lead_shadow/events.jsonl")
USD = 2000.0                                    # dollars per trade for the $ column (pilot trade size)


def main() -> None:
    if not os.path.exists(PATH):
        print("no events yet")
        return
    ev = [json.loads(line) for line in open(PATH, encoding="utf-8") if line.strip()]
    px = {}
    if os.path.exists(DB):
        c = sqlite3.connect(DB)
        px = {str(e): (b, a) for e, b, a in c.execute("select exchange_id, best_bid, best_ask from prices")
              if b is not None and a is not None}
    span = (max(e["epoch"] for e in ev) - min(e["epoch"] for e in ev)) / 86400 if ev else 0
    print(f"events {len(ev)} over {span:.2f}d")
    for lb in sorted({float(e["lb_h"]) for e in ev}):
        E = [e for e in ev if float(e["lb_h"]) == lb]
        entries = {e["id"]: e for e in E if e["event"] == "entry"}
        exits = {e["id"]: e for e in E if e["event"] == "exit"}
        rows = []
        for tid, e in entries.items():
            x = exits.get(tid)
            tin = e["prices"]["taker_in"]
            if x:
                out, mk, state = x["prices"]["taker_out"], x["prices"]["maker_out"] - e["prices"]["maker_in"], x["reason"]
            elif e["exchange_id"] in px:
                b, a = px[e["exchange_id"]]
                out, mk, state = (b if e["side"] == "yes" else 1 - a), None, "open"
            else:
                continue
            rows.append(dict(taker=out - tin, maker=mk, state=state, usd=USD / tin * (out - tin), age=x and x["age_h"]))
        if not rows:
            print(f"  {lb:g}h: {len(entries)} entries, nothing scorable")
            continue
        t = [r["taker"] * 100 for r in rows]
        closed = [r for r in rows if r["state"] != "open"]
        mk = [r["maker"] * 100 for r in closed if r["maker"] is not None]
        states = {}
        for r in rows:
            states[r["state"]] = states.get(r["state"], 0) + 1
        print(f"  {lb:g}h: {len(rows)} trades ({len(closed)} closed) taker {st.mean(t):+.2f}c/share "
              f"hit {sum(x > 0 for x in t) / len(t):.0%} | maker(closed) {st.mean(mk) if mk else float('nan'):+.2f}c "
              f"| $ at ${USD:,.0f}/trade {sum(r['usd'] for r in rows):+,.0f} "
              f"| med hold {st.median(r['age'] for r in closed) if closed else float('nan'):.1f}h | {states}")


if __name__ == "__main__":
    main()
