"""Validate the D+R PARITY shadow (sig/engine/parity.py) — the gate before any live NO-pair trading.

Reads data/parity_shadow/events.jsonl (entry/exit events from main_sig._parity_loop) and reports:
  • entries/day, closed round trips, realized c/pair (taker, at real fetched bid/ask), hold time
  • still-open pairs and the edge they LOCK at settlement (a NO pair always pays >= 1)
  • $ per day at an assumed size (PAIRS per trade; check top-of-book depth before trusting it)
Promote only if round trips are consistently positive over several days AND fills look achievable.
Local only. Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/parity_shadow_validate.py [pairs]
"""
from __future__ import annotations

import collections
import json
import os
import statistics
import sys
from datetime import datetime

PATH = os.environ.get("PARITY_SHADOW_PATH", "data/parity_shadow/events.jsonl")


def _ep(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def summarize(events: list[dict], pairs_per_trade: float = 1000.0) -> dict:
    entries = [e for e in events if e["event"] == "entry"]
    exits = [e for e in events if e["event"] == "exit"]
    if not entries:
        return {"entries": 0}
    t0 = min(_ep(e["ts"]) for e in events)
    t1 = max(_ep(e["ts"]) for e in events)
    days = max((t1 - t0) / 86400, 1e-9)
    open_ = {}
    for e in events:
        if e["event"] == "entry":
            open_[e["race"]] = e
        else:
            open_.pop(e["race"], None)
    holds = [(_ep(x["ts"]) - _ep(x["entry_ts"])) / 60 for x in exits]
    pnl = [100 * x["exit_pnl"] for x in exits]
    realized_usd = sum(x["exit_pnl"] for x in exits) * pairs_per_trade
    return {
        "days": round(days, 2), "entries": len(entries), "entries_per_day": round(len(entries) / days, 1),
        "exits": len(exits),
        "exit_pnl_c_mean": round(statistics.mean(pnl), 2) if pnl else None,
        "exit_pnl_c_min": round(min(pnl), 2) if pnl else None,
        "hold_min_median": round(statistics.median(holds), 0) if holds else None,
        "open": len(open_),
        "open_locked_c_mean": round(100 * statistics.mean(e["entry_edge"] for e in open_.values()), 2) if open_ else None,
        "realized_usd_per_day": round(realized_usd / days, 0),
        "races": len({e["race"] for e in entries}),
        "top_races": collections.Counter(e["race"] for e in entries).most_common(5),
    }


def main() -> None:
    if not os.path.exists(PATH):
        print("no parity shadow events yet")
        return
    events = [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
    size = float(sys.argv[1]) if len(sys.argv) > 1 else 1000.0
    for k, v in summarize(events, size).items():
        print(f"  {k:22s} {v}")
    print(f"  (realized_usd_per_day assumes {size:.0f} pairs per trade)")


if __name__ == "__main__":
    main()
