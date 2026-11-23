"""CAPITAL ROTATION — live trader (2026-10-11). Executes the rotation v2 decision (sig/engine/rotation_v2.decide_v2, the
retuned shadow: top-ups, 3c margin, 60-min persistence, 24h freeze) for real: sell part of a holding whose remaining
edge is small, buy a fresher, bigger edge with the proceeds. Shadow evidence (9 swaps): +6.5 per $100 swapped at the
Polymarket fair, turned positive at SIG marks after 1 day.

ONE SWAP AT A TIME, as a state machine:  probation -> selling -> buying -> done (markets released).
ISOLATION (same model as fair_band_live / fresh_gap_live):
  * Both markets are CLAIMED (sig/engine/ownership.py) before any order, only if nobody owns them and neither has an
    open order; the claim is judged on the NEXT read (probation): a foreign order there -> release, abort. While we
    own them the broker refuses every other strategy (cross cannot re-buy / exit / chase into the swap).
  * SELL is reduce-only: never more than the venue position on the held side minus our own unconfirmed sells.
  * BUY is funded only by THIS swap's realised proceeds, and never more than free cash minus every other strategy's
    carve-out; the unspent proceeds are carved out of the orchestrator's ledger while buying (cross can't take them).
  * Marketable limits at the touch with a 120s venue expiry; spread on each leg must be <= max_spread.
  * Unconfirmed fills: an order that left the book counts as filled until resolved from the ingested fills
    (resolve_after_gone_s) or, without fills data, for confirm_s (oversell / double-buy guard).
  * Caps: max_swap_usd per swap, max_swaps_per_day, max_writes_per_min. STOP file / election cutoff -> no new swaps
    (an active swap finishes its BUY so proceeds are never stranded in cash... unless the buy can't be done safely,
    then it releases with cash). Orchestrator halt -> pause (no writes).
  * Restart-safe: the active swap + history persisted to state_path.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

SID = "rotation"


def _size(v) -> float | None:
    try:
        return None if v is None else max(float(v), 0.0)
    except (TypeError, ValueError):
        return None


def _cap(depth: float | None) -> float:
    return float("inf") if depth is None else depth


def sell_depth_ok(row: dict | None, qty_yes: float, min_usd: float) -> bool:
    """Is a holding worth proposing as a SELL source? False only when the touch depth on its held side is KNOWN and
    worth less than min_usd (a thin bid turned two swaps into $277 sales that each used a daily swap slot)."""
    r = row or {}
    held_yes = qty_yes > 0
    size = _size(r.get("bid_size") if held_yes else r.get("ask_size"))
    if size is None:
        return True
    try:
        px = float(r["best_bid"]) if held_yes else 1.0 - float(r["best_ask"])
    except (KeyError, TypeError, ValueError):
        return True
    return size * px >= min_usd


@dataclass(frozen=True)
class RotationLiveConfig:
    max_swap_usd: float = 2000.0
    max_swaps_per_day: int = 6
    max_writes_per_min: int = 6
    order_expiry_s: float = 120.0
    send_deadline_s: float = 20.0
    max_spread: float = 0.03
    max_row_age_s: float = 60.0
    min_order_usd: float = 100.0
    leg_attempts: int = 4                  # sell / buy attempts before the leg is considered done
    sell_attempts: int = 8                 # SELL leg: orders rest at the bid and fill as the (thin) bid refills
    pending_ttl_s: float = 180.0
    pending_min_reads: int = 2
    confirm_s: float = 600.0
    resolve_after_gone_s: float = 150.0
    freeze_s: float = 86400.0
    resume_after_clear_ticks: int = 5
    stop_file: str = "data/rotation_live/STOP"
    state_path: str = "data/rotation_live/state.json"
    election_cutoff_iso: str = "2026-11-02T00:00:00+00:00"


@dataclass
class _Pending:
    exchange_id: str
    action: str
    order_id: str | None
    ts: float
    reads: int = 0


@dataclass
class RotationLive:
    broker: Any
    ownership: Any
    cfg: RotationLiveConfig = field(default_factory=RotationLiveConfig)
    clock: Any = time.time
    others_reserved: Any = None
    swap: dict | None = None                         # the active swap
    history: list = field(default_factory=list)      # closed swaps (persisted; drives freeze + daily cap)
    stopped: bool = False
    stop_reason: str | None = None
    paused: bool = False
    _clear: int = 0
    _writes: list = field(default_factory=list)
    _pending: list = field(default_factory=list)
    _gen: int = 0

    # ---------------- persistence ----------------
    def load(self) -> None:
        try:
            with open(self.cfg.state_path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.swap, self.history = d.get("swap"), list(d.get("history") or [])
        except (OSError, ValueError):
            self.swap, self.history = None, []
        if self.swap and self.swap.get("phase") == "probation":
            self.swap["gen"] = -1                    # tick counter restarts: judge on the first read
        # markets we own with no active swap (lost state) -> release (positions belong to cross again)
        mine = set(self.ownership.owned_by(SID))
        active = {self.swap["sell_ex"], self.swap["buy_ex"]} if self.swap else set()
        for ex in mine - active:
            if not self.ownership.orders_in(SID, ex):
                self.ownership.release(SID, ex)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.cfg.state_path) or ".", exist_ok=True)
        tmp = self.cfg.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"swap": self.swap, "history": self.history[-500:]}, fh)
        os.replace(tmp, self.cfg.state_path)

    # ---------------- capital / writes / kill ----------------
    def unused_budget(self) -> float:
        """Ledger carve-out: proceeds of the active swap not yet spent on its buy leg (cross must not take them)."""
        s = self.swap
        if not s or s.get("phase") != "buying":
            return 0.0
        return max(round(float(s.get("proceeds", 0.0)) - float(s.get("spent", 0.0)), 6), 0.0)

    def _can_write(self) -> bool:
        now = self.clock()
        self._writes = [t for t in self._writes if now - t < 60.0]
        return len(self._writes) < self.cfg.max_writes_per_min

    def _past_cutoff(self) -> bool:
        from datetime import datetime
        try:
            return self.clock() >= datetime.fromisoformat(self.cfg.election_cutoff_iso).timestamp()
        except (TypeError, ValueError):
            return True

    def check_kill(self, orchestrator_halted: bool = False) -> None:
        if os.path.exists(self.cfg.stop_file):
            self.stopped, self.stop_reason = True, "stop_file"
        if self._past_cutoff():
            self.stopped, self.stop_reason = True, "election_cutoff"
        if getattr(self.ownership, "corrupt", False):
            self.stopped, self.stop_reason = True, "ownership_registry_corrupt"
        if orchestrator_halted:
            self.paused, self._clear = True, 0
        elif self.paused:
            self._clear += 1
            if self._clear >= self.cfg.resume_after_clear_ticks:
                self.paused, self._clear = False, 0

    def frozen(self, now: float) -> set:
        out = set()
        for h in self.history:
            if now - float(h.get("ts", 0)) < self.cfg.freeze_s:
                out |= {h.get("sell_ex"), h.get("buy_ex")}
        return out

    def swaps_today(self, now: float) -> int:
        return sum(1 for h in self.history if now - float(h.get("ts", 0)) < 86400.0)

    # ---------------- one tick ----------------
    async def tick(self, proposal: dict | None, rows: dict, positions: dict, open_orders: list | None,
                   free_cash: float | None, fills_by_order: dict | None = None,
                   market_fills: dict | None = None) -> dict:
        """proposal: {sell_ex, sell_qty (signed YES), buy_ex, buy_side, advantage} or None (from decide_v2).
        rows: ex -> {best_bid, best_ask, bid_size, ask_size, age_s}; positions: ex -> signed YES qty (whole account).
        market_fills: ex -> {sold_qty, sold_usd, bought_qty, bought_usd} of fills in the swap's markets since the swap
        started (the markets are OURS exclusively, so every fill there is ours) — authoritative when present."""
        out = {"placed": 0, "phase": (self.swap or {}).get("phase"), "stopped": self.stopped, "paused": self.paused}
        if open_orders is None:
            out["skipped"] = "open_orders_read_failed"
            return out
        now = self.clock()
        if self._past_cutoff():
            self.stopped, self.stop_reason = True, "election_cutoff"
        changed = False
        shown = {str(o["order_id"]) for o in open_orders}
        keep = []
        for p in self._pending:
            if p.order_id is not None and p.order_id in shown:
                continue
            p.reads += 1
            if p.reads >= self.cfg.pending_min_reads and now - p.ts >= self.cfg.pending_ttl_s:
                continue
            keep.append(p)
        self._pending = keep
        pend_ids = {p.order_id for p in self._pending if p.order_id}

        def book(ex):
            r = rows.get(ex) or {}
            try:
                b, a = float(r["best_bid"]), float(r["best_ask"])
            except (KeyError, TypeError, ValueError):
                return None
            if not (0.0 < b < a < 1.0) or float(r.get("age_s", 0.0)) > self.cfg.max_row_age_s:
                return None
            if a - b > self.cfg.max_spread + 1e-9:
                return None
            # sizes: None = UNKNOWN (the price stream carries none; only some markets get a depth poll) -> no depth
            # cap (the limit + venue expiry bound the order). 2026-10-13: unknown read as 0 -> 4 zero-size buys ->
            # swap 847->1028 sold $277 and bought nothing.
            return b, a, _size(r.get("bid_size")), _size(r.get("ask_size"))

        mine = [o for o in open_orders if self.ownership.order_owner(o["order_id"]) == SID]

        def live(ex):
            return ([o for o in mine if str(o["exchange_id"]) == ex] +
                    [p for p in self._pending if p.exchange_id == ex])

        def held(ex, side):
            q = positions.get(ex, 0.0)
            return q if side == "yes" else -q

        def unconfirmed(rec_list, q_now, action):
            rec, assumed = [], 0.0
            for x in rec_list:
                if x[1] != action or now - float(x[0]) >= self.cfg.confirm_s:
                    continue
                oid = x[4]
                if oid and (oid in shown or oid in pend_ids):
                    continue
                qty = float(x[2])
                if oid:
                    if not x[5]:
                        x[5] = now
                    if fills_by_order is not None and now - float(x[5]) >= self.cfg.resolve_after_gone_s:
                        qty = float(fills_by_order.get(oid, 0.0))
                rec.append(x)
                assumed += qty
            if not rec:
                return 0.0
            base = float(rec[0][3])
            seen = (base - q_now) if action == "sell" else (q_now - base)
            return max(0.0, assumed - max(seen, 0.0))

        def ambiguous(action) -> bool:
            """An order of this leg whose outcome can never be resolved by id (reply lost): place nothing more on
            the leg (an unresolvable sell + stale positions was an oversell in the break test)."""
            return any(x[1] == action and not x[4] for x in (self.swap or {}).get("recent", []))

        def mf(ex, key) -> float:
            return float(((market_fills or {}).get(ex) or {}).get(key, 0.0) or 0.0)

        async def place(ex, action, side, px, qty, q_now) -> bool:
            if self.paused or not self._can_write():
                out["skipped_writes"] = out.get("skipped_writes", 0) + 1
                return False
            self._writes.append(now)
            try:
                res = await self.broker.place_order(strategy_id=SID, market_id=f"{ex}#{uuid.uuid4().hex[:12]}",
                                                    exchange_id=ex, side=action, action=action, quantity=int(qty),
                                                    price=round(px, 3), outcome=side,
                                                    expiration_s=self.cfg.order_expiry_s,
                                                    send_deadline_s=self.cfg.send_deadline_s)
            except Exception as exc:  # noqa: BLE001 — ambiguous: may have reached the venue
                self._pending.append(_Pending(ex, action, None, now))
                self.swap.setdefault("recent", []).append([now, action, int(qty), q_now, None, None])
                out.setdefault("place_errors", []).append(type(exc).__name__)
                return False
            if getattr(res, "status", "") == "rejected":
                return False
            oid = getattr(res, "order_id", None)
            self._pending.append(_Pending(ex, action, str(oid) if oid else None, now))
            self.swap.setdefault("recent", []).append([now, action, int(qty), q_now, str(oid) if oid else None, None])
            out["placed"] += 1
            return True

        s = self.swap
        # ---------------- start a new swap ----------------
        if s is None:
            if (proposal and not self.stopped and not self.paused
                    and self.swaps_today(now) < self.cfg.max_swaps_per_day):
                a, b = str(proposal["sell_ex"]), str(proposal["buy_ex"])
                busy = {str(o["exchange_id"]) for o in open_orders}
                fz = self.frozen(now)
                max_usd = min(self.cfg.max_swap_usd, float(proposal.get("max_buy_usd") or self.cfg.max_swap_usd))
                ok = (max_usd >= self.cfg.min_order_usd
                      and a != b and a not in busy and b not in busy and a not in fz and b not in fz
                      and self.ownership.owner_of(a) is None and self.ownership.owner_of(b) is None
                      and book(a) is not None and book(b) is not None)
                sell_side = "yes" if positions.get(a, 0.0) > 0 else "no"
                if ok and abs(positions.get(a, 0.0)) >= 1 and (positions.get(b, 0.0) == 0 or
                                                               (positions.get(b, 0.0) > 0) == (proposal["buy_side"] == "yes")):
                    got = self.ownership.claim(SID, [a, b])
                    if set(got) >= {a, b}:
                        self.swap = s = {"id": uuid.uuid4().hex[:10], "sell_ex": a, "sell_side": sell_side,
                                         "buy_ex": b, "buy_side": str(proposal["buy_side"]), "phase": "probation",
                                         "gen": self._gen, "epoch": now, "advantage": proposal.get("advantage"),
                                         "sell_q0": held(a, sell_side), "buy_q0": max(held(b, str(proposal["buy_side"])), 0.0),
                                         "proceeds": 0.0, "spent": 0.0, "attempts": 0, "recent": [],
                                         "max_usd": max_usd}
                        out["claimed"] = [a, b]
                        changed = True
                    else:
                        for ex in got:
                            self.ownership.release(SID, ex)
            self._gen += 1
            if changed:
                self._save()
            return out

        a, b = s["sell_ex"], s["buy_ex"]
        ss, bs = s["sell_side"], s["buy_side"]
        qa, qb = held(a, ss), held(b, bs)
        # ---------------- probation: judged on a read taken after the claim ----------------
        if s["phase"] == "probation":
            if s.get("gen", 0) < self._gen:
                foreign = any(str(o["exchange_id"]) in (a, b) and self.ownership.order_owner(o["order_id"]) != SID
                              for o in open_orders)
                if foreign or qa < 1 or self.stopped:
                    self._finish(s, "aborted_probation" if foreign else ("aborted_no_position" if qa < 1 else
                                                                          "aborted_stopped"), now)
                    out["aborted"] = s["id"]
                else:
                    s["phase"], s["attempts"] = "selling", 0
                    s["sell_target"] = 0.0          # sized below, on the first selling tick
                changed = True
        # ---------------- selling (reduce-only) ----------------
        elif s["phase"] == "selling":
            pos_drop = max(float(s["sell_q0"]) - max(qa, 0.0), 0.0)
            sold = max(pos_drop, mf(a, "sold_qty")) + unconfirmed(s["recent"], max(qa, 0.0), "sell")
            bk = book(a)
            if not s.get("sell_target") and bk is not None:
                sell_px = bk[0] if ss == "yes" else 1.0 - bk[1]
                # sell only what the buy market has ROOM for under its cap (never strand proceeds as cash)
                cap_usd = float(s.get("max_usd") or self.cfg.max_swap_usd)
                # NOT capped by the touch depth (2026-10-22): the low-edge holdings sit behind 33-490 share bids;
                # a sell limit AT the bid never sells below it and the rest fills as buyers arrive
                s["sell_target"] = float(math.floor(min(float(s["sell_q0"]), cap_usd / max(sell_px, 0.01))))
                changed = True
            target = float(s.get("sell_target") or 0.0)
            if live(a):
                pass
            elif (sold >= 0.95 * target and target > 0 or s["attempts"] >= self.cfg.sell_attempts or target < 1
                  or ambiguous("sell")):
                # leg done: proceeds = what was really sold (confirmed position drop) at the sale price
                px = float(s.get("sell_px") or 0.0)
                if unconfirmed(s["recent"], max(qa, 0.0), "sell") >= 1:
                    pass                             # wait: don't buy against cash we can't confirm yet
                else:
                    # proceeds = CONFIRMED sales only: the fills in our (exclusive) market, else the position drop
                    s["proceeds"] = round(mf(a, "sold_usd") if mf(a, "sold_qty") > 0 else pos_drop * px, 2)
                    s["phase"], s["attempts"] = ("buying" if s["proceeds"] >= self.cfg.min_order_usd else "done"), 0
                    changed = True
            elif bk is not None and not self.stopped:
                sell_px = bk[0] if ss == "yes" else 1.0 - bk[1]
                sellable = max(qa, 0.0) - unconfirmed(s["recent"], max(qa, 0.0), "sell")
                qty = int(math.floor(min(target - sold, sellable)))     # reduce-only; limit at the bid
                if qty >= 1 and qty * sell_px >= self.cfg.min_order_usd:
                    if await place(a, "sell", ss, sell_px, qty, max(qa, 0.0)):
                        s["sell_px"] = sell_px
                    s["attempts"] += 1
                    changed = True
                else:
                    s["attempts"] = self.cfg.sell_attempts
                    changed = True
            elif self.stopped:
                s["attempts"] = self.cfg.sell_attempts   # stop selling; whatever sold is bought below
                changed = True
        # ---------------- buying (funded by this swap's proceeds only) ----------------
        elif s["phase"] == "buying":
            bought_pos = max(qb - float(s["buy_q0"]), 0.0)
            bk = book(b)
            buy_px = None if bk is None else (bk[1] if bs == "yes" else 1.0 - bk[0])
            px_ref = float(s.get("buy_px") or buy_px or 0.0)
            unconf_b = unconfirmed(s["recent"], max(qb, 0.0), "buy")
            # spent = the larger of what fills / positions show, plus anything not yet visible (conservative)
            s["spent"] = round(max(mf(b, "bought_usd"), bought_pos * px_ref) + unconf_b * px_ref, 2)
            left = float(s["proceeds"]) - float(s["spent"])
            if live(b):
                pass
            elif left < self.cfg.min_order_usd or s["attempts"] >= self.cfg.leg_attempts or ambiguous("buy"):
                if unconfirmed(s["recent"], max(qb, 0.0), "buy") < 1:
                    s["phase"] = "done"
                    changed = True
            elif bk is not None:
                depth = bk[3] if bs == "yes" else bk[2]
                try:
                    reserved = float(self.others_reserved()) if callable(self.others_reserved) else 0.0
                except Exception:  # noqa: BLE001
                    reserved = float("inf")
                # The unspent proceeds are OUR carve-out (other strategies subtract it before spending), so they
                # are ours to spend. Other strategies' carve-outs are honoured out of the cash that is NOT ours:
                # their reserves are budget-based and can exceed real cash (2026-10-11: fresh_gap + fair_band
                # reserved ~$11.1k against ~$10k buying power) -- subtracting them from the whole balance zeroed
                # every buy leg (4 swaps sold $2,893, bought $0).
                if free_cash is None:
                    cash = float("inf")
                else:
                    bp = max(float(free_cash), 0.0)
                    cash = bp - min(max(reserved, 0.0), max(bp - left, 0.0))
                spend = min(left, cash)
                qty = int(math.floor(min(spend / max(buy_px, 0.01), _cap(depth))))
                if qty >= 1 and qty * buy_px >= self.cfg.min_order_usd:
                    if await place(b, "buy", bs, buy_px, qty, max(qb, 0.0)):
                        s["buy_px"] = buy_px
                s["attempts"] += 1
                changed = True
            else:
                s["attempts"] += 1                   # no sane book: count it, give up after leg_attempts
                changed = True
        # ---------------- done: release both markets once nothing of ours rests ----------------
        if s is not None and s.get("phase") == "done" and not live(a) and not live(b) \
                and not self.ownership.orders_in(SID, a) and not self.ownership.orders_in(SID, b):
            sold_only = float(s.get("proceeds") or 0.0) >= self.cfg.min_order_usd and \
                float(s.get("spent") or 0.0) < 0.5 * float(s.get("proceeds") or 0.0)
            self._finish(s, "sold_only" if sold_only else "done", now)
            out["closed"] = s["id"]
            changed = True
        # prune registered ids the venue stopped listing (filled / expired) after 3 clean reads
        for oid in self.ownership.orders_of(SID):
            if oid in shown or oid in pend_ids:
                continue
            seen_n = (s or {}).setdefault("_unseen", {}).get(oid, 0) + 1 if s else 3
            if s:
                s["_unseen"][oid] = seen_n
            if seen_n >= 3:
                self.ownership.forget_orders([oid])
        out["phase"] = (self.swap or {}).get("phase")
        self._gen += 1
        if changed:
            self._save()
        return out

    def _finish(self, s: dict, result: str, now: float) -> None:
        for ex in (s["sell_ex"], s["buy_ex"]):
            self.ownership.release(SID, ex)
        rec = {k: s.get(k) for k in ("id", "sell_ex", "sell_side", "buy_ex", "buy_side", "advantage", "proceeds",
                                     "spent", "sell_px", "buy_px")}
        rec.update(result=result, ts=now)
        self.history.append(rec)
        self.swap = None

    def _save(self) -> None:
        try:
            self.save()
        except OSError:
            pass
