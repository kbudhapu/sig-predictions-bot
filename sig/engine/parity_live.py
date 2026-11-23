"""D+R PARITY — LIVE pilot (2026-10-22). Executes the parity shadow (sig/engine/parity.py; 308 round trips since
10-08, every one >= +1.0c/pair, median hold 15 min): when a race's D and R YES bids sum to >= 1 + entry_edge, buy NO
on BOTH legs (pair cost = 2 - d_bid - r_bid < 1; a NO pair always pays >= 1, so that edge is LOCKED), and sell both
NOs again once the round trip realizes >= exit_pnl. Capital recycles in minutes.

What the shadow ignores and this handles: the two legs fill independently.
ONE STATE MACHINE PER PAIR:  probation -> entering -> (balancing) -> holding -> exiting -> done (markets released)
  * ISOLATION: both legs are CLAIMED (sig/engine/ownership.py) only in races where we hold NOTHING in either leg and
    nobody has an open order; judged on the next read (probation). While owned the broker refuses everyone else, so
    the venue position in a leg IS this pair's position.
  * QUIET RULE: after any order we act on positions only once none of our orders rests on the pair AND settle_s has
    passed since the last order (stale position reads were the rotation oversell hazard).
  * LEGGING: unequal fills -> buy the short leg, but only at a price keeping the pair cost <= 1 - min_lock (edge stays
    locked); after balance_attempts, sell the excess on the heavy leg instead (bounded loss, never a naked NO left).
  * Sells are reduce-only (never more than the NO held); buys never exceed the pair's target size.
  * Capital: hard budget_usd; every ACTIVE pair reserves its full notional; the unused budget is carved out of the
    orchestrator's ledger; each order also needs free cash minus the other strategies' carve-outs.
  * Caps: pairs (shares per pair), max_open pairs, max_writes_per_min, max_loss_usd (realized, from fills at our limit
    prices) -> wind-down. STOP file / election cutoff -> wind-down: no entries; holding pairs exit if the round trip is
    >= -wind_down_max_loss, otherwise they HOLD their locked edge to settlement. Restart-safe (state_path).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SID = "parity"


@dataclass(frozen=True)
class ParityLiveConfig:
    pairs: int = 400                        # NO shares per leg per trade
    budget_usd: float = 3000.0
    max_open: int = 4
    entry_edge: float = 0.02                # d_bid + r_bid - 1 at entry (locked per pair)
    exit_pnl: float = 0.01                  # realized per pair to exit
    min_lock: float = 0.0                   # balancing never lets the pair cost exceed 1 - min_lock
    wind_down_max_loss: float = 0.005       # wind-down exits a pair only if its round trip is >= -this
    order_expiry_s: float = 60.0
    send_deadline_s: float = 10.0
    settle_s: float = 90.0                  # quiet period after our last order before reading positions
    pending_ttl_s: float = 180.0
    balance_attempts: int = 3
    max_writes_per_min: int = 10
    max_loss_usd: float = 150.0
    max_quote_age_s: float = 60.0
    resume_after_clear_ticks: int = 5
    stop_file: str = "data/parity_live/STOP"
    state_path: str = "data/parity_live/state.json"
    election_cutoff_iso: str = "2026-11-02T00:00:00+00:00"


@dataclass
class ParityLive:
    broker: Any
    ownership: Any
    cfg: ParityLiveConfig = field(default_factory=ParityLiveConfig)
    clock: Any = time.time
    others_reserved: Any = None
    active: dict = field(default_factory=dict)       # race -> pair state
    history: list = field(default_factory=list)      # closed pairs
    realized: float = 0.0
    winding_down: bool = False
    halt_reason: str | None = None
    paused: bool = False
    _clear: int = 0
    _writes: list = field(default_factory=list)
    _gen: int = 0

    # ---------------- persistence ----------------
    def load(self) -> None:
        try:
            with open(self.cfg.state_path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        self.active = dict(d.get("active") or {})
        self.history = list(d.get("history") or [])
        self.realized = float(d.get("realized") or 0.0)
        for p in self.active.values():
            if p.get("phase") == "probation":
                p["gen"] = -1
            p["last_order"] = float(p.get("last_order") or 0.0)
        # owned legs with no active pair (lost state) and nothing held/resting -> release
        mine = set(self.ownership.owned_by(SID))
        legs = {x for p in self.active.values() for x in (p["d"], p["r"])}
        for ex in mine - legs:
            if not self.ownership.orders_in(SID, ex):
                self._orphans = getattr(self, "_orphans", set()) | {ex}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.cfg.state_path) or ".", exist_ok=True)
        tmp = self.cfg.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"active": self.active, "history": self.history[-1000:], "realized": round(self.realized, 4)}, fh)
        os.replace(tmp, self.cfg.state_path)

    # ---------------- capital / writes / kill ----------------
    def reserved(self) -> float:
        return round(sum(float(p.get("reserve", 0.0)) for p in self.active.values()), 2)

    def unused_budget(self) -> float:
        """Ledger carve-out: budget not reserved by an active pair (cross must not take it). 0 once winding down."""
        if self.winding_down:
            return 0.0
        return max(round(self.cfg.budget_usd - self.reserved(), 2), 0.0)

    def _can_write(self, n: int = 1) -> bool:
        now = self.clock()
        self._writes = [t for t in self._writes if now - t < 60.0]
        return len(self._writes) + n <= self.cfg.max_writes_per_min

    def halt(self, reason: str) -> None:
        if not self.winding_down:
            self.winding_down, self.halt_reason = True, reason

    def check_kill(self, orchestrator_halted: bool = False) -> None:
        if os.path.exists(self.cfg.stop_file):
            self.halt("stop_file")
        try:
            if self.clock() >= datetime.fromisoformat(self.cfg.election_cutoff_iso).timestamp():
                self.halt("election_cutoff")
        except (TypeError, ValueError):
            self.halt("bad_cutoff")
        if self.realized <= -abs(self.cfg.max_loss_usd):
            self.halt(f"loss_limit {self.realized:.2f}")
        if getattr(self.ownership, "corrupt", False):
            self.halt("ownership_registry_corrupt")
        if orchestrator_halted:
            self.paused, self._clear = True, 0
        elif self.paused:
            self._clear += 1
            if self._clear >= self.cfg.resume_after_clear_ticks:
                self.paused, self._clear = False, 0

    # ---------------- one tick ----------------
    async def tick(self, quotes: dict, positions: dict, open_orders: list | None, free_cash: float | None) -> dict:
        """quotes: race -> {d, r, d_bid, d_ask, r_bid, r_ask} (fresh only); positions: ex -> signed YES qty (whole
        account); open_orders: venue open orders (None = read failed -> skip)."""
        out = {"placed": 0, "active": len(self.active), "winding_down": self.winding_down, "paused": self.paused}
        if open_orders is None:
            out["skipped"] = "open_orders_read_failed"
            return out
        now = self.clock()
        changed = False
        busy_ex = {str(o["exchange_id"]) for o in open_orders}
        mine_ex = {str(o["exchange_id"]) for o in open_orders if self.ownership.order_owner(o["order_id"]) == SID}
        for ex in list(getattr(self, "_orphans", set())):
            if positions.get(ex, 0.0) == 0 and ex not in mine_ex:
                self.ownership.release(SID, ex)
            self._orphans.discard(ex)

        def no_held(ex) -> float:
            return max(-float(positions.get(ex, 0.0)), 0.0)

        def other_long(ex) -> bool:
            return float(positions.get(ex, 0.0)) > 0.5                  # YES held in a leg: not ours to net

        def quiet(p) -> bool:
            if p["d"] in mine_ex or p["r"] in mine_ex:
                return False
            # our previous order can't still rest once its venue expiry passed - independent of (lagging) reads
            return now - float(p.get("last_order", 0.0)) >= max(self.cfg.settle_s, self.cfg.order_expiry_s + 15.0)

        try:
            reserved_others = float(self.others_reserved()) if callable(self.others_reserved) else 0.0
        except Exception:  # noqa: BLE001
            reserved_others = float("inf")
        cash = float("inf") if free_cash is None else float(free_cash) - max(reserved_others, 0.0)

        async def place(p, leg, action, px, qty) -> bool:
            nonlocal cash
            ex = p[leg]
            px = round(min(max(px, 0.001), 0.999), 3)
            if qty < 1 or self.paused or not self._can_write():
                out["skipped_writes"] = out.get("skipped_writes", 0) + (0 if qty < 1 else 1)
                return False
            if action == "buy" and qty * px > cash:
                out["cash_limited"] = out.get("cash_limited", 0) + 1
                return False
            self._writes.append(now)
            p["last_order"] = now
            try:
                res = await self.broker.place_order(strategy_id=SID, market_id=f"{ex}#{uuid.uuid4().hex[:12]}",
                                                    exchange_id=ex, side=action, action=action, quantity=int(qty),
                                                    price=px, outcome="no", expiration_s=self.cfg.order_expiry_s,
                                                    send_deadline_s=self.cfg.send_deadline_s)
            except Exception as exc:  # noqa: BLE001 — may have reached the venue; the quiet rule waits it out
                p.setdefault("orders", []).append([now, None, leg, action, int(qty), px])
                p["last_order"] = now + self.cfg.pending_ttl_s           # wait out a possibly-live lost order
                out.setdefault("place_errors", []).append(type(exc).__name__)
                return False
            if getattr(res, "status", "") == "rejected":
                return False
            oid = getattr(res, "order_id", None)
            p.setdefault("orders", []).append([now, str(oid) if oid else None, leg, action, int(qty), px])
            if action == "buy":
                cash -= qty * px
            out["placed"] += 1
            return True

        def book_fills(p) -> None:
            """Attribute position changes since the last quiet read to the last order on each leg (its limit)."""
            for leg in ("d", "r"):
                h = no_held(p[leg])
                prev = float(p.get(f"seen_{leg}", 0.0))
                if abs(h - prev) < 0.5:
                    continue
                last = next((o for o in reversed(p.get("orders", [])) if o[2] == leg), None)
                px = float(last[5]) if last else 0.0
                if h > prev:
                    p["cost"] = float(p.get("cost", 0.0)) + (h - prev) * px
                else:
                    p["proceeds"] = float(p.get("proceeds", 0.0)) + (prev - h) * px
                p[f"seen_{leg}"] = h

        # ---------------- manage active pairs ----------------
        for race, p in list(self.active.items()):
            q = quotes.get(race)
            d, r = p["d"], p["r"]
            ph = p["phase"]
            if ph == "probation":
                if p.get("gen", 0) < self._gen:
                    foreign = any(str(o["exchange_id"]) in (d, r) and self.ownership.order_owner(o["order_id"]) != SID
                                  for o in open_orders)
                    if foreign or no_held(d) > 0.5 or no_held(r) > 0.5 or other_long(d) or other_long(r) \
                            or self.winding_down or q is None:
                        p["phase"] = "done"
                        p["result"] = "aborted_probation"
                    else:
                        p["phase"] = "entering"
                    changed = True
                continue
            if not quiet(p):
                continue
            book_fills(p)
            hd, hr = no_held(d), no_held(r)
            if ph == "entering":
                if not p.get("entered"):
                    if q is None or q["d_bid"] + q["r_bid"] - 1.0 < self.cfg.entry_edge - 1e-9 or self.winding_down:
                        p["phase"], p["result"] = "done", "edge_gone"
                    elif self._can_write(2):
                        ok_d = await place(p, "d", "buy", 1.0 - q["d_bid"], p["n"])
                        ok_r = await place(p, "r", "buy", 1.0 - q["r_bid"], p["n"]) if ok_d else False
                        p["entered"] = True
                        p["px_d"], p["px_r"] = round(1.0 - q["d_bid"], 3), round(1.0 - q["r_bid"], 3)
                        if not ok_d:
                            p["phase"], p["result"] = "done", "entry_not_placed"
                    changed = True
                elif abs(hd - hr) < 0.5:
                    p["phase"] = "holding" if hd >= 0.5 else "done"
                    if hd < 0.5:
                        p["result"] = "nothing_filled"
                    changed = True
                else:
                    p["phase"], p["attempts"] = "balancing", 0
                    changed = True
                continue
            if ph == "balancing":
                if abs(hd - hr) < 0.5:
                    p["phase"] = "holding" if hd >= 0.5 else "done"
                    changed = True
                    continue
                short, heavy = ("d", "r") if hd < hr else ("r", "d")
                k = abs(hd - hr)
                paid_heavy = float(p.get(f"px_{heavy}", 0.0))
                cap = 1.0 - self.cfg.min_lock - paid_heavy               # pair cost stays <= 1 - min_lock
                if p["attempts"] < self.cfg.balance_attempts and q is not None and not self.winding_down:
                    no_ask = 1.0 - q[f"{short}_bid"]
                    if no_ask <= cap + 1e-9:
                        if await place(p, short, "buy", no_ask, k):
                            p[f"px_{short}"] = round(no_ask, 3)
                    p["attempts"] += 1
                elif q is not None:
                    sell_px = 1.0 - q[f"{heavy}_ask"]                    # NO bid = 1 - YES ask
                    await place(p, heavy, "sell", sell_px, min(k, no_held(p[heavy])))
                changed = True
                continue
            if ph == "holding":
                if hd < 0.5 and hr < 0.5:
                    p["phase"] = "done"
                    changed = True
                    continue
                if abs(hd - hr) >= 0.5:
                    p["phase"], p["attempts"] = "balancing", self.cfg.balance_attempts   # unwind the excess
                    changed = True
                    continue
                if q is None:
                    continue
                pnl = (float(p["d_bid0"]) + float(p["r_bid0"])) - (q["d_ask"] + q["r_ask"])
                want = pnl >= self.cfg.exit_pnl - 1e-9 or (self.winding_down and pnl >= -self.cfg.wind_down_max_loss - 1e-9)
                if want and self._can_write(2):
                    await place(p, "d", "sell", 1.0 - q["d_ask"], hd)
                    await place(p, "r", "sell", 1.0 - q["r_ask"], hr)
                    p["phase"], p["attempts"] = "exiting", 0
                    changed = True
                continue
            if ph == "exiting":
                if hd < 0.5 and hr < 0.5:
                    p["phase"] = "done"
                    changed = True
                    continue
                if q is not None:                                  # until flat: reduce-only, spaced by quiet()
                    for leg, h in (("d", hd), ("r", hr)):
                        if h >= 0.5:
                            await place(p, leg, "sell", 1.0 - q[f"{leg}_ask"], h)   # reduce-only remainder
                    p["attempts"] += 1
                    changed = True
                continue
        # ---------------- close finished pairs ----------------
        for race, p in list(self.active.items()):
            if p["phase"] != "done":
                continue
            d, r = p["d"], p["r"]
            if d in mine_ex or r in mine_ex or no_held(d) > 0.5 or no_held(r) > 0.5:
                continue                                                 # never release while we hold / rest
            if now - float(p.get("last_order", 0.0)) < self.cfg.settle_s:
                continue
            book_fills(p)
            pnl = round(float(p.get("proceeds", 0.0)) - float(p.get("cost", 0.0)), 4)
            self.realized += pnl
            for ex in (d, r):
                self.ownership.release(SID, ex)
            self.history.append({"race": race, "d": d, "r": r, "n": p["n"], "edge0": p.get("edge0"),
                                 "result": p.get("result", "round_trip"), "pnl": pnl, "opened": p.get("opened"),
                                 "closed": now})
            del self.active[race]
            out.setdefault("closed", []).append(race)
            changed = True
        # ---------------- new entries (biggest edge first) ----------------
        if not self.winding_down and not self.paused:
            held_any = {ex for ex, qy in positions.items() if abs(float(qy)) >= 1}
            cands = []
            for race, q in quotes.items():
                if race in self.active:
                    continue
                d, r = str(q["d"]), str(q["r"])
                edge = q["d_bid"] + q["r_bid"] - 1.0
                if edge < self.cfg.entry_edge - 1e-9:
                    continue
                if {d, r} & (busy_ex | held_any):
                    continue
                if self.ownership.owner_of(d) is not None or self.ownership.owner_of(r) is not None:
                    continue
                cands.append((edge, race, q))
            for edge, race, q in sorted(cands, key=lambda c: -c[0]):
                if len(self.active) >= self.cfg.max_open:
                    break
                n = int(self.cfg.pairs)
                reserve = round(n * (2.0 - q["d_bid"] - q["r_bid"]), 2)
                if self.reserved() + reserve > self.cfg.budget_usd + 1e-6 or reserve > cash:
                    out["budget_limited"] = out.get("budget_limited", 0) + 1
                    break
                got = self.ownership.claim(SID, [q["d"], q["r"]])
                if set(got) >= {str(q["d"]), str(q["r"])}:
                    self.active[race] = {"d": str(q["d"]), "r": str(q["r"]), "n": n, "phase": "probation",
                                         "gen": self._gen, "reserve": reserve, "edge0": round(edge, 4),
                                         "d_bid0": q["d_bid"], "r_bid0": q["r_bid"], "opened": now,
                                         "last_order": 0.0, "orders": []}
                    out.setdefault("claimed", []).append(race)
                    changed = True
                else:
                    for ex in got:
                        self.ownership.release(SID, ex)
        self._gen += 1
        if changed:
            self.save()
        out["active"] = len(self.active)
        out["realized"] = round(self.realized, 2)
        return out
