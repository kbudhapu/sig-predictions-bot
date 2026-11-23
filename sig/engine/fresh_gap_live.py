"""FRESH-DISLOCATION REVERSION — live pilot trader (2026-10-09). Signal: sig/engine/fresh_gap.py.

Trades the snap-back after SIG moves >= 3c AWAY from Polymarket within 24h: buy the side toward Poly, sell it when the
gap has half-closed / flipped / 48h passed. Research: tools/research/fresh_gap_bt.py; markets nobody holds did best
(+1.28c/share, 86% hit, n=29), so this pilot ONLY trades markets that are flat and unowned.

ISOLATION (same model as sig/engine/fair_band_live.py):
  * EXCLUSIVE MARKETS (sig/engine/ownership.py). A market is claimed only when the account is FLAT there, has NO open
    order there, nobody owns it, and it is not a cross contract. Claims sit on PROBATION until a later read shows
    nothing foreign appeared; then the broker refuses every other strategy there, so the venue position in an owned
    market IS this trader's position (SIG's YES/NO netting can only net its own shares).
  * SELLS ONLY WHAT IT HOLDS: an exit sells at most the venue position on the traded side; never a short.
  * HARD CAPITAL: entries keep cost(positions) + open/pending buys <= budget_usd; unused_budget() is a carve-out on the
    orchestrator's CapitalLedger. Per trade <= trade_usd, <= max_open trades, never more than the touch depth.
  * OWN WRITE CAP (max_writes_per_min of the account's 30/min). Entries/exits are marketable limits with a short
    venue expiry, so nothing rests unmanaged if we vanish.
  * KILL: STOP file / loss limit -> WIND-DOWN (no entries; every position is exited with sells; markets released
    when flat). Orchestrator halt -> PAUSE (no writes except cancels) until clear for resume_after_clear_ticks.
  * Venue truth each tick: positions + ALL open orders; if the open-orders read fails the tick is skipped. Placed
    orders count as present until the venue shows them or they could only have expired (pending tracking).
  * Restart-safe: trades persisted to state_path; owned markets without a record are exited (flat -> released).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sig.engine.fresh_gap import FreshGapConfig, Row, entry_signal, exit_reason

SID = "fresh_gap"


@dataclass(frozen=True)
class FreshGapLiveConfig:
    budget_usd: float = 15000.0
    trade_usd: float = 2500.0
    max_open: int = 6
    max_writes_per_min: int = 6
    wind_down_writes_per_min: int = 12     # risk-reducing exits; leaves room in the account's shared 30/min
    max_loss_usd: float = 750.0
    stop_file: str = "data/fresh_gap_live/STOP"
    state_path: str = "data/fresh_gap_live/trades.json"
    order_expiry_s: float = 120.0          # marketable limits: the venue cancels leftovers by itself
    send_deadline_s: float = 20.0
    entry_window_s: float = 900.0          # stop topping up an entry after this (hold what filled)
    cooldown_s: float = 6 * 3600.0         # no re-entry in a market for this long after an exit
    max_row_age_s: float = 60.0
    min_order_usd: float = 100.0
    max_exit_spread: float = 0.03          # never dump into a blown-out book: wait for a sane touch
    px_min: float = 0.03
    px_max: float = 0.97
    pending_ttl_s: float = 180.0           # a placement unseen this long (> order expiry) can only have filled/expired
    pending_min_reads: int = 2
    resume_after_clear_ticks: int = 5
    # fills our position read has not reflected yet are assumed to have happened for this long (stale/lagging
    # position reads must never cause an oversell or a second buy); after it, the venue position is trusted.
    confirm_s: float = 600.0
    # MAKER ENTRIES (2026-10-10; shadow: maker prices +2c/share vs taker): rest a buy at our side's best bid with a
    # venue expiry (never needs a cancel unless the signal dies), cross only after maker_fallback_after_s.
    entry_mode: str = "taker"              # "taker" | "maker"
    # MAKER EXITS (2026-10-11, OFF by default): profitable exits ('closed', 'timeout') rest a sell at our side's best
    # ask (YES ask / NO ask = 1 - YES bid) with maker_expiry_s, crossing to the bid after maker_exit_fallback_s.
    # Risk-reducing exits (gap flipped, wind-down / loss / STOP) always sell at the bid immediately.
    exit_mode: str = "taker"               # "taker" | "maker"
    maker_exit_fallback_s: float = 900.0
    maker_expiry_s: float = 300.0
    maker_fallback_after_s: float = 600.0
    # an order that has LEFT the book is resolved from the ingested fills (strategy_fills, ingested every 60s) once
    # this long has passed — until then (or without fills data) it is assumed fully filled (oversell/double-buy guard)
    resolve_after_gone_s: float = 150.0
    # election night: markets resolve on REAL news -> no entries after this instant, and everything is exited.
    election_cutoff_iso: str = "2026-11-02T00:00:00+00:00"
    signal: FreshGapConfig = field(default_factory=FreshGapConfig)


@dataclass
class _Pending:
    exchange_id: str
    action: str
    outcome: str
    price: float
    qty: int
    ts: float
    order_id: str | None = None
    reads: int = 0


def _held_px(side: str, bid: float, ask: float) -> tuple[float, float]:
    """(buy_px, sell_px) of the HELD side at the touch: YES buys at ask/sells at bid; NO buys at 1-bid/sells at 1-ask."""
    return (ask, bid) if side == "yes" else (1.0 - bid, 1.0 - ask)


@dataclass
class FreshGapLive:
    broker: Any
    ownership: Any
    cfg: FreshGapLiveConfig = field(default_factory=FreshGapLiveConfig)
    clock: Any = time.time
    exclude: set = field(default_factory=set)        # cross contracts etc. — never claimed
    winding_down: bool = False
    halt_reason: str | None = None
    paused: bool = False
    trades: dict = field(default_factory=dict)       # exchange_id -> trade record (persisted)
    closed: list = field(default_factory=list)
    _writes: list = field(default_factory=list)
    _pending: list = field(default_factory=list)
    _cooldown: dict = field(default_factory=dict)
    _gen: int = 0
    _pos_cost: float = 0.0
    _resting_cost: float = 0.0
    _clear: int = 0
    _unseen: dict = field(default_factory=dict)
    # cash OTHER strategies have reserved (ledger carve-outs: fair_band, cross resting maker entries, ...). SIG
    # reserves no cash for resting orders, so spending it would let their resting orders fill without cash.
    others_reserved: Any = None

    # ---------------- persistence ----------------
    def load(self) -> None:
        try:
            with open(self.cfg.state_path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.trades = {str(k): v for k, v in (d.get("trades") or {}).items()}
            self._cooldown = {str(k): float(v) for k, v in (d.get("cooldown") or {}).items()}
        except (OSError, ValueError):
            self.trades, self._cooldown = {}, {}
        for t in self.trades.values():
            if t.get("phase") == "probation":
                t["gen"] = -1          # tick counter restarts at 0: judge the claim on the FIRST read after restart
        # markets we own without a record (lost state) -> exit whatever is there, then release
        for ex in self.ownership.owned_by(SID):
            if ex not in self.trades:
                self.trades[ex] = {"side": None, "phase": "exiting", "reason": "orphan_on_load", "gen": -1}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.cfg.state_path) or ".", exist_ok=True)
        tmp = self.cfg.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"trades": self.trades, "cooldown": self._cooldown}, fh)
        os.replace(tmp, self.cfg.state_path)

    # ---------------- capital / writes / kill ----------------
    def unused_budget(self) -> float:
        """Ledger carve-out: budget - cost of held positions (SIG reserves no cash for resting orders, so the cash
        behind our open/pending buys stays carved out). Winding down: only what still rests."""
        if self.winding_down:
            return max(round(self._resting_cost, 6), 0.0)
        return max(round(self.cfg.budget_usd - self._pos_cost, 6), 0.0)

    def _can_write(self) -> bool:
        now = self.clock()
        self._writes = [t for t in self._writes if now - t < 60.0]
        cap = self.cfg.wind_down_writes_per_min if self.winding_down else self.cfg.max_writes_per_min
        return len(self._writes) < cap

    def _wrote(self) -> None:
        self._writes.append(self.clock())

    def halt(self, reason: str) -> None:
        if not self.winding_down:
            self.winding_down, self.halt_reason = True, reason

    def _past_cutoff(self) -> bool:
        from datetime import datetime
        try:
            return self.clock() >= datetime.fromisoformat(self.cfg.election_cutoff_iso).timestamp()
        except (TypeError, ValueError):
            return True                                  # unparseable cutoff: fail safe (no new risk)

    def check_kill(self, pnl_usd: float | None, orchestrator_halted: bool = False,
                   venue_pnl_usd: float | None = None) -> None:
        """Wind-down on STOP file, EITHER loss measure (fills since start / venue cost basis of what it holds) at or
        below -max_loss, the election cutoff, or a corrupt registry. Orchestrator halt pauses (resumable)."""
        if os.path.exists(self.cfg.stop_file):
            self.halt("stop_file")
        for name, v in (("loss_limit_fills", pnl_usd), ("loss_limit_venue", venue_pnl_usd)):
            if v is not None and v <= -abs(self.cfg.max_loss_usd):
                self.halt(f"{name} {v:.0f}")
        if self._past_cutoff():
            self.halt("election_cutoff")
        if orchestrator_halted:
            self.paused, self._clear = True, 0
        elif self.paused:
            self._clear += 1
            if self._clear >= self.cfg.resume_after_clear_ticks:
                self.paused, self._clear = False, 0
        if getattr(self.ownership, "corrupt", False):
            self.halt("ownership_registry_corrupt")

    async def cancel_open_registered(self, read_timeout_s: float = 10.0) -> int:
        """Clean slate (startup/shutdown): forget registered ids the venue no longer lists, cancel the rest."""
        try:
            opens = await asyncio.wait_for(self.broker.get_open_orders_all(), timeout=read_timeout_s)
        except Exception:  # noqa: BLE001
            opens = None
        mine = [str(x) for x in self.ownership.orders_of(SID)]
        if opens is not None:
            open_ids = {str(o.get("order_id") or o.get("id") or "") for o in opens}
            gone = [oid for oid in mine if oid not in open_ids]
            if gone:
                self.ownership.forget_orders(gone)
            mine = [oid for oid in mine if oid in open_ids]
        n = 0
        for oid in mine:
            try:
                res = await self.broker.cancel_order(oid, strategy_id=SID)
            except Exception:  # noqa: BLE001
                continue
            if str(getattr(res, "status", "")).lower() in ("canceled", "cancelled"):
                self.ownership.forget_orders([oid])
                n += 1
        self._pending = []
        return n

    # ---------------- one tick ----------------
    async def tick(self, rows: dict, past: dict, positions: dict, open_orders: list | None,
                   free_cash: float | None, fills_by_order: dict | None = None) -> dict:
        """rows: ex -> {sharp, best_bid, best_ask, bid_size, ask_size, age_s}; past: ex -> (mid_24h, sharp_24h);
        positions: ex -> signed YES qty (whole account); open_orders: ALL account open orders (None = read failed)."""
        out = {"placed": 0, "cancelled": 0, "skipped_writes": 0, "winding_down": self.winding_down,
               "paused": self.paused}
        if open_orders is None:
            out["skipped"] = "open_orders_read_failed"
            return out
        now = self.clock()
        changed = False
        if self._past_cutoff():
            self.halt("election_cutoff")
        owned = set(self.ownership.owned_by(SID))
        shown_ids = {str(o["order_id"]) for o in open_orders}

        # pending placements not yet shown by the venue (kept until shown, or unseen long enough to have expired)
        keep = []
        for p in self._pending:
            if p.order_id is not None and p.order_id in shown_ids:
                continue
            p.reads += 1
            if p.reads >= self.cfg.pending_min_reads and now - p.ts >= self.cfg.pending_ttl_s:
                continue
            keep.append(p)
        self._pending = keep

        # 1) probation: a claim from an EARLIER tick is judged on this read
        for ex, t in list(self.trades.items()):
            if t.get("phase") != "probation" or t.get("gen", 0) >= self._gen:
                continue
            foreign = abs(positions.get(ex, 0.0)) >= 1 or any(
                str(o["exchange_id"]) == ex and self.ownership.order_owner(o["order_id"]) != SID for o in open_orders)
            if foreign or ex not in owned:
                self.ownership.release(SID, ex)
                self.trades.pop(ex, None)
                out.setdefault("released_foreign", []).append(ex)
            else:
                t["phase"] = "entering"
            changed = True

        owned = set(self.ownership.owned_by(SID))
        in_owned = [o for o in open_orders if str(o["exchange_id"]) in owned]
        strays = [o for o in in_owned if self.ownership.order_owner(o["order_id"]) != SID]
        mine = [o for o in in_owned if self.ownership.order_owner(o["order_id"]) == SID]

        def r_ok(ex):
            r = rows.get(ex) or {}
            try:
                b, a = float(r["best_bid"]), float(r["best_ask"])
            except (KeyError, TypeError, ValueError):
                return None
            if not (0.0 < b < a < 1.0) or float(r.get("age_s", 0.0)) > self.cfg.max_row_age_s:
                return None
            return r

        def held(ex, side):
            q = positions.get(ex, 0.0)
            return q if side == "yes" else -q

        def mid_held(ex, side):
            r = r_ok(ex)
            if r is None:
                return 1.0
            m = (float(r["best_bid"]) + float(r["best_ask"])) / 2.0
            return m if side == "yes" else 1.0 - m

        def unconfirmed(t, action, q_now):
            """Shares our own recent orders of `action` may have moved that the position read does not show yet.
            Assumed FILLED for confirm_s (a stale/lagging position read must never cause an oversell or a second
            buy); after that the venue position is trusted."""
            rec, assumed = [], 0.0
            pend_ids = {p.order_id for p in self._pending if p.order_id}
            for x in t.get("recent", []):
                if x[1] != action or now - float(x[0]) >= self.cfg.confirm_s:
                    continue
                oid = x[4] if len(x) > 4 else None
                if oid and (oid in shown_ids or oid in pend_ids):
                    continue                             # still resting / not yet shown: live, not assumed filled
                qty = float(x[2])
                if oid:
                    if len(x) < 6 or not x[5]:
                        while len(x) < 6:
                            x.append(None)
                        x[5] = now                       # first tick it is gone from the book
                    if (fills_by_order is not None and now - float(x[5]) >= self.cfg.resolve_after_gone_s):
                        qty = float(fills_by_order.get(oid, 0.0))   # resolved from the ingested fills
                rec.append(x)
                assumed += qty
            if not rec:
                return 0.0
            base = float(rec[0][3])                      # held quantity when the first of these was placed
            seen = (base - q_now) if action == "sell" else (q_now - base)
            return max(0.0, assumed - max(seen, 0.0))

        for t in self.trades.values():                  # forget confirmations older than the window
            if t.get("recent"):
                t["recent"] = [x for x in t["recent"] if now - float(x[0]) < self.cfg.confirm_s]

        # capital (positions incl. unconfirmed buys, valued at the held-side mid; unknown book -> $1/share)
        pos_cost = 0.0
        for ex, t in self.trades.items():
            if t.get("side"):
                qh = max(held(ex, t["side"]), 0.0)
                pos_cost += (qh + unconfirmed(t, "buy", qh)) * mid_held(ex, t["side"])
        buy_open = sum(float(o["price"]) * float(o["quantity"]) for o in mine if o.get("action", "buy") == "buy")
        buy_pend = sum(p.price * p.qty for p in self._pending if p.action == "buy")
        committed = pos_cost + buy_open + buy_pend
        self._pos_cost, self._resting_cost = pos_cost, buy_open + buy_pend
        try:
            reserved_by_others = float(self.others_reserved()) if callable(self.others_reserved) else 0.0
        except Exception:  # noqa: BLE001 — unknown reserves: assume ALL cash is reserved (no new buys)
            reserved_by_others = float("inf")
        cash = (float("inf") if free_cash is None
                else float(free_cash) - buy_open - buy_pend - max(reserved_by_others, 0.0))

        # 2) cancel strays in our markets (unregistered orders are cancellable by anyone)
        for o in strays:
            if not self._can_write():
                out["skipped_writes"] += 1
                continue
            res = await self.broker.cancel_order(o["order_id"], strategy_id=SID)
            self._wrote()
            if str(getattr(res, "status", "")).lower() in ("canceled", "cancelled"):
                out["cancelled"] += 1

        def live(ex):
            return [o for o in mine if str(o["exchange_id"]) == ex] + [p for p in self._pending if p.exchange_id == ex]

        async def place(ex, action, outcome, px, qty, t, q_now, expiry_s=None) -> bool:
            if self.paused or not self._can_write():
                out["skipped_writes"] += 1
                return False
            self._wrote()                                # counted BEFORE sending: a raise still used the budget
            try:
                res = await self.broker.place_order(strategy_id=SID, market_id=f"{ex}#{uuid.uuid4().hex[:12]}",
                                                    exchange_id=ex, side=action, action=action, quantity=int(qty),
                                                    price=round(px, 3), outcome=outcome,
                                                    expiration_s=expiry_s or self.cfg.order_expiry_s,
                                                    send_deadline_s=self.cfg.send_deadline_s)
            except Exception as exc:  # noqa: BLE001 — AMBIGUOUS: it may have reached the venue
                self._pending.append(_Pending(ex, action, outcome, round(px, 3), int(qty), now, order_id=None))
                t.setdefault("recent", []).append([now, action, int(qty), q_now, None, None])
                out.setdefault("place_errors", []).append(f"{ex}:{type(exc).__name__}")
                return False
            if getattr(res, "status", "") == "rejected":
                return False
            oid = getattr(res, "order_id", None)
            self._pending.append(_Pending(ex, action, outcome, round(px, 3), int(qty), now,
                                          order_id=str(oid) if oid else None))
            t.setdefault("recent", []).append([now, action, int(qty), q_now, str(oid) if oid else None, None])
            out["placed"] += 1
            return True

        # 3) manage every trade
        for ex, t in list(self.trades.items()):
            if t.get("phase") == "probation":
                continue
            side = t.get("side")
            q = held(ex, side) if side else abs(positions.get(ex, 0.0))
            if side is None:                         # orphan: infer the side from the venue position
                side = "yes" if positions.get(ex, 0.0) > 0 else "no"
            if q < -0.5:                             # holding the OTHER side in an owned market: never expected
                self.halt(f"wrong_side_position {ex}")
                q = 0.0
            r = r_ok(ex)
            fair = None
            if r is not None and r.get("sharp") is not None:
                try:
                    fair = float(r["sharp"])
                except (TypeError, ValueError):
                    fair = None                      # no usable Polymarket fair: no entries, exits on time only
            if self.winding_down and t["phase"] in ("entering", "holding"):
                t["phase"], t["reason"] = "exiting", t.get("reason") or "wind_down"
                changed = True
            if t["phase"] == "entering":
                if live(ex):
                    if self.cfg.entry_mode == "maker" and r is not None:
                        still_now = fair is not None and ex in past and entry_signal(
                            Row(ex, fair, float(r["best_bid"]), float(r["best_ask"]), past[ex][0], past[ex][1]),
                            self.cfg.signal) == side
                        if not still_now:
                            # signal died while our entry rests: cancel ONLY orders the venue still lists as open
                            # (a DELETE of a filled/expired order costs ~60s and a 429 on SIG)
                            for o in [o for o in mine if str(o["exchange_id"]) == ex and o.get("action") == "buy"]:
                                if not self._can_write():
                                    out["skipped_writes"] += 1
                                    break
                                self._wrote()
                                try:
                                    res = await self.broker.cancel_order(o["order_id"], strategy_id=SID)
                                except Exception:  # noqa: BLE001
                                    continue
                                if str(getattr(res, "status", "")).lower() in ("canceled", "cancelled"):
                                    out["cancelled"] += 1
                    continue
                age = now - float(t["epoch"])
                q_est = max(q, 0.0) + unconfirmed(t, "buy", max(q, 0.0))
                filled_usd = q_est * mid_held(ex, side)
                still = r is not None and fair is not None and ex in past and entry_signal(
                    Row(ex, fair, float(r["best_bid"]), float(r["best_ask"]), past[ex][0], past[ex][1]),
                    self.cfg.signal) == side
                unresolved = q_est - max(q, 0.0)              # assumed-filled shares the venue doesn't show yet
                confirmed_usd = max(q, 0.0) * mid_held(ex, side)
                if (confirmed_usd >= 0.9 * self.cfg.trade_usd or age >= self.cfg.entry_window_s or not still):
                    if unresolved >= 1 and q < 1:
                        continue                              # unknown whether it filled: decide once resolved
                    t["phase"] = "holding" if q_est >= 1 else "done"
                    changed = True
                elif filled_usd >= 0.9 * self.cfg.trade_usd:
                    continue                                  # (assumed) complete: place nothing, wait to resolve
                elif r is not None:
                    maker = self.cfg.entry_mode == "maker" and age < self.cfg.maker_fallback_after_s
                    if maker:
                        # join OUR side's best bid: YES at the YES bid, NO at 1 - YES ask (= the NO bid)
                        buy_px = float(r["best_bid"]) if side == "yes" else 1.0 - float(r["best_ask"])
                        depth = float("inf")             # a resting bid is not limited by the opposite touch
                    else:
                        buy_px, _ = _held_px(side, float(r["best_bid"]), float(r["best_ask"]))
                        depth = float(r.get("ask_size" if side == "yes" else "bid_size") or 0.0)
                    room = min(self.cfg.trade_usd - filled_usd, self.cfg.budget_usd - committed, cash)
                    qty = int(math.floor(min(room / max(buy_px, 0.01), depth)))
                    if (self.cfg.px_min <= buy_px <= self.cfg.px_max and qty * buy_px >= self.cfg.min_order_usd
                            and not self.winding_down):
                        if await place(ex, "buy", side, buy_px, qty, t, max(q, 0.0),
                                       expiry_s=self.cfg.maker_expiry_s if maker else None):
                            committed += qty * buy_px
                            cash -= qty * buy_px
            if t["phase"] == "holding":
                why = None
                if r is not None and fair is not None:
                    why = exit_reason(float(t["entry_gap"]), side, now - float(t["epoch"]), fair,
                                      float(r["best_bid"]), float(r["best_ask"]), self.cfg.signal)
                elif now - float(t["epoch"]) >= self.cfg.signal.max_hold_s:
                    why = "timeout_no_fair_or_book"
                if why:
                    t["phase"], t["reason"], t["exit_at"] = "exiting", why, now
                    changed = True
            if t["phase"] == "exiting":
                sellable = max(q, 0.0) - unconfirmed(t, "sell", max(q, 0.0))
                if q < 1:
                    if not live(ex) and unconfirmed(t, "buy", max(q, 0.0)) < 1:
                        t["phase"] = "done"
                        changed = True
                elif (not live(ex) and r is not None and sellable >= 1
                      and float(r["best_ask"]) - float(r["best_bid"]) <= self.cfg.max_exit_spread + 1e-9):
                    maker_exit = (self.cfg.exit_mode == "maker" and not self.winding_down
                                  and t.get("reason") in ("closed", "timeout")
                                  and now - float(t.get("exit_at", now)) < self.cfg.maker_exit_fallback_s)
                    if maker_exit:
                        # rest at OUR side's ask: YES at the YES ask, NO at 1 - YES bid (= the NO ask)
                        sell_px = float(r["best_ask"]) if side == "yes" else 1.0 - float(r["best_bid"])
                    else:
                        _, sell_px = _held_px(side, float(r["best_bid"]), float(r["best_ask"]))
                    await place(ex, "sell", side, sell_px, int(math.floor(sellable)), t, max(q, 0.0),
                                expiry_s=self.cfg.maker_expiry_s if maker_exit else None)
            if t["phase"] == "done" and not live(ex) and not self.ownership.orders_in(SID, ex):
                if abs(positions.get(ex, 0.0)) < 1 and unconfirmed(t, "buy", 0.0) < 1:
                    self.ownership.release(SID, ex)
                    self._cooldown[ex] = now
                    self.closed.append({"exchange_id": ex, "side": side, "reason": t.get("reason"), "ts": now})
                    self.trades.pop(ex, None)
                    out.setdefault("closed", []).append(ex)
                    changed = True
                else:
                    t["phase"] = "exiting"            # something is still held: keep exiting
                    changed = True

        # 4) new entries: claim on PROBATION (orders only on a later tick, after a clean re-read)
        if not (self.winding_down or self.paused):
            n_open = len(self.trades)
            busy = {str(o["exchange_id"]) for o in open_orders}
            cands = []
            for ex, r in rows.items():
                ex = str(ex)
                if (ex in self.trades or ex in self.exclude or ex in busy or ex not in past
                        or abs(positions.get(ex, 0.0)) >= 1 or self.ownership.owner_of(ex) is not None
                        or now - self._cooldown.get(ex, -1e18) < self.cfg.cooldown_s):
                    continue
                rr = r_ok(ex)
                if rr is None or rr.get("sharp") is None:
                    continue
                row = Row(ex, float(rr["sharp"]), float(rr["best_bid"]), float(rr["best_ask"]), past[ex][0], past[ex][1])
                side = entry_signal(row, self.cfg.signal)
                if side:
                    cands.append((abs(row.gap), ex, side, row.gap))
            for _, ex, side, gap in sorted(cands, reverse=True):
                if n_open >= self.cfg.max_open or committed + self.cfg.trade_usd > self.cfg.budget_usd:
                    break
                if ex in self.ownership.claim(SID, [ex]):
                    self.trades[ex] = {"side": side, "entry_gap": round(gap, 4), "epoch": now, "phase": "probation",
                                       "gen": self._gen, "id": uuid.uuid4().hex[:10]}
                    n_open += 1
                    committed += self.cfg.trade_usd          # reserve until the entry resolves
                    out.setdefault("claimed", []).append(ex)
                    changed = True

        # prune registry ids the venue stopped listing (filled / expired) after 3 successful reads
        pend_ids = {p.order_id for p in self._pending if p.order_id}
        for oid in self.ownership.orders_of(SID):
            if oid in shown_ids or oid in pend_ids:
                self._unseen.pop(oid, None)
                continue
            self._unseen[oid] = self._unseen.get(oid, 0) + 1
            if self._unseen[oid] >= 3:
                self.ownership.forget_orders([oid])
                self._unseen.pop(oid, None)

        if changed:
            try:
                self.save()
            except OSError:
                pass
        out.update(open=len(self.trades), committed=round(committed, 2), winding_down=self.winding_down,
                   paused=self.paused)
        self._gen += 1
        return out


def fills_pnl(fills: list[dict], books: dict) -> float:
    """P&L of this trader's fills (buys AND sells). strategy_fills.signed_qty is a YES-delta (NO buys negative, YES
    sells negative). Cash: buys pay q*p, sells receive q*p (p in the outcome's price space). The net YES position per
    market is valued at liquidation (YES at bid, NO at 1-ask); a missing book values it at 0 (conservative)."""
    cash, net = 0.0, {}
    for f in fills:
        ex = str(f["exchange_id"])
        q = abs(float(f.get("quantity") or 0.0))
        sq = float(f.get("signed_qty") or 0.0)
        outcome = str(f.get("outcome", "yes")).lower()
        buy = (sq > 0) if outcome == "yes" else (sq < 0)
        cash += (-q if buy else q) * float(f["price"])
        net[ex] = net.get(ex, 0.0) + sq
    val = 0.0
    for ex, y in net.items():
        b = books.get(ex) or {}
        try:
            bid, ask = float(b["best_bid"]), float(b["best_ask"])
        except (KeyError, TypeError, ValueError):
            continue
        val += y * bid if y > 0 else (-y) * (1.0 - ask)
    return cash + val
