"""FAIR-BAND MAKER — live pilot trader (2026-10-08). Decision rule: sig/engine/fair_band.py (band_quotes).

ISOLATION (the point of this module):
  * EXCLUSIVE MARKETS via sig/engine/ownership.py: it only trades markets it owns, and it only claims markets where
    the account holds no position and has no open order. The broker refuses every other strategy's orders and
    cancels there (entries, exits, MM, arb, OMS timeouts, chase), and refuses this trader anywhere else. Because
    nobody else trades an owned market, the venue position there IS this trader's position — SIG's YES/NO netting
    can only ever net its own inventory.
  * BUYS ONLY: bid = buy YES at px; ask = buy NO at 1 - px. Never a sell, so never a naked short.
  * HARD CAPITAL: committed = cost of owned positions + notional of its open/pending orders; every placement must
    keep committed <= budget_usd and the market's exposure <= market_cap_usd. unused_budget() is registered as a
    carve-out on the orchestrator's CapitalLedger, so no other strategy can spend it either.
  * OWN WRITE CAP (max_writes_per_min, default 6 of the venue's 30/min), cancels before places.
  * KILL SWITCHES: loss limit (fills in owned markets, valued at SIG mid) or the STOP file -> wind-down:
    cancel all own orders, place nothing, release markets that are flat. Wind-down is sticky until restart.

Venue truth each tick: positions + ALL open orders. If the open-orders read fails the tick is skipped (a partial
list could cause duplicate orders). Recently placed orders (< pending_ttl_s) count as present until the venue
shows them, so a lagging read cannot double-place.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sig.engine.fair_band import FairBandConfig, band_quotes

SID = "fair_band"


@dataclass(frozen=True)
class FairBandLiveConfig:
    w: float = 0.03
    quote_usd: float = 500.0
    market_cap_usd: float = 1500.0
    budget_usd: float = 10000.0
    n_markets: int = 10
    max_writes_per_min: int = 6
    max_loss_usd: float = 750.0
    max_row_age_s: float = 60.0
    requote_ticks: int = 2                # keep a resting quote up to this many ticks MORE conservative than target
    wind_down_cancels_per_min: int = 30   # cancels while winding down (risk-reducing; separate from writes cap)
    pending_ttl_s: float = 600.0          # forget an unseen placement only after this long AND ...
    pending_min_reads: int = 3            # ... this many SUCCESSFUL reads that did not show it (conservative)
    stop_file: str = "data/fair_band_live/STOP"
    quote_expiry_s: float = 900.0         # venue-side expiry of every quote (the venue cancels it if we vanish)
    send_deadline_s: float = 20.0         # never SEND a quote that waited longer than this in the write queue
    release_on_wind_down: bool = True     # winding down: hand markets (incl. inventory) back to the exits
    resume_after_clear_ticks: int = 5     # an orchestrator halt that clears for this many ticks resumes quoting
    px_min: float = 0.02                  # never quote a leg outside [px_min, px_max]
    px_max: float = 0.98


@dataclass
class _Pending:
    exchange_id: str
    outcome: str
    price: float
    qty: int
    ts: float
    order_id: str | None = None
    reads: int = 0                       # successful venue reads since placement that did not show it


@dataclass
class FairBandLive:
    broker: Any
    ownership: Any
    cfg: FairBandLiveConfig = field(default_factory=FairBandLiveConfig)
    clock: Any = time.time
    winding_down: bool = False
    halt_reason: str | None = None
    _writes: list = field(default_factory=list)
    _pending: list = field(default_factory=list)
    _committed: float = 0.0
    # PROBATION: markets claimed in THIS process are not quoted until a tick AFTER the claim re-reads them. The
    # claim decision used a read taken before the claim, so another strategy could have placed in between
    # (time-of-check/time-of-use). After the claim the broker refuses everyone else, so a clean re-read proves
    # the market is ours alone; anything foreign there (a position or an order we never placed) -> release it
    # untouched. Markets loaded from the registry at start-up (we may already hold them) are trusted.
    _probation: dict = field(default_factory=dict)     # exchange_id -> tick generation it was claimed in
    _gen: int = 0
    _pos_cost: float = 0.0
    _resting_cost: float = 0.0
    # markets where an order we never placed showed up after probation (e.g. a pre-claim order the venue view
    # lagged on): never quoted again, never adopted; released once flat with none of our orders left.
    contaminated: set = field(default_factory=set)
    _no_fair_ticks: dict = field(default_factory=dict)   # exchange_id -> consecutive ticks without a fair
    _unseen: dict = field(default_factory=dict)          # our registered order id -> successful reads not listing it
    dead_after_ticks: int = 60

    # ---------------- capital ----------------
    def unused_budget(self) -> float:
        """Carve-out for the orchestrator's ledger: cash this trader may still need. SIG does NOT reserve cash for
        resting orders (journal 2026-10-05 stacking incident), so the cash behind our resting/pending quotes must
        stay carved out too: budget - cost of positions. Winding down: only what still rests (until cancelled)."""
        if self.winding_down:
            return max(round(self._resting_cost, 6), 0.0)            # round: float residue is not cash
        return max(round(self.cfg.budget_usd - self._pos_cost, 6), 0.0)

    @staticmethod
    def _exposure(yes_qty: float, mid: float) -> float:
        return yes_qty * mid if yes_qty > 0 else -yes_qty * (1.0 - mid)

    # ---------------- writes ----------------
    def _can_write(self, cancel: bool = False) -> bool:
        now = self.clock()
        self._writes = [t for t in self._writes if now - t < 60.0]
        cap = self.cfg.wind_down_cancels_per_min if (cancel and self.winding_down) else self.cfg.max_writes_per_min
        return len(self._writes) < cap

    def _wrote(self) -> None:
        self._writes.append(self.clock())

    # ---------------- market selection ----------------
    def restore_probation(self, max_age_s: float = 600.0) -> list[str]:
        """After a restart: markets claimed within max_age_s go back on probation (judged on the next read),
        so a claim that raced a foreign order right before a crash is still checked."""
        from datetime import datetime, timezone
        out = []
        for ex in self.ownership.owned_by(SID):
            ts = self.ownership.claimed_at(ex)
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(ts))).total_seconds()
            except (TypeError, ValueError):
                continue
            if age <= max_age_s and not self.ownership.orders_in(SID, ex):
                self._probation[ex] = self._gen
                out.append(ex)
        return out

    def select_markets(self, candidates: list[str], positions: dict[str, float], open_orders: list[dict]) -> list[str]:
        """Claim up to n_markets from `candidates` (best first) where the account is FLAT and has NO open order.
        Markets already owned by this trader are kept. Returns the owned set."""
        if getattr(self.ownership, "corrupt", False):
            self.halt("ownership_registry_corrupt")
            return []
        owned = sorted(self.ownership.owned_by(SID))
        busy = {str(o["exchange_id"]) for o in open_orders}
        want = self.cfg.n_markets - len(owned)
        new = []
        for ex in candidates:
            if want <= 0:
                break
            ex = str(ex)
            if ex in owned or self.ownership.owner_of(ex) is not None:
                continue
            if abs(positions.get(ex, 0.0)) >= 1 or ex in busy:
                continue
            new.append(ex)
            want -= 1
        if new:
            for ex in self.ownership.claim(SID, new):
                self._probation[ex] = self._gen
        return sorted(self.ownership.owned_by(SID))

    # ---------------- kill switches ----------------
    def halt(self, reason: str) -> None:
        if not self.winding_down:
            self.winding_down = True
            self.halt_reason = reason

    def check_kill(self, pnl_usd: float | None, venue_pnl_usd: float | None = None,
                   orchestrator_halted: bool = False) -> None:
        """Wind-down on: STOP file, either loss measure at/below -max_loss, or the orchestrator being halted. All
        sticky until restart EXCEPT an orchestrator halt, which resumes after it has been clear for
        resume_after_clear_ticks consecutive checks (an operator resume() must not leave the pilot dead)."""
        if os.path.exists(self.cfg.stop_file):
            self.halt("stop_file")
        if orchestrator_halted:
            self._halt_clear = 0
            self.halt("orchestrator_halted")
        elif self.winding_down and self.halt_reason == "orchestrator_halted":
            self._halt_clear = getattr(self, "_halt_clear", 0) + 1
            if self._halt_clear >= self.cfg.resume_after_clear_ticks:
                self.winding_down, self.halt_reason, self._halt_clear = False, None, 0
        for name, v in (("loss_limit_fills", pnl_usd), ("loss_limit_venue", venue_pnl_usd)):
            if v is not None and v <= -abs(self.cfg.max_loss_usd):
                self.halt(f"{name} {v:.0f}")

    async def cancel_all_registered(self, max_n: int = 100) -> int:
        """Cancel every order registered to us WITHOUT needing a venue read (startup clean slate, shutdown, and
        wind-down when reads fail). Confirmed cancels are forgotten; others stay registered for the next pass."""
        n = 0
        for oid in self.ownership.orders_of(SID)[:max_n]:
            try:
                res = await self.broker.cancel_order(oid, strategy_id=SID)
            except Exception:  # noqa: BLE001
                continue
            st = str(getattr(res, "status", "")).lower()
            if st in ("canceled", "cancelled") or st == "rejected" and "404" in str(getattr(res, "raw", "")):
                self.ownership.forget_orders([oid])
                n += 1
        self._pending = []
        return n

    async def cancel_open_registered(self, read_timeout_s: float = 10.0) -> int:
        """Startup / shutdown clean slate that only cancels what is ACTUALLY resting. Registered ids the venue no
        longer lists as open (expired / filled / cancelled) are forgotten without a write: SIG answers a DELETE of
        an expired order only after ~60s, with a 429 (live 2026-10-09 05:31 and 05:42: 9 expired ids stalled the
        pilot's start 6+ min and tripped the write breaker). If the open-orders read fails we fall back to the
        blind cancel (correct, just slow)."""
        try:
            opens = await asyncio.wait_for(self.broker.get_open_orders_all(), timeout=read_timeout_s)
        except Exception:  # noqa: BLE001 — includes timeout
            opens = None
        if opens is None:
            return await self.cancel_all_registered()
        open_ids = {str(o.get("order_id") or o.get("id") or "") for o in opens}
        mine = [str(x) for x in self.ownership.orders_of(SID)]
        gone = [oid for oid in mine if oid not in open_ids]
        if gone:
            self.ownership.forget_orders(gone)
        n = 0
        for oid in [oid for oid in mine if oid in open_ids]:
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
    async def tick(self, rows: dict[str, dict], positions: dict[str, float], open_orders: list[dict] | None,
                   free_cash: float | None) -> dict:
        """rows: exchange_id -> {fair, best_bid, best_ask, bid_size, ask_size, age_s}; positions: exchange_id ->
        signed YES qty (whole account); open_orders: ALL account open orders (None = read failed); free_cash: venue
        buying power. Returns a summary dict."""
        out = {"placed": 0, "cancelled": 0, "skipped_writes": 0, "winding_down": self.winding_down}
        if open_orders is None:
            out["skipped"] = "open_orders_read_failed"
            return out
        now = self.clock()
        # probation: judge only markets claimed in an EARLIER generation, i.e. on a read taken after the claim
        released = []
        for ex, g in sorted(self._probation.items()):
            if g >= self._gen:
                continue
            foreign = abs(positions.get(ex, 0.0)) >= 1 or any(
                str(o["exchange_id"]) == ex and self.ownership.order_owner(o["order_id"]) != SID for o in open_orders)
            if foreign:
                self.ownership.release(SID, ex)
                released.append(ex)
            del self._probation[ex]
        if released:
            out["released_foreign"] = released
        owned = self.ownership.owned_by(SID) - set(self._probation)       # probation markets: not traded yet
        in_owned = [o for o in open_orders if str(o["exchange_id"]) in owned]
        # an order in an owned market that we did not register (our own after a lost id / crash mid-POST, or a
        # stray foreign order that slipped in before the claim): never adopted, CANCELLED (we hold the cancel right:
        # unregistered orders are cancellable by anyone). Nothing may rest unmanaged in an owned market.
        strays = [o for o in in_owned if self.ownership.order_owner(o["order_id"]) != SID]
        mine = [o for o in in_owned if self.ownership.order_owner(o["order_id"]) == SID]
        # pending placements the venue hasn't shown yet. Kept until the venue SHOWS them (by order id; by
        # (market, side, price) only when the id is unknown) or until pending_min_reads successful reads AND
        # pending_ttl_s have passed without them (filled or cancelled before ever being listed). Never pruned on
        # time alone: a lagging/failing open-orders view must not make the trader forget a live order.
        shown_ids = {str(o["order_id"]) for o in open_orders}
        shown_keys = {(str(o["exchange_id"]), o["side"], round(float(o["price"]), 3)) for o in in_owned}
        keep = []
        for p in self._pending:
            if p.order_id is not None and p.order_id in shown_ids:
                continue
            if p.order_id is None and (p.exchange_id, p.outcome, p.price) in shown_keys:
                continue
            p.reads += 1
            if p.reads >= self.cfg.pending_min_reads and now - p.ts >= self.cfg.pending_ttl_s:
                continue
            keep.append(p)
        self._pending = keep

        def mid_of(ex):
            r = rows.get(ex) or {}
            try:
                return (float(r["best_bid"]) + float(r["best_ask"])) / 2.0
            except (KeyError, TypeError, ValueError):
                return None

        def leg_mid(ex, outcome):
            m = mid_of(ex)
            if m is None:
                return 1.0                       # unknown value: assume the most a share can be worth (caps)
            return m if outcome == "yes" else 1.0 - m

        def expo(ex):
            held = positions.get(ex, 0.0)
            m = mid_of(ex)
            return abs(held) * 1.0 if m is None else self._exposure(held, m)

        def notional(price, qty, ex=None, outcome=None):
            """Value an order at what it is worth IF FILLED: max(order price, that leg's mid). A buy below the mid
            marks UP on fill, so counting it at its price let filled positions overshoot the caps."""
            px = float(price)
            if ex is not None and outcome is not None:
                px = max(px, leg_mid(ex, outcome))
            return px * float(qty)

        pos_cost = sum(expo(ex) for ex in owned)
        open_cost = sum(notional(o["price"], o["quantity"], str(o["exchange_id"]), o["side"]) for o in mine
                        if o.get("action", "buy") == "buy")
        pend_cost = sum(notional(p.price, p.qty, p.exchange_id, p.outcome) for p in self._pending)
        self._committed = pos_cost + open_cost + pend_cost
        self._pos_cost, self._resting_cost = pos_cost, open_cost + pend_cost
        out["committed"] = round(self._committed, 2)

        # desired orders: (ex, outcome) -> (price, qty)
        desired: dict[tuple, tuple] = {}
        if not self.winding_down:
            fb = FairBandConfig(w=self.cfg.w, quote_usd=self.cfg.quote_usd, cap_usd=self.cfg.market_cap_usd,
                                universes=("pilot",))
            for ex in owned:
                r = rows.get(ex)
                if not r or r.get("fair") is None or float(r.get("age_s", 1e9)) > self.cfg.max_row_age_s:
                    continue                                    # stale/missing -> no quotes (cancels below)
                if mid_of(ex) is None:
                    continue
                for q in band_quotes({**r, "exchange_id": ex, "tags": {"pilot"}}, fb, positions.get(ex, 0.0)):
                    outc = "yes" if q["side"] == "bid" else "no"
                    px = round(q["px"] if outc == "yes" else 1.0 - q["px"], 3)
                    if not (self.cfg.px_min <= px <= self.cfg.px_max):
                        continue
                    qty = int(math.floor(self.cfg.quote_usd / max(px, 0.01)))
                    if qty >= 1:
                        desired[(ex, outc)] = (px, qty)

        # 1) CANCELS first: strays, duplicates, sells, and quotes outside the tolerance band. A quote is KEPT while
        #    target - requote_ticks*tick <= price <= target (same or more conservative): no churn on 1-tick moves.
        tol = self.cfg.requote_ticks * 0.005 + 1e-9
        seen: set = set()
        to_cancel = list(strays)
        for o in sorted(mine, key=lambda o: str(o["order_id"])):
            side_key = (str(o["exchange_id"]), o["side"])
            want = desired.get(side_key)
            px = round(float(o["price"]), 3)
            ok = want is not None and want[0] - tol <= px <= want[0] + 1e-9 and o.get("action", "buy") == "buy"
            if not ok or side_key in seen:
                to_cancel.append(o)
            else:
                seen.add(side_key)
        cancelled_ids = []
        for o in to_cancel:
            if not self._can_write(cancel=True):
                out["skipped_writes"] += 1
                seen.add((str(o["exchange_id"]), o["side"]))     # still resting: never place another on top
                continue
            res = await self.broker.cancel_order(o["order_id"], strategy_id=SID)
            self._wrote()
            if str(getattr(res, "status", "")).lower() in ("canceled", "cancelled"):
                out["cancelled"] += 1
                cancelled_ids.append(o["order_id"])
                if o in mine:
                    open_cost -= notional(o["price"], o["quantity"], str(o["exchange_id"]), o["side"])
                    self.ownership.forget_orders([o["order_id"]])
                else:
                    out.setdefault("stray_cancelled", []).append(o["order_id"])
            else:
                seen.add((str(o["exchange_id"]), o["side"]))     # unconfirmed: treat as still resting

        # 2) PLACES: desired keys not already resting/pending, within every cap
        have = seen | {(p.exchange_id, p.outcome) for p in self._pending}
        committed = pos_cost + max(open_cost, 0.0) + sum(notional(p.price, p.qty, p.exchange_id, p.outcome)
                                                          for p in self._pending)
        # SIG does not reserve cash for resting orders: our own resting/pending quotes already claim part of it
        cash = (float("inf") if free_cash is None
                else float(free_cash) - max(open_cost, 0.0) - sum(float(p.price) * p.qty for p in self._pending))
        for (ex, outcome), (px, qty) in sorted(desired.items()):
            if (ex, outcome) in have:
                continue
            held = positions.get(ex, 0.0)
            gone = set(cancelled_ids)
            mkt_open = sum(notional(o["price"], o["quantity"], ex, outcome) for o in mine
                           if str(o["exchange_id"]) == ex and o["side"] == outcome and o["order_id"] not in gone)
            mkt_open += sum(notional(p.price, p.qty, ex, outcome) for p in self._pending
                            if p.exchange_id == ex and p.outcome == outcome)
            adds = (outcome == "yes" and held >= 0) or (outcome == "no" and held <= 0)
            room_mkt = (self.cfg.market_cap_usd - expo(ex) - mkt_open) if adds else float("inf")
            unit = max(px, leg_mid(ex, outcome), 0.01)           # value per share if filled
            room = min(self.cfg.budget_usd - committed, room_mkt)
            qty = min(qty, int(math.floor(room / unit))) if room < float("inf") else qty
            qty = min(qty, int(math.floor(cash / max(px, 0.01)))) if cash < float("inf") else qty
            if qty < 1:
                continue
            if not self._can_write():
                out["skipped_writes"] += 1
                continue
            # unique idempotency scope per placement: SIG replays the stored response for an identical key, so a
            # re-quote at a price/size used before (after a cancel or fill) would otherwise place nothing.
            res = await self.broker.place_order(strategy_id=SID, market_id=f"{ex}#{uuid.uuid4().hex[:12]}",
                                                exchange_id=ex, side="buy", action="buy", quantity=int(qty),
                                                price=px, outcome=outcome, expiration_s=self.cfg.quote_expiry_s,
                                                send_deadline_s=self.cfg.send_deadline_s)
            self._wrote()
            st = getattr(res, "status", "")
            if st not in ("rejected",):
                oid = getattr(res, "order_id", None)
                self._pending.append(_Pending(ex, outcome, round(px, 3), int(qty), now,
                                              order_id=str(oid) if oid else None))
                committed += notional(px, qty, ex, outcome)
                cash -= float(px) * qty
                out["placed"] += 1
        self._committed = committed
        self._resting_cost = max(committed - pos_cost, 0.0)
        out["committed"] = round(committed, 2)

        # 3) housekeeping: release markets that are flat with no live/pending order of ours when winding down,
        # contaminated, or dead (no fair for dead_after_ticks ticks — e.g. settled / delisted)
        gone = set(cancelled_ids)
        for ex in owned:
            has_fair = (rows.get(ex) or {}).get("fair") is not None
            self._no_fair_ticks[ex] = 0 if has_fair else self._no_fair_ticks.get(ex, 0) + 1
            dead = self._no_fair_ticks[ex] >= self.dead_after_ticks
            if not (self.winding_down or dead):
                continue
            live = [o for o in mine if str(o["exchange_id"]) == ex and o["order_id"] not in gone]
            registered = [oid for oid in self.ownership.orders_in(SID, ex) if oid not in gone]
            flat = abs(positions.get(ex, 0.0)) < 1
            # hand-over: winding down with inventory -> release to the orchestrator's exits (managed, not frozen)
            # (never on a resumable orchestrator halt: that would strip our markets on a transient pause)
            handover = (self.winding_down and self.cfg.release_on_wind_down
                        and self.halt_reason != "orchestrator_halted")
            if ((flat or handover) and not live and not registered
                    and not any(p.exchange_id == ex for p in self._pending)):
                if self.ownership.release(SID, ex):
                    out.setdefault("released", []).append(ex)
                    self._no_fair_ticks.pop(ex, None)
        # prune registry ids of ours that the venue has not listed for 3 successful reads (filled / cancelled by
        # the venue) so the registry does not grow forever; pending ones are kept.
        pend_ids = {p.order_id for p in self._pending if p.order_id}
        for oid in self.ownership.orders_of(SID):
            if oid in shown_ids or oid in pend_ids:
                self._unseen.pop(oid, None)
                continue
            self._unseen[oid] = self._unseen.get(oid, 0) + 1
            if self._unseen[oid] >= 3:
                self.ownership.forget_orders([oid])
                self._unseen.pop(oid, None)
        out["winding_down"] = self.winding_down
        self._gen += 1
        return out


def liquidation_value(books: dict, ex, outcome) -> float:
    """Conservative per-share value for the LOSS LIMIT: min(what we could sell for now, fair) — YES at min(best bid,
    fair), NO at min(1 - best ask, 1 - fair). Capping at fair stops our OWN resting bid from propping up the value.
    A missing side falls back to fair - 5c (not 0: an empty bid side must not trip a false halt); no book and no
    fair -> 0. Never a guessed or stale mid, never 'unknown' (which disabled the limit)."""
    b = books.get(str(ex)) or {}
    yes = str(outcome).lower() == "yes"
    f = b.get("fair")
    leg_fair = None if f is None else (float(f) if yes else 1.0 - float(f))
    try:
        px = float(b["best_bid"]) if yes else 1.0 - float(b["best_ask"])
    except (KeyError, TypeError, ValueError):
        px = None
    if px is None:
        return max(leg_fair - 0.05, 0.0) if leg_fair is not None else 0.0
    return max(min(px, leg_fair) if leg_fair is not None else px, 0.0)


def venue_pnl(positions: list, owned: set, books: dict) -> float:
    """Unrealized P&L of owned markets from the VENUE's cost basis (BrokerPositionSnapshot: contract_id, side
    long/short, qty, avg_entry_price) at liquidation value. Verified live 2026-10-08: a NO holding is side short /
    negative YES qty and its avg_entry_price is the NO price."""
    tot = 0.0
    for p in positions or []:
        ex = str(getattr(p, "contract_id", ""))
        if ex not in owned:
            continue
        q = abs(float(getattr(p, "qty", 0.0) or 0.0))
        if q < 1:
            continue
        avg = float(getattr(p, "avg_entry_price", 0.0) or 0.0)
        short = str(getattr(p, "side", "")).lower() == "short"
        tot += q * (liquidation_value(books, ex, "no" if short else "yes") - avg)
    return tot


def fills_pnl(fills: list[dict], books: dict) -> float:
    """P&L of the trader's fills. The trader only BUYS, so every fill adds shares of its outcome.

    SIGN CONVENTION (verified on live strategy_fills 2026-10-08): `signed_qty` is a YES-DELTA — every NO buy is
    stored NEGATIVE. Shares = |quantity|.
    NETTING (fixed 2026-10-09): per market, matched YES/NO pairs were netted by the venue into $1 of cash each, so
    they are worth exactly $1 — valuing each leg separately (NO capped at 1 - fair, e.g. 1.35c on a 98.65% market)
    showed -$132 for a real -$19 and the gap grew with every round trip (false loss-limit halts). Only the NET
    remainder is valued at liquidation value (bid side, capped at fair; one-sided book -> fair - 5c)."""
    per: dict = {}
    for f in fills:
        ex = str(f["exchange_id"])
        q = f.get("quantity")
        q = abs(float(q if q is not None else (f.get("signed_qty") or 0.0)))
        m = per.setdefault(ex, {"yes": 0.0, "no": 0.0, "cash": 0.0})
        m["yes" if str(f.get("outcome", "yes")).lower() == "yes" else "no"] += q
        m["cash"] -= q * float(f["price"])
    tot = 0.0
    for ex, m in per.items():
        pairs = min(m["yes"], m["no"])
        net = m["yes"] - m["no"]
        rem = (net * liquidation_value(books, ex, "yes")) if net > 0 else (-net * liquidation_value(books, ex, "no"))
        tot += m["cash"] + pairs * 1.0 + rem
    return tot
