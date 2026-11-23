"""Randomized stress sweep for the fair-band live trader's isolation + capital guarantees (2026-10-08).

Hostile fake venue (tests/test_isolation.Venue): random partial/full fills, lagging open-orders view, rejects,
failed reads, stale-book bursts, cash squeezes, loss-limit kill switch, random configs. Counts violations of:
budget / market cap at every placement, write cap, cash bound (budget + loss limit), sells, writes under another
strategy id, trades outside owned markets, and any change to another strategy's position. Expect all zero.
Run:  PYTHONPATH=. python tools/isolation_stress.py [seeds] [ticks]
"""
import asyncio
import collections
import random
import sys
import tempfile

from sig.engine import ownership as om
from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID
from sig.engine.ownership import MarketOwnership
from tests.test_isolation import Clock, Venue, Walk, tr_pairs


def main(seeds: int = 50, ticks: int = 400) -> int:
    bad, stats = collections.Counter(), collections.Counter()
    for seed in range(seeds):
        rng = random.Random(1000 + seed)
        d = tempfile.mkdtemp()
        reg = MarketOwnership(d + "/o.json")
        om.set_registry(reg)
        clk = Clock()
        cfg = FairBandLiveConfig(budget_usd=rng.choice([2000., 5000., 10000.]), market_cap_usd=rng.choice([500., 1500.]),
                                 quote_usd=rng.choice([200., 500.]), n_markets=rng.choice([3, 6, 10]),
                                 max_writes_per_min=rng.choice([4, 6, 12]), max_loss_usd=rng.choice([300., 750.]),
                                 stop_file=d + "/S")
        v = Venue(reg, rng, lag=rng.random() < 0.5, reject_p=rng.choice([0, 0.1, 0.4]))
        v.pos["cross"] = 777.0
        tr = FairBandLive(broker=v, ownership=reg, cfg=cfg, clock=clk)
        owned = tr.select_markets([str(i) for i in range(15)] + ["cross"], dict(v.pos), v.open_view())
        walk, spent = Walk(owned, rng), 0.0
        for _ in range(ticks):
            clk.t += 60
            rows = walk.rows()
            if rng.random() < 0.05:
                rows = {ex: {**r, "age_s": 999} for ex, r in rows.items()}
            mids = {ex: (r["best_bid"] + r["best_ask"]) / 2 for ex, r in rows.items()}
            v.mids, v.cfg = mids, cfg
            val = sum(FairBandLive._exposure(v.pos.get(ex, 0.0), mids[ex]) for ex in owned)
            tr.check_kill(val - (spent - tr_pairs(v)))
            view = None if rng.random() < 0.05 else v.open_view()
            out = asyncio.run(tr.tick(rows, dict(v.pos), view, rng.choice([None, 1e6, 100.0])))
            stats["placed"] += out["placed"]
            stats["cancelled"] += out["cancelled"]
            if out["placed"] + out["cancelled"] > cfg.max_writes_per_min:
                bad["writes"] += 1
            before = {o: dict(x) for o, x in v.orders.items()}
            v.random_fills()
            for o, x in before.items():
                spent += (x["quantity"] - (v.orders[o]["quantity"] if o in v.orders else 0.0)) * x["price"]
            if spent - tr_pairs(v) > cfg.budget_usd + cfg.max_loss_usd + 1:
                bad["cash"] += 1
        bad["violations"] += len(v.violations)
        bad["sells"] += v.sells
        bad["foreign_writes"] += sum(1 for w in v.writes if w[1] != SID)
        bad["outside_markets"] += sum(1 for w in v.writes if w[0] == "place" and w[2] not in owned)
        bad["cross_touched"] += v.pos["cross"] != 777.0
        stats["halted"] += tr.winding_down
    print(f"seeds {seeds} x {ticks} ticks | placed {stats['placed']} cancelled {stats['cancelled']} "
          f"kill-switch runs {stats['halted']}")
    print("violations:", dict(bad) or "none")
    return 1 if sum(bad.values()) else 0


if __name__ == "__main__":
    sys.exit(main(*(int(a) for a in sys.argv[1:3])))
