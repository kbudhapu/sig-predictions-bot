"""Backfill sharp_history from the trader's journald `cross_market_raw` debug lines (sampled once per ~minute
since 2026-10-05 15:00Z). Idempotent (INSERT … ON CONFLICT DO NOTHING), so re-running is harmless.

Run (droplet):
  journalctl -u sig-bot --no-pager -o cat | grep cross_market_raw | \
    PYTHONPATH=/opt/sig-bot .venv/bin/python tools/backfill_sharp_history.py
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone

from infra.state.store import StateStore
from signals.mapping import load_race_mapping

_PAT = re.compile(r"^(\S+ \S+) .*cross_market_raw\s+implied_aligned=([0-9.]+).*?race=(\S+).*?raw_yes=([0-9.]+)")


def parse(lines) -> list[dict]:
    key2ex = {k: str(v.sig_exchange_id) for k, v in load_race_mapping().items() if v.sig_exchange_id}
    rows = []
    for line in lines:
        m = _PAT.search(line)
        if not m:
            continue
        ex = key2ex.get(m.group(3).strip("'\""))
        if not ex:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
        rows.append({"exchange_id": ex, "ts": ts, "sharp": float(m.group(2)), "raw": float(m.group(4)),
                     "sig_mid": None, "venue": "polymarket"})
    return rows


async def main() -> None:
    rows = parse(sys.stdin)
    store = await StateStore().open()
    try:
        n = 0
        for i in range(0, len(rows), 2000):
            n += await store.append_sharp_history(rows[i:i + 2000])
        print(f"parsed={len(rows)} submitted={n}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
