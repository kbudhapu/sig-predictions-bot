"""Leaderboard recorder (research): top 300 every 15 min -> snaps.jsonl. 3 reads per snapshot (page size max 100)."""
import json
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/opt/sig-bot")
from sig.sig_client import SigClient  # noqa: E402

c = SigClient()
OUT = "/opt/sig-bot/data/leaderboard/snaps.jsonl"
while True:
    rows, r = [], {}
    try:
        for off in (0, 100, 200):
            r = c.get("/tournaments/midterm-elections/leaderboard", params={"limit": 100, "offset": off})
            rows += r.get("leaderboard", [])
            time.sleep(2)
        with open(OUT, "a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "total": r.get("total"),
                                 "myRank": r.get("myRank"), "rows": rows}) + "\n")
    except Exception as e:  # noqa: BLE001
        print("err", str(e)[:200], flush=True)
    time.sleep(900)
