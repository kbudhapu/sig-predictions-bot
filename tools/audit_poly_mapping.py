"""Audit signals/race_mapping.yaml against LIVE Polymarket: flag every mapped market that is closed, not accepting
orders, or ends before 2026 (2026-10-09: 21 Senate legs pointed at the CLOSED 2024 markets; cross silently had no fair
for them while Kalshi showed 4-12c gaps). External reads only (Gamma), no SIG calls. Exit 1 if anything is flagged.
Run:  python tools/audit_poly_mapping.py
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from signals.mapping import load_race_mapping  # noqa: E402


def main() -> int:
    bad = []
    races = load_race_mapping("signals/race_mapping.yaml")
    for key, r in races.items():
        p = r.externals.get("polymarket")
        if not p or not p.market_id:
            continue
        try:
            req = urllib.request.Request(f"https://gamma-api.polymarket.com/markets/{p.market_id}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            m = json.load(urllib.request.urlopen(req, timeout=20))
        except Exception as exc:  # noqa: BLE001
            bad.append((key, p.market_id, f"fetch failed: {exc}"))
            continue
        end = str(m.get("endDate") or "")
        if m.get("closed") or not m.get("acceptingOrders") or (end and end[:4] < "2026"):
            bad.append((key, p.market_id, f"closed={m.get('closed')} accepting={m.get('acceptingOrders')} end={end[:10]}"
                                          f" active_in_bot={r.is_active()} q={str(m.get('question'))[:60]}"))
        time.sleep(0.15)
    print(f"{len(races)} races checked, {len(bad)} flagged")
    for b in bad:
        print("  ", *b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
