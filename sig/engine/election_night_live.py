"""ELECTION-NIGHT race-call trader — LIVE (2026-10-13). Executes sig/engine/election_night.step for real: when a race is
called on Polymarket (a leg crosses theta=95% from below, Kalshi agreeing) and SIG still lags, BUY that leg with a limit
that never pays more than Polymarket - min_edge, and HOLD it to settlement (it settles at 1.00). Research: journal
2026-10-08 (2024 replay at theta 0.95: 0 false calls; +$3.3k at no SIG lag .. +$20k at a 60-min lag, $3k/race).

OFF unless election_night_live_mode is "dry" (full decision path on live data, NO orders, would-be orders recorded) or
"live". Orders only inside [window_start, window_end] (6pm ET Nov 3 .. noon ET Nov 4, when SIG stops trading).

ISOLATION (same model as the other live pilots):
  * Each market is CLAIMED (sig/engine/ownership.py) before its first order and NEVER released while held: the broker
    then refuses every other strategy there, so cross's convergence exits can't sell our winners once SIG catches up.
    The loop heart-beats the lease until shutdown (a lapsed lease would let exit:* strategies trade our markets).
  * Cash: a hard budget_usd. From reserve_from (Nov 2, when the other pilots wind down) until window_end the UNSPENT
    budget is carved out of the orchestrator's ledger (cross can't spend the night's cash); before that, no carve-out.
    Each order is also capped by buying power minus every other strategy's carve-out.
  * Committed cash is conservative: resting orders at full notional, orders that left the book at full notional until
    their fills are ingested (resolve_after_gone_s), lost replies at full notional forever. Never over budget.
  * Buy-only, limit <= poly - min_edge, venue expiry order_expiry_s, <= max_attempts orders per race, one live order
    per market, own write cap. Cash-limited -> biggest edge first.
  * STOP file -> no new orders (holdings stay). Orchestrator halt -> pause. Restart-safe (state_path).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sig.engine.election_night import ElectionNightConfig, NightState, step

SID = "election_night"


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


@dataclass(frozen=True)
class ElectionNightLiveConfig:
    window_start_iso: str = "2026-11-03T23:00:00+00:00"    # 6pm ET (EST) - first polls close
    window_end_iso: str = "2026-11-04T17:00:00+00:00"      # noon ET Nov 4 - SIG stops trading
    reserve_from_iso: str = "2026-11-02T00:00:00+00:00"    # carve the night's cash out from here (pilots wind down)
    budget_usd: float = 30000.0
    usd_per_race: float = 2000.0
    max_attempts: int = 3                                   # orders per race (first + top-ups)
    retry_after_s: float = 75.0                             # wait after an order before a top-up
    order_expiry_s: float = 60.0
    send_deadline_s: float = 10.0
    max_writes_per_min: int = 20
    min_order_usd: float = 50.0
    resolve_after_gone_s: float = 150.0
    pending_ttl_s: float = 180.0
    resume_after_clear_ticks: int = 5
    stop_file: str = "data/election_night_live/STOP"
    state_path: str = "data/election_night_live/state.json"
    dry_path: str = "data/election_night_live/dry.jsonl"
    signal: ElectionNightConfig = field(default_factory=ElectionNightConfig)


@dataclass
class ElectionNightLive:
    broker: Any
    ownership: Any
    cfg: ElectionNightLiveConfig = field(default_factory=ElectionNightLiveConfig)
    dry: bool = True
    clock: Any = time.time
    others_reserved: Any = None
    races: dict = field(default_factory=dict)        # race -> job {exchange_id, orders: [[ts, oid|None, qty, px, gone_at]], ...}
    night: NightState = field(default_factory=NightState)
    stopped: bool = False
    stop_reason: str | None = None
    paused: bool = False
    _clear: int = 0
    _writes: list = field(default_factory=list)
    _last_committed: float = 0.0

    # ---------------- persistence ----------------
    def load(self) -> None:
        try:
            with open(self.cfg.state_path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        self.races = dict(d.get("races") or {})
        self.night = NightState(last_low={str(k): float(v) for k, v in (d.get("last_low") or {}).items()},
                                fired=set(d.get("fired") or []))

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.cfg.state_path) or ".", exist_ok=True)
        tmp = self.cfg.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"races": self.races, "last_low": self.night.last_low, "fired": sorted(self.night.fired)}, fh)
        os.replace(tmp, self.cfg.state_path)

    # ---------------- windows / capital ----------------
    def in_window(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        try:
            return _ts(self.cfg.window_start_iso) <= now < _ts(self.cfg.window_end_iso)
        except (TypeError, ValueError):
            return False

    def committed(self, open_ids: set | None = None, fills_by_order: dict | None = None) -> float:
        """Cash this strategy has spent or may have spent (conservative)."""
        now = self.clock()
        tot = 0.0
        for job in self.races.values():
            for o in job.get("orders", []):
                ts, oid, qty, px = float(o[0]), o[1], float(o[2]), float(o[3])
                gone_at = o[4] if len(o) > 4 else None
                if (oid and fills_by_order is not None and gone_at is not None
                        and now - float(gone_at) >= self.cfg.resolve_after_gone_s
                        and (open_ids is None or oid not in open_ids)):
                    tot += min(float(fills_by_order.get(oid, 0.0)), qty) * px
                else:
                    tot += qty * px                 # resting, recently gone, or reply lost: full notional
        return round(tot, 2)

    def unused_budget(self) -> float:
        """Ledger carve-out: the night's unspent budget, only from reserve_from until window_end."""
        now = self.clock()
        try:
            if not (_ts(self.cfg.reserve_from_iso) <= now < _ts(self.cfg.window_end_iso)) or self.stopped:
                return 0.0
        except (TypeError, ValueError):
            return 0.0
        return max(round(self.cfg.budget_usd - self._last_committed, 2), 0.0)

    def _can_write(self) -> bool:
        now = self.clock()
        self._writes = [t for t in self._writes if now - t < 60.0]
        return len(self._writes) < self.cfg.max_writes_per_min

    def check_kill(self, orchestrator_halted: bool = False) -> None:
        if os.path.exists(self.cfg.stop_file):
            self.stopped, self.stop_reason = True, "stop_file"
        if getattr(self.ownership, "corrupt", False):
            self.stopped, self.stop_reason = True, "ownership_registry_corrupt"
        if orchestrator_halted:
            self.paused, self._clear = True, 0
        elif self.paused:
            self._clear += 1
            if self._clear >= self.cfg.resume_after_clear_ticks:
                self.paused, self._clear = False, 0

    def held_markets(self) -> set:
        return {j["exchange_id"] for j in self.races.values() if j.get("orders")}

    # ---------------- one tick ----------------
    async def tick(self, legs: list[dict], open_orders: list | None, free_cash: float | None,
                   fills_by_order: dict | None = None) -> dict:
        """legs: {race, exchange_id, poly, kalshi, kalshi_mapped, sig_bid, sig_ask, ask_size} (sig_* None if stale).
        open_orders: the venue's open orders (None = read failed -> no orders this tick)."""
        now = self.clock()
        out = {"placed": 0, "dry": 0, "fired": 0}
        sc = self.cfg.signal
        # ARM always (a crossing counts only if the leg was seen low within arm_window_s), fire only in the window
        for l in legs:
            if l.get("poly") is not None and l["poly"] <= sc.theta - sc.arm_margin:
                self.night.last_low[str(l["exchange_id"])] = now
        changed = False
        if not self.in_window(now) or self.stopped:
            self.save()
            out["window"] = self.in_window(now)
            return out
        if open_orders is None:
            out["skipped"] = "open_orders_read_failed"
            return out
        open_ids = {str(o["order_id"]) for o in open_orders}
        mine_open = {str(o["exchange_id"]) for o in open_orders if self.ownership.order_owner(o["order_id"]) == SID}
        # orders that left the book: stamp when first seen gone
        for job in self.races.values():
            for o in job.get("orders", []):
                if o[1] and o[1] not in open_ids and (len(o) < 5 or o[4] is None):
                    while len(o) < 5:
                        o.append(None)
                    if now - float(o[0]) >= 1.0:
                        o[4] = now
                        changed = True
        # new triggers (step marks each race fired once)
        for f in step(legs, self.night, sc, now):
            self.races[f["race"]] = {"exchange_id": str(f["exchange_id"]), "fired_at": now, "poly_at_fire": f["poly"],
                                     "edge_c": f["edge_c"], "orders": [], "attempts": 0, "skip": None}
            out["fired"] += 1
            changed = True
        by_ex = {str(l["exchange_id"]): l for l in legs}
        self._last_committed = self.committed(open_ids, fills_by_order)
        budget_left = self.cfg.budget_usd - self._last_committed
        try:
            reserved = float(self.others_reserved()) if callable(self.others_reserved) else 0.0
        except Exception:  # noqa: BLE001
            reserved = float("inf")
        cash = float("inf") if free_cash is None else float(free_cash) - max(reserved, 0.0)
        # candidate orders this tick, biggest current edge first
        cands = []
        for race, job in self.races.items():
            ex = job["exchange_id"]
            l = by_ex.get(ex)
            if job.get("skip") or job["attempts"] >= self.cfg.max_attempts or l is None:
                continue
            if ex in mine_open or any(o[1] is None and now - float(o[0]) < self.cfg.pending_ttl_s
                                      for o in job["orders"]):
                continue                                    # one live order per market (lost reply: wait it out)
            # our previous order can't still rest once its venue expiry passed - independent of (lagging) reads
            if job["orders"] and now - float(job["orders"][-1][0]) < max(self.cfg.retry_after_s,
                                                                         self.cfg.order_expiry_s + 15.0):
                continue
            p, ask, k = l.get("poly"), l.get("sig_ask"), l.get("kalshi")
            if p is None or ask is None or p < sc.theta or (k is not None and k < sc.theta):
                continue                                    # call no longer confirmed / no fresh book: not now
            if ask > p - sc.min_edge:
                continue                                    # SIG caught up
            spent_race = sum(float(o[2]) * float(o[3]) for o in job["orders"])
            room = self.cfg.usd_per_race - spent_race
            if room < self.cfg.min_order_usd:
                continue
            limit = round(min(ask + sc.max_sweep, p - sc.min_edge), 3)
            cands.append((p - limit, race, job, ex, limit, room))
        for edge, race, job, ex, limit, room in sorted(cands, key=lambda c: -c[0]):
            if self.paused or not self._can_write():
                out["skipped_writes"] = out.get("skipped_writes", 0) + 1
                break
            usd = min(room, budget_left, cash)
            qty = int(usd / max(limit, 0.01))
            if qty < 1 or qty * limit < self.cfg.min_order_usd:
                out["cash_limited"] = out.get("cash_limited", 0) + 1
                continue
            owner = self.ownership.owner_of(ex)
            if owner not in (None, SID):
                job["skip"] = f"owned_by_{owner}"
                changed = True
                continue
            if self.dry:
                self._dry_record(race, ex, limit, qty, edge, by_ex.get(ex))
                job["attempts"] = self.cfg.max_attempts     # one record per race in dry mode
                out["dry"] += 1
                changed = True
                continue
            if owner is None:
                got = self.ownership.claim(SID, [ex])
                if ex not in got:
                    job["skip"] = "claim_failed"
                    changed = True
                    continue
            self._writes.append(now)
            job["attempts"] += 1
            changed = True
            try:
                res = await self.broker.place_order(strategy_id=SID, market_id=f"{ex}#{uuid.uuid4().hex[:12]}",
                                                    exchange_id=ex, side="buy", action="buy", quantity=qty,
                                                    price=limit, outcome="yes", expiration_s=self.cfg.order_expiry_s,
                                                    send_deadline_s=self.cfg.send_deadline_s)
            except Exception as exc:  # noqa: BLE001 — reply lost: may have reached the venue -> full notional
                job["orders"].append([now, None, qty, limit, None])
                out.setdefault("place_errors", []).append(type(exc).__name__)
                budget_left -= qty * limit
                cash -= qty * limit
                continue
            if getattr(res, "status", "") == "rejected":
                continue
            oid = getattr(res, "order_id", None)
            job["orders"].append([now, str(oid) if oid else None, qty, limit, None])
            budget_left -= qty * limit
            cash -= qty * limit
            out["placed"] += 1
        self._last_committed = self.committed(open_ids, fills_by_order)
        if changed:
            self.save()
        out["committed"] = self._last_committed
        out["races"] = len(self.races)
        return out

    def _dry_record(self, race, ex, limit, qty, edge, leg) -> None:
        os.makedirs(os.path.dirname(self.cfg.dry_path) or ".", exist_ok=True)
        with open(self.cfg.dry_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": self.clock(), "race": race, "exchange_id": ex, "limit": limit, "qty": qty,
                                 "usd": round(qty * limit, 2), "edge_c": round(100 * edge, 2), "leg": leg}) + "\n")
